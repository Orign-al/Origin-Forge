import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordState,
    RiskLevel,
)
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalManagedUser,
    PortalOperation,
    PortalPasswordCredential,
    PortalRole,
    PortalSshKey,
    PortalUser,
    utcnow,
)
from h100_portal_api.routes import ssh_keys as ssh_key_routes
from h100_portal_api.security import hash_password
from h100_portal_api.ssh_keys import validate_ssh_public_key
from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class TemporaryKey:
    public_key: str
    private_key: str


def _generate_temporary_key(tmp_path: Path, name: str) -> TemporaryKey:
    path = tmp_path / name
    subprocess.run(
        [
            "/usr/bin/ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "Portal test key",
            "-f",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    public_key = path.with_suffix(".pub").read_text(encoding="utf-8").strip()
    private_key = path.read_text(encoding="utf-8")
    path.unlink()
    path.with_suffix(".pub").unlink()
    return TemporaryKey(public_key=public_key, private_key=private_key)


@pytest.fixture
def temporary_key(tmp_path: Path) -> TemporaryKey:
    return _generate_temporary_key(tmp_path, "portal-test-ed25519")


def _staged_user(
    database: Session,
    *,
    login: str = "origin-user",
    unix_username: str = "origin-pilot",
    uid: int = 20001,
    gid: int = 20001,
    project_id: int = 30001,
    container_port: int = 22023,
) -> tuple[PortalUser, PortalManagedUser]:
    role = database.scalar(select(PortalRole).where(PortalRole.name == "user"))
    assert role is not None
    user = PortalUser(
        login_name=login,
        normalized_login=login,
        display_name="Origin User",
        unix_username=None,
        account_state=AccountState.ACTIVE,
        password_state=PasswordState.SET,
        resource_onboarding_state=OnboardingState.STAGED,
        roles=[role],
    )
    database.add(user)
    database.flush()
    database.add(
        PortalPasswordCredential(
            user_id=user.id,
            password_hash=hash_password("A long portal passphrase 2026"),
            password_changed_at=utcnow(),
        )
    )
    managed = PortalManagedUser(  # noqa: S604 -- ORM shell field, no subprocess shell.
        portal_user_id=user.id,
        unix_username=unix_username,
        uid=uid,
        gid=gid,
        shell="/usr/sbin/nologin",
        host_access_state="DISABLED",
        gpu_isolation_state="VERIFIED",
        slurm_account="company",
        slurm_qos="general",
        project_id=project_id,
        quota_bytes=300 * 1024**3,
        container_name=f"gpu-dev-{unix_username}",
        container_port=container_port,
        onboarding_state=OnboardingState.STAGED,
        ssh_key_state="REQUIRED_BEFORE_ACTIVATION",
        ssh_key_count=0,
        staged_at=utcnow(),
    )
    database.add(managed)
    database.commit()
    return user, managed


def _login(
    client,
    origin_headers: dict[str, str],
    login: str = "origin-user",
    *,
    enrollment_required: bool = True,
) -> dict[str, str]:  # type: ignore[no-untyped-def]
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        headers={**origin_headers, "X-CSRF-Token": csrf},
        json={"username": login, "password": "A long portal passphrase 2026"},
    )
    assert response.status_code == 200
    assert response.json()["ssh_enrollment"]["required"] is enrollment_required
    return {**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]}


def _worker_success(
    operation: str, *, payload: dict[str, object], **_kwargs: object
) -> dict[str, object]:
    assert operation == "ssh_key.prepare"
    serialized = json.dumps(payload)
    assert "PRIVATE KEY" not in serialized.upper()
    return {
        "status": "SUCCEEDED",
        "request_id": "worker-test-request",
        "record_id": payload["record_id"],
        "fingerprint_sha256": payload["fingerprint_sha256"],
        "content_sha256": payload["content_sha256"],
    }


