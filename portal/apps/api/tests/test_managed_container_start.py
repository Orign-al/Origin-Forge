import subprocess
import uuid
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
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalPasswordCredential,
    PortalRole,
    PortalSshKey,
    PortalUser,
    utcnow,
)
from h100_portal_api.routes import containers as container_routes
from h100_portal_api.security import hash_password
from h100_portal_api.ssh_keys import validate_ssh_public_key
from sqlalchemy import select
from sqlalchemy.orm import Session


def temporary_public_key(tmp_path: Path) -> str:
    key_path = tmp_path / "container-start-test-key"
    subprocess.run(
        [
            "/usr/bin/ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "Container start test",
            "-f",
            str(key_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    public_key = key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()
    key_path.unlink()
    key_path.with_suffix(".pub").unlink()
    return public_key


def active_container_owner(
    database: Session,
    tmp_path: Path,
    *,
    login: str = "origin-user",
    username: str = "origin-pilot",
    uid: int = 20001,
    port: int = 22023,
) -> tuple[PortalUser, PortalManagedUser, PortalContainer]:
    role = database.scalar(select(PortalRole).where(PortalRole.name == "user"))
    assert role is not None
    user = PortalUser(
        login_name=login,
        normalized_login=login,
        display_name="Origin User",
        unix_username=None,
        account_state=AccountState.ACTIVE,
        password_state=PasswordState.SET,
        resource_onboarding_state=OnboardingState.ACTIVE,
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
        unix_username=username,
        uid=uid,
        gid=uid,
        shell="/bin/bash",
        host_access_state="ENABLED",
        gpu_isolation_state="VERIFIED",
        slurm_account="company",
        slurm_qos="general",
        project_id=uid + 10000,
        quota_bytes=300 * 1024**3,
        container_name=f"gpu-dev-{username}",
        container_port=port,
        onboarding_state=OnboardingState.ACTIVE,
        ssh_key_state="INSTALLED",
        ssh_key_count=1,
        staged_at=utcnow(),
        compute_activated_at=utcnow(),
    )
    database.add(managed)
    database.flush()
    enrollment = PortalOperation(
        operation_type="ssh_key.enroll",
        target_type="ssh_public_key",
        target_id=str(uuid.uuid4()),
        requested_by=user.id,
        owner_managed_user_id=managed.id,
        approved_by=user.id,
        request_summary="Temporary public-key fixture",
        validated_payload={"public_material_only": True},
        idempotency_key=f"test-key:{uuid.uuid4()}",
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.SUCCEEDED,
        created_at=utcnow(),
        approved_at=utcnow(),
        started_at=utcnow(),
        finished_at=utcnow(),
    )
    database.add(enrollment)
    database.flush()
    validated = validate_ssh_public_key(temporary_public_key(tmp_path))
    key_id = uuid.uuid4()
    database.add(
        PortalSshKey(
            id=key_id,
            managed_user_id=managed.id,
            owner_managed_user_id=managed.id,
            key_type=validated.key_type,
            fingerprint_sha256=validated.fingerprint_sha256,
            public_key=validated.public_key,
            comment=validated.comment,
            scope="BOTH",
            state="INSTALLED",
            generation_method="IMPORTED",
            created_by=user.id,
            enrollment_operation_id=enrollment.id,
            staging_file_name=f"{key_id}.pub",
            content_sha256=validated.content_sha256,
            approved_by=user.id,
            approved_at=utcnow(),
            validated_at=utcnow(),
            installed_at=utcnow(),
            active=True,
            created_at=utcnow(),
        )
    )
    container = PortalContainer(
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        name=f"gpu-dev-{username}",
        image_digest="sha256:" + "a" * 64,
        ssh_port=port,
        desired_state="STOPPED",
        observed_state="STOPPED",
        safe_spec={
            "gpu": "NONE",
            "privileged": False,
            "host_network": False,
            "host_pid": False,
            "host_ipc": False,
            "docker_socket": False,
            "authorized_keys": "INSTALLED",
        },
    )
    database.add(container)
    database.commit()
    return user, managed, container


def login(client, origin_headers: dict[str, str], username: str = "origin-user") -> dict[str, str]:  # type: ignore[no-untyped-def]
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        headers={**origin_headers, "X-CSRF-Token": csrf},
        json={"username": username, "password": "A long portal passphrase 2026"},
    )
    assert response.status_code == 200
    return {**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]}


def start_body() -> dict[str, str]:
    return {
        "idempotency_key": str(uuid.uuid4()),
        "expected_compute_state": "ACTIVE",
        "expected_container_state": "STOPPED",
        "expected_ssh_key_state": "INSTALLED",
    }


def test_owned_active_container_start_uses_closed_worker_payload(
    client,
    database: Session,
    origin_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    user, managed, container = active_container_owner(database, tmp_path)
    headers = login(client, origin_headers)
    observed: dict[str, object] = {}

    def worker(operation: str, **kwargs: object) -> dict[str, object]:
        observed["operation"] = operation
        observed.update(kwargs)
        return {
            "status": "SUCCEEDED",
            "handler": "container.start",
            "request_id": "container-worker-test",
            "name": container.name,
            "username": managed.unix_username,
            "container_state": "RUNNING",
            "container_gpu": "NONE",
        }

    monkeypatch.setattr(container_routes, "call_worker", worker)
    body = start_body()
    response = client.post(f"/api/v1/containers/{container.name}/start", headers=headers, json=body)

    assert response.status_code == 200
    assert response.json()["container"] == {
        "name": container.name,
        "state": "RUNNING",
        "gpu": "NONE",
    }
    assert observed["operation"] == "container.start"
    assert observed["payload"] == {
        "name": container.name,
        "username": managed.unix_username,
        "managed_user_id": str(managed.id),
        "expected_compute_state": "ACTIVE",
        "expected_container_state": "STOPPED",
        "expected_ssh_key_state": "INSTALLED",
    }
    assert observed["requested_by"] == user.normalized_login
    assert observed["approved_by"] == user.normalized_login
    assert observed["dry_run"] is False
    assert observed["idempotency_key"] == (
        f"container-start:{managed.id}:{body['idempotency_key']}"
    )
    database.expire_all()
    refreshed = database.get(PortalContainer, container.id)
    assert refreshed is not None and refreshed.observed_state == "RUNNING"
    operation = database.scalar(
        select(PortalOperation).where(PortalOperation.operation_type == "container.start")
    )
    assert operation is not None and operation.status == OperationStatus.SUCCEEDED
    audit = database.scalar(
        select(PortalAuditEvent).where(PortalAuditEvent.event_type == "container.start")
    )
    assert audit is not None and audit.safe_metadata["gpu"] == "NONE"


@pytest.mark.parametrize(
    "mutation",
    ["STAGED", "NO_INSTALLED_KEY", "UNSAFE_GPU", "RUNNING"],
)
def test_container_start_rejects_ineligible_portal_state_before_worker(
    client,
    database: Session,
    origin_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:  # type: ignore[no-untyped-def]
    _user, managed, container = active_container_owner(database, tmp_path)
    if mutation == "STAGED":
        managed.onboarding_state = OnboardingState.STAGED
        managed.shell = "/usr/sbin/nologin"
        managed.host_access_state = "DISABLED"
    elif mutation == "NO_INSTALLED_KEY":
        managed.ssh_key_state = "VALIDATED"
    elif mutation == "UNSAFE_GPU":
        container.safe_spec = {**container.safe_spec, "gpu": "REQUESTED"}
    else:
        container.observed_state = "RUNNING"
    database.commit()
    headers = login(client, origin_headers)
    monkeypatch.setattr(
        container_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("Worker must not be called"),
    )

    response = client.post(
        f"/api/v1/containers/{container.name}/start",
        headers=headers,
        json=start_body(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONTAINER_START_STATE_REJECTED"


def test_container_start_rejects_cross_user_and_arbitrary_fields(
    client,
    database: Session,
    origin_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    _owner, _managed, container = active_container_owner(database, tmp_path)
    other, _other_managed, _other_container = active_container_owner(
        database,
        tmp_path,
        login="other-user",
        username="other-pilot",
        uid=20002,
        port=22024,
    )
    headers = login(client, origin_headers, username=other.normalized_login)
    monkeypatch.setattr(
        container_routes,
        "call_worker",
        lambda *_args, **_kwargs: pytest.fail("Worker must not be called"),
    )

    cross_user = client.post(
        f"/api/v1/containers/{container.name}/start",
        headers=headers,
        json=start_body(),
    )
    arbitrary = client.post(
        "/api/v1/containers/gpu-dev-other-pilot/start",
        headers=headers,
        json={**start_body(), "path": "/etc/shadow", "command": "docker start"},
    )

    assert cross_user.status_code == 404
    assert cross_user.json()["detail"]["code"] == "MANAGED_CONTAINER_NOT_FOUND"
    assert arbitrary.status_code == 422


def test_container_start_worker_failure_keeps_portal_stopped(
    client,
    database: Session,
    origin_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    _user, _managed, container = active_container_owner(database, tmp_path)
    headers = login(client, origin_headers)
    monkeypatch.setattr(
        container_routes,
        "call_worker",
        lambda *_args, **_kwargs: {
            "status": "ERROR",
            "error": {"code": "CONTAINER_START_SECURITY_REJECTED"},
        },
    )

    response = client.post(
        f"/api/v1/containers/{container.name}/start",
        headers=headers,
        json=start_body(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONTAINER_START_SECURITY_REJECTED"
    database.expire_all()
    refreshed = database.get(PortalContainer, container.id)
    assert refreshed is not None
    assert refreshed.desired_state == "STOPPED"
    assert refreshed.observed_state == "STOPPED"


def test_container_start_unproven_recovery_marks_observed_state_unknown(
    client,
    database: Session,
    origin_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    _user, _managed, container = active_container_owner(database, tmp_path)
    headers = login(client, origin_headers)
    monkeypatch.setattr(
        container_routes,
        "call_worker",
        lambda *_args, **_kwargs: {
            "status": "ERROR",
            "error": {"code": "CONTAINER_STOP_RECOVERY_FAILED"},
        },
    )

    response = client.post(
        f"/api/v1/containers/{container.name}/start",
        headers=headers,
        json=start_body(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONTAINER_STOP_RECOVERY_FAILED"
    database.expire_all()
    refreshed = database.get(PortalContainer, container.id)
    assert refreshed is not None
    assert refreshed.desired_state == "STOPPED"
    assert refreshed.observed_state == "UNKNOWN"