def _post_key(client, headers: dict[str, str], user: PortalUser, public_key: str, **fields: object):  # type: ignore[no-untyped-def]
    validated = validate_ssh_public_key(public_key)
    body: dict[str, object] = {
        "key_type": validated.key_type,
        "public_key": public_key,
        "comment": "Origin laptop",
        "scope": "CONTAINER",
        "generation_method": "IMPORTED",
        "client_fingerprint_sha256": validated.fingerprint_sha256,
        "confirmed_public_key": True,
    }
    body.update(fields)
    return client.post(f"/api/v1/users/{user.id}/ssh-keys", headers=headers, json=body)


def test_self_service_import_stores_only_public_material_and_metadata(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, managed = _staged_user(database)
    headers = _login(client, origin_headers)
    monkeypatch.setattr(ssh_key_routes, "call_worker", _worker_success)

    response = _post_key(client, headers, user, temporary_key.public_key)

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "VALIDATED"
    assert payload["private_key_received"] is False
    assert payload["authorized_keys_installed"] is False
    assert "public_key" not in payload["key"]
    assert payload["key"]["scope"] == "CONTAINER"
    assert payload["key"]["state"] == "VALIDATED"
    database.expire_all()
    record = database.scalar(select(PortalSshKey))
    assert record is not None
    assert record.public_key is not None and record.public_key.startswith("ssh-ed25519 ")
    assert "PRIVATE KEY" not in record.public_key.upper()
    assert record.generation_method == "IMPORTED"
    assert record.enrollment_operation_id is not None
    refreshed = database.get(PortalManagedUser, managed.id)
    assert refreshed is not None
    assert refreshed.ssh_key_count == 1
    assert refreshed.ssh_key_state == "VALIDATED"
    listed = client.get(f"/api/v1/users/{user.id}/ssh-keys")
    assert listed.status_code == 200
    assert "public_key" not in listed.json()["keys"][0]


def test_private_key_is_rejected_without_worker_db_or_audit_body(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, _managed = _staged_user(database)
    headers = _login(client, origin_headers)
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("private material must not reach Root Worker"),
    )

    response = client.post(
        f"/api/v1/users/{user.id}/ssh-keys",
        headers=headers,
        json={
            "public_key": temporary_key.public_key,
            "key_type": "ssh-ed25519",
            "private_key": temporary_key.private_key,
            "scope": "CONTAINER",
            "generation_method": "IMPORTED",
            "confirmed_public_key": True,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SSH_PRIVATE_KEY_UPLOAD_REJECTED"
    assert database.scalar(select(PortalSshKey)) is None
    assert database.scalar(select(PortalOperation)) is None
    audit = database.scalar(
        select(PortalAuditEvent).where(
            PortalAuditEvent.event_type == "SSH_PRIVATE_KEY_UPLOAD_REJECTED"
        )
    )
    assert audit is not None
    assert "PRIVATE KEY" not in json.dumps(audit.safe_metadata).upper()
    assert audit.safe_metadata == {
        "error_code": "SSH_PRIVATE_KEY_UPLOAD_REJECTED",
        "body_stored": False,
    }


def test_nested_private_key_field_is_rejected_and_never_staged(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, _managed = _staged_user(database)
    headers = _login(client, origin_headers)
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("private material must not reach Root Worker"),
    )
    response = client.post(
        f"/api/v1/users/{user.id}/ssh-keys",
        headers=headers,
        json={"public_key": temporary_key.public_key, "metadata": {"private_key": "forbidden"}},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SSH_PRIVATE_KEY_UPLOAD_REJECTED"


def test_encrypted_private_key_armor_is_rejected_with_safe_audit(
    client,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, _managed = _staged_user(database)
    headers = _login(client, origin_headers)
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("private material must not reach Root Worker"),
    )
    response = client.post(
        f"/api/v1/users/{user.id}/ssh-keys",
        headers=headers,
        json={
            "key_type": "ssh-ed25519",
            "public_key": (
                "-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
                "forbidden\n"
                "-----END ENCRYPTED PRIVATE KEY-----"
            ),
            "scope": "CONTAINER",
            "generation_method": "IMPORTED",
            "confirmed_public_key": True,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SSH_PRIVATE_KEY_UPLOAD_REJECTED"
    audit = database.scalar(
        select(PortalAuditEvent).where(
            PortalAuditEvent.event_type == "SSH_PRIVATE_KEY_UPLOAD_REJECTED"
        )
    )
    assert audit is not None
    assert audit.safe_metadata["body_stored"] is False


@pytest.mark.parametrize(
    ("public_key", "expected_code"),
    [
        ("", "SSH_KEY_ENROLLMENT_REJECTED"),
        ("ssh-ed25519 !!! malformed-key-payload-over-minimum-length", "SSH_PUBLIC_KEY_MALFORMED"),
    ],
)
def test_empty_and_malformed_public_keys_are_rejected_before_worker(
    client,
    database: Session,
    origin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    public_key: str,
    expected_code: str,
) -> None:  # type: ignore[no-untyped-def]
    user, _managed = _staged_user(database)
    headers = _login(client, origin_headers)
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("invalid key must not reach Root Worker"),
    )
    response = client.post(
        f"/api/v1/users/{user.id}/ssh-keys",
        headers=headers,
        json={
            "key_type": "ssh-ed25519",
            "public_key": public_key,
            "scope": "CONTAINER",
            "generation_method": "IMPORTED",
            "confirmed_public_key": True,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == expected_code


def test_declared_key_type_must_match_validated_public_key(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, _managed = _staged_user(database)
    headers = _login(client, origin_headers)
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("mismatched key must not reach Root Worker"),
    )
    response = client.post(
        f"/api/v1/users/{user.id}/ssh-keys",
        headers=headers,
        json={
            "key_type": "ecdsa-sha2-nistp256",
            "public_key": temporary_key.public_key,
            "scope": "CONTAINER",
            "generation_method": "IMPORTED",
            "confirmed_public_key": True,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SSH_PUBLIC_KEY_TYPE_MISMATCH"


def test_client_fingerprint_must_match_server_validation(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, _managed = _staged_user(database)
    headers = _login(client, origin_headers)
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("mismatched fingerprint must not reach Worker"),
    )
    response = _post_key(
        client,
        headers,
        user,
        temporary_key.public_key,
        client_fingerprint_sha256="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SSH_KEY_FINGERPRINT_MISMATCH"


def test_browser_generated_key_requires_saved_private_key_confirmation(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, _managed = _staged_user(database)
    headers = _login(client, origin_headers)
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("unconfirmed key must not reach Worker"),
    )
    response = _post_key(
        client,
        headers,
        user,
        temporary_key.public_key,
        generation_method="BROWSER_GENERATED",
        confirmed_public_key=False,
        confirmed_private_key_saved=False,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SSH_KEY_ENROLLMENT_REJECTED"


def _existing_record(
    database: Session,
    *,
    user: PortalUser,
    managed: PortalManagedUser,
    fingerprint: str,
    state: str = "VALIDATED",
) -> PortalSshKey:
    record_id = uuid.uuid4()
    operation = PortalOperation(
        operation_type="ssh_key.enroll",
        target_type="ssh_public_key",
        target_id=str(record_id),
        requested_by=user.id,
        owner_managed_user_id=managed.id,
        approved_by=user.id,
        request_summary="test SSH key enrollment",
        validated_payload={"fingerprint_sha256": fingerprint},
        idempotency_key=f"test-key:{record_id}",
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.SUCCEEDED,
    )
    database.add(operation)
    database.flush()
    revoked = state == "REVOKED"
    record = PortalSshKey(
        id=record_id,
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        key_type="ssh-ed25519",
        fingerprint_sha256=fingerprint,
        public_key="ssh-ed25519 TEST-ONLY-NOT-A-REAL-KEY",
        comment="test metadata record",
        scope="BOTH",
        state=state,
        generation_method="IMPORTED",
        created_by=user.id,
        enrollment_operation_id=operation.id,
        staging_file_name=f"{record_id}.pub",
        content_sha256=uuid.uuid4().hex * 2,
        approved_by=user.id,
        approved_at=utcnow(),
        validated_at=utcnow(),
        installed_at=utcnow() if state == "INSTALLED" else None,
        active=not revoked,
        revoked_at=utcnow() if revoked else None,
    )
    database.add(record)
    database.flush()
    return record


def test_duplicate_and_revoked_fingerprints_are_globally_rejected(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, managed = _staged_user(database)
    headers = _login(client, origin_headers)
    fingerprint = validate_ssh_public_key(temporary_key.public_key).fingerprint_sha256
    _existing_record(
        database,
        user=user,
        managed=managed,
        fingerprint=fingerprint,
        state="REVOKED",
    )
    database.commit()
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("duplicate key must not reach Root Worker"),
    )

    response = _post_key(client, headers, user, temporary_key.public_key)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SSH_KEY_FINGERPRINT_CONFLICT"


def test_same_user_can_enroll_a_second_distinct_key(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, managed = _staged_user(database)
    headers = _login(client, origin_headers)
    monkeypatch.setattr(ssh_key_routes, "call_worker", _worker_success)

    first = _post_key(client, headers, user, temporary_key.public_key)
    second_key = _generate_temporary_key(tmp_path, "portal-test-ed25519-second")
    second = _post_key(
        client,
        headers,
        user,
        second_key.public_key,
        comment="Second workstation",
        scope="CONTAINER",
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["key"]["scope"] == "CONTAINER"
    records = database.scalars(
        select(PortalSshKey).where(PortalSshKey.managed_user_id == managed.id)
    ).all()
    assert len(records) == 2
    assert len({record.fingerprint_sha256 for record in records}) == 2
    database.expire_all()
    refreshed = database.get(PortalManagedUser, managed.id)
    assert refreshed is not None
    assert refreshed.ssh_key_count == 2


def test_ordinary_user_cannot_enroll_host_scoped_key(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, _managed = _staged_user(database)
    headers = _login(client, origin_headers)
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("forbidden Host key must not reach Root Worker"),
    )

    response = _post_key(
        client,
        headers,
        user,
        temporary_key.public_key,
        scope="HOST",
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "HOST_KEY_SCOPE_FORBIDDEN"


def test_fingerprint_owned_by_another_user_is_rejected_globally(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    target_user, _target_managed = _staged_user(database)
    other_user, other_managed = _staged_user(
        database,
        login="other-user",
        unix_username="other-pilot",
        uid=20002,
        gid=20002,
        project_id=30002,
        container_port=22024,
    )
    fingerprint = validate_ssh_public_key(temporary_key.public_key).fingerprint_sha256
    _existing_record(
        database,
        user=other_user,
        managed=other_managed,
        fingerprint=fingerprint,
    )
    database.commit()
    headers = _login(client, origin_headers)
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("conflicting key must not reach Root Worker"),
    )

    response = _post_key(client, headers, target_user, temporary_key.public_key)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SSH_KEY_FINGERPRINT_CONFLICT"


def test_five_active_keys_enforce_limit_before_worker(
    client,
    database: Session,
    origin_headers: dict[str, str],
    temporary_key: TemporaryKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, managed = _staged_user(database)
    for index in range(5):
        _existing_record(
            database,
            user=user,
            managed=managed,
            fingerprint=f"SHA256:test-only-{index}",
        )
    managed.ssh_key_count = 5
    managed.ssh_key_state = "VALIDATED"
    database.commit()
    headers = _login(client, origin_headers, enrollment_required=False)
    monkeypatch.setattr(
        ssh_key_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("sixth key must not reach Root Worker"),
    )

    response = _post_key(client, headers, user, temporary_key.public_key)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SSH_KEY_LIMIT_REACHED"
