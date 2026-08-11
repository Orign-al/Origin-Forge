import json
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    PasswordActionPurpose,
    PasswordActionTokenState,
    PasswordState,
)
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalComputeLease,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalPasswordCredential,
    PortalPasswordSetupToken,
    PortalRole,
    PortalSession,
    PortalUser,
    utcnow,
)
from h100_portal_api.security import digest_secret, hash_password, verify_password
from sqlalchemy import func, select

PASSWORD = "A long Portal owner passphrase 2026"


def active_account(database, *, login: str, role: str) -> PortalUser:  # type: ignore[no-untyped-def]
    selected_role = database.scalar(select(PortalRole).where(PortalRole.name == role))
    user = PortalUser(
        login_name=login,
        normalized_login=login,
        display_name=login,
        unix_username=None,
        account_state=AccountState.ACTIVE,
        password_state=PasswordState.SET,
        resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        roles=[selected_role],
    )
    database.add(user)
    database.flush()
    database.add(
        PortalPasswordCredential(
            user_id=user.id,
            password_hash=hash_password(PASSWORD),
            password_changed_at=utcnow(),
        )
    )
    database.commit()
    return user


def login_headers(client, origin_headers, *, login: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    preauth = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    response = client.post(
        "/api/v1/auth/login",
        headers={**origin_headers, "X-CSRF-Token": preauth},
        json={"username": login, "password": PASSWORD},
    )
    assert response.status_code == 200
    return {**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]}


def create_invited(client, headers, login: str = "origin-pilot2"):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "login_name": login,
            "display_name": "Origin Pilot 2",
            "role": "user",
            "note": "Portal identity only",
        },
    )


def raw_token_from(response) -> str:  # type: ignore[no-untyped-def]
    split = urlsplit(response.json()["setup_url"])
    assert split.query == ""
    values = parse_qs(split.fragment)
    return values["token"][0]


def exchange(client, origin_headers, token: str):  # type: ignore[no-untyped-def]
    csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    return client.post(
        "/api/v1/auth/password-action/exchange",
        headers={**origin_headers, "X-CSRF-Token": csrf},
        json={"token": token},
    )


def test_admin_creates_invited_identity_without_compute_resources(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    owner = active_account(database, login="identity-owner", role="platform_owner")
    headers = login_headers(client, origin_headers, login=owner.normalized_login)
    monkeypatch.setattr(
        "h100_portal_api.routes.users.pwd.getpwnam", lambda _name: (_ for _ in ()).throw(KeyError)
    )

    response = create_invited(client, headers)

    assert response.status_code == 201
    payload = response.json()
    assert payload["user"]["normalized_login"] == "origin-pilot2"
    assert payload["user"]["account_state"] == "INVITED"
    assert payload["user"]["password_state"] == "SETUP_REQUIRED"
    assert payload["user"]["resource_onboarding_state"] == "NOT_ENROLLED"
    assert payload["user"]["unix_username"] is None
    assert payload["compute_resources_created"] is False
    assert database.scalar(select(func.count()).select_from(PortalManagedUser)) == 0
    assert database.scalar(select(func.count()).select_from(PortalContainer)) == 0
    assert database.scalar(select(func.count()).select_from(PortalComputeLease)) == 0
    assert database.scalar(select(func.count()).select_from(PortalPasswordCredential)) == 1
    operation = database.scalar(
        select(PortalOperation).where(PortalOperation.operation_type == "portal_user.create")
    )
    assert operation is not None
    assert operation.status == "SUCCEEDED"
    assert operation.validated_payload["compute_identity"] == "NOT_PROVISIONED"


def test_normalized_portal_compute_and_linux_conflicts_are_rejected(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    owner = active_account(database, login="conflict-owner", role="platform_owner")
    headers = login_headers(client, origin_headers, login=owner.normalized_login)
    monkeypatch.setattr(
        "h100_portal_api.routes.users.pwd.getpwnam", lambda _name: (_ for _ in ()).throw(KeyError)
    )
    assert create_invited(client, headers).status_code == 201
    duplicate = create_invited(client, headers, login="ORIGIN-PILOT2")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "PORTAL_LOGIN_CONFLICT"

    monkeypatch.setattr("h100_portal_api.routes.users.pwd.getpwnam", lambda _name: object())
    linux_conflict = create_invited(client, headers, login="linux-present")
    assert linux_conflict.status_code == 409
    assert linux_conflict.json()["detail"]["code"] == "LINUX_USERNAME_CONFLICT"


def test_role_restrictions_and_user_idor_are_enforced(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    admin = active_account(database, login="portal-admin", role="platform_admin")
    headers = login_headers(client, origin_headers, login=admin.normalized_login)
    monkeypatch.setattr(
        "h100_portal_api.routes.users.pwd.getpwnam", lambda _name: (_ for _ in ()).throw(KeyError)
    )
    elevated = client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "login_name": "forbidden-owner",
            "display_name": "Forbidden Owner",
            "role": "platform_owner",
            "note": None,
        },
    )
    assert elevated.status_code == 403

    target_response = create_invited(client, headers)
    assert target_response.status_code == 201
    target_id = target_response.json()["user"]["id"]
    ordinary = active_account(database, login="ordinary-user", role="user")
    headers = login_headers(client, origin_headers, login=ordinary.normalized_login)
    assert (
        client.post(f"/api/v1/users/{target_id}/password-setup-links", headers=headers).status_code
        == 403
    )
    assert (
        client.post(f"/api/v1/users/{target_id}/password-reset-links", headers=headers).status_code
        == 403
    )
    assert (
        client.get(f"/api/v1/users/{target_id}/password-action-tokens", headers=headers).status_code
        == 403
    )


def test_initial_setup_link_is_24h_hashed_single_use_and_regeneration_revokes_old(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    owner = active_account(database, login="setup-owner", role="platform_owner")
    headers = login_headers(client, origin_headers, login=owner.normalized_login)
    monkeypatch.setattr(
        "h100_portal_api.routes.users.pwd.getpwnam", lambda _name: (_ for _ in ()).throw(KeyError)
    )
    created = create_invited(client, headers)
    target_id = created.json()["user"]["id"]

    first = client.post(f"/api/v1/users/{target_id}/password-setup-links", headers=headers)
    assert first.status_code == 201
    first_raw = raw_token_from(first)
    first_record = database.scalar(
        select(PortalPasswordSetupToken).where(
            PortalPasswordSetupToken.token_hash == digest_secret(first_raw)
        )
    )
    assert first_record is not None
    assert first_record.token_hash != first_raw
    assert first_record.purpose == PasswordActionPurpose.INITIAL_PASSWORD_SETUP
    assert first_record.state == PasswordActionTokenState.ACTIVE
    lifetime = first_record.expires_at - first_record.created_at
    assert timedelta(hours=23, minutes=59) < lifetime <= timedelta(hours=24, seconds=1)

    second = client.post(f"/api/v1/users/{target_id}/password-setup-links", headers=headers)
    assert second.status_code == 201
    second_raw = raw_token_from(second)
    database.expire_all()
    assert (
        database.get(PortalPasswordSetupToken, first_record.id).state
        == PasswordActionTokenState.REVOKED
    )
    denied = exchange(client, origin_headers, first_raw)
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "PASSWORD_ACTION_REVOKED"

    accepted = exchange(client, origin_headers, second_raw)
    assert accepted.status_code == 200
    assert accepted.json()["username"] == "origin-pilot2"
    complete = client.post(
        "/api/v1/auth/setup-password",
        headers={**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]},
        json={
            "password": "Origin Pilot Two chooses this long passphrase 2026",
            "confirmation": "Origin Pilot Two chooses this long passphrase 2026",
        },
    )
    assert complete.status_code == 200
    assert complete.json()["requires_login"] is False
    database.expire_all()
    target = database.get(PortalUser, uuid.UUID(target_id))
    assert target.account_state == AccountState.ACTIVE
    assert target.password_state == PasswordState.SET
    assert target.resource_onboarding_state == OnboardingState.NOT_ENROLLED
    assert target.unix_username is None
    credential = database.scalar(
        select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == target.id)
    )
    assert credential is not None
    assert credential.password_hash.startswith("$argon2id$")
    replay = exchange(client, origin_headers, second_raw)
    assert replay.status_code == 400
    assert replay.json()["detail"]["code"] == "PASSWORD_ACTION_USED"


def test_password_reset_is_30_minutes_revokes_sessions_and_preserves_identity_state(
    client, database, origin_headers
) -> None:  # type: ignore[no-untyped-def]
    owner = active_account(database, login="reset-owner", role="platform_owner")
    target = active_account(database, login="reset-target", role="user")
    target.unix_username = "unchanged-compute-name"
    now = utcnow()
    for number in range(2):
        database.add(
            PortalSession(
                user_id=target.id,
                owner_managed_user_id=None,
                session_hash=digest_secret(f"old-session-{number}"),
                csrf_hash=digest_secret(f"old-csrf-{number}"),
                source_ip="127.0.0.1",
                user_agent_digest=digest_secret("test"),
                created_at=now,
                last_seen_at=now,
                idle_expires_at=now + timedelta(minutes=30),
                absolute_expires_at=now + timedelta(hours=12),
            )
        )
    database.commit()
    headers = login_headers(client, origin_headers, login=owner.normalized_login)
    first_response = client.post(f"/api/v1/users/{target.id}/password-reset-links", headers=headers)
    assert first_response.status_code == 201
    first_raw = raw_token_from(first_response)
    response = client.post(f"/api/v1/users/{target.id}/password-reset-links", headers=headers)
    assert response.status_code == 201
    raw = raw_token_from(response)
    record = database.scalar(
        select(PortalPasswordSetupToken).where(
            PortalPasswordSetupToken.token_hash == digest_secret(raw)
        )
    )
    assert record is not None
    assert record.purpose == PasswordActionPurpose.PASSWORD_RESET
    assert (
        timedelta(minutes=29, seconds=59)
        < record.expires_at - record.created_at
        <= timedelta(minutes=30, seconds=1)
    )
    revoked = exchange(client, origin_headers, first_raw)
    assert revoked.status_code == 400
    assert revoked.json()["detail"]["code"] == "PASSWORD_ACTION_REVOKED"

    assert exchange(client, origin_headers, raw).status_code == 200
    reset = client.post(
        "/api/v1/auth/setup-password",
        headers={**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]},
        json={
            "password": "Reset Target selects a new long passphrase 2026",
            "confirmation": "Reset Target selects a new long passphrase 2026",
        },
    )
    assert reset.status_code == 200
    assert reset.json()["requires_login"] is True
    database.expire_all()
    refreshed = database.get(PortalUser, target.id)
    assert refreshed.account_state == AccountState.ACTIVE
    assert refreshed.unix_username == "unchanged-compute-name"
    assert refreshed.resource_onboarding_state == OnboardingState.NOT_ENROLLED
    credential = database.scalar(
        select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == target.id)
    )
    assert verify_password(
        credential.password_hash, "Reset Target selects a new long passphrase 2026"
    )
    assert credential.password_hash.startswith("$argon2id$")
    sessions = database.scalars(
        select(PortalSession).where(PortalSession.user_id == target.id)
    ).all()
    assert sessions and all(item.revoked_at is not None for item in sessions)
    audit_types = database.scalars(select(PortalAuditEvent.event_type)).all()
    assert "PASSWORD_RESET_COMPLETED" in audit_types
    assert "SESSION_REVOKED_AFTER_PASSWORD_RESET" in audit_types
    replay = exchange(client, origin_headers, raw)
    assert replay.status_code == 400
    assert replay.json()["detail"]["code"] == "PASSWORD_ACTION_USED"


def test_password_action_challenge_expires_without_setting_password(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    owner = active_account(database, login="challenge-owner", role="platform_owner")
    headers = login_headers(client, origin_headers, login=owner.normalized_login)
    monkeypatch.setattr(
        "h100_portal_api.routes.users.pwd.getpwnam", lambda _name: (_ for _ in ()).throw(KeyError)
    )
    created = create_invited(client, headers, login="challenge-target")
    target_id = uuid.UUID(created.json()["user"]["id"])
    generated = client.post(f"/api/v1/users/{target_id}/password-setup-links", headers=headers)
    raw = raw_token_from(generated)
    assert exchange(client, origin_headers, raw).status_code == 200
    token = database.scalar(
        select(PortalPasswordSetupToken).where(
            PortalPasswordSetupToken.token_hash == digest_secret(raw)
        )
    )
    assert token is not None
    token.challenge_expires_at = utcnow() - timedelta(seconds=1)
    database.commit()

    denied = client.post(
        "/api/v1/auth/setup-password",
        headers={**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]},
        json={
            "password": "Challenge Target chooses a sufficiently long password 2026",
            "confirmation": "Challenge Target chooses a sufficiently long password 2026",
        },
    )
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "PASSWORD_ACTION_INVALID"
    assert (
        database.scalar(
            select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == target_id)
        )
        is None
    )


def test_account_state_is_rechecked_when_password_action_is_consumed(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    owner = active_account(database, login="state-owner", role="platform_owner")
    headers = login_headers(client, origin_headers, login=owner.normalized_login)
    monkeypatch.setattr(
        "h100_portal_api.routes.users.pwd.getpwnam", lambda _name: (_ for _ in ()).throw(KeyError)
    )
    created = create_invited(client, headers, login="state-target")
    target_id = uuid.UUID(created.json()["user"]["id"])
    generated = client.post(f"/api/v1/users/{target_id}/password-setup-links", headers=headers)
    raw = raw_token_from(generated)
    assert exchange(client, origin_headers, raw).status_code == 200
    target = database.get(PortalUser, target_id)
    assert target is not None
    target.account_state = AccountState.SUSPENDED
    database.commit()

    denied = client.post(
        "/api/v1/auth/setup-password",
        headers={**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]},
        json={
            "password": "State Target chooses a sufficiently long password 2026",
            "confirmation": "State Target chooses a sufficiently long password 2026",
        },
    )
    assert denied.status_code == 400
    assert denied.json()["detail"]["code"] == "PASSWORD_ACTION_INVALID"
    database.expire_all()
    assert database.get(PortalUser, target_id).account_state == AccountState.SUSPENDED
    assert (
        database.scalar(
            select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == target_id)
        )
        is None
    )
    token = database.scalar(
        select(PortalPasswordSetupToken).where(
            PortalPasswordSetupToken.token_hash == digest_secret(raw)
        )
    )
    assert token.state == PasswordActionTokenState.REVOKED


def test_token_metadata_and_audit_never_return_or_persist_raw_token(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    owner = active_account(database, login="redaction-owner", role="platform_owner")
    headers = login_headers(client, origin_headers, login=owner.normalized_login)
    monkeypatch.setattr(
        "h100_portal_api.routes.users.pwd.getpwnam", lambda _name: (_ for _ in ()).throw(KeyError)
    )
    created = create_invited(client, headers)
    target_id = created.json()["user"]["id"]
    generated = client.post(f"/api/v1/users/{target_id}/password-setup-links", headers=headers)
    raw = raw_token_from(generated)

    listed = client.get(f"/api/v1/users/{target_id}/password-action-tokens", headers=headers)
    assert listed.status_code == 200
    listed_text = json.dumps(listed.json(), sort_keys=True)
    assert raw not in listed_text
    assert "token_hash" not in listed_text
    assert "challenge_hash" not in listed_text
    audit_text = json.dumps(
        [event.safe_metadata for event in database.scalars(select(PortalAuditEvent)).all()],
        sort_keys=True,
    )
    operation_text = json.dumps(
        [row.validated_payload for row in database.scalars(select(PortalOperation)).all()],
        sort_keys=True,
    )
    assert raw not in audit_text
    assert raw not in operation_text
    assert "/setup-password" not in audit_text
    assert "/setup-password" not in operation_text


def test_validation_errors_do_not_echo_password_or_token(client, origin_headers) -> None:  # type: ignore[no-untyped-def]
    password = "unique-secret-value"
    response = client.post(
        "/api/v1/auth/setup-password",
        headers=origin_headers,
        json={"password": password, "confirmation": "different-secret-value"},
    )
    assert response.status_code == 422
    assert password not in response.text
    assert "different-secret-value" not in response.text

    raw_token = "unique-token-value-that-is-long-enough-for-validation"
    invalid = client.post(
        "/api/v1/auth/password-action/exchange",
        headers=origin_headers,
        json={"token": raw_token * 10},
    )
    assert invalid.status_code == 422
    assert raw_token not in invalid.text


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(-1, "PASSWORD_ACTION_EXPIRED"), (1, None)],
)
def test_setup_token_expiry_is_enforced(
    client, database, origin_headers, offset: int, expected: str | None
) -> None:  # type: ignore[no-untyped-def]
    user = PortalUser(
        login_name=f"expiry-user-{offset + 2}",
        normalized_login=f"expiry-user-{offset + 2}",
        display_name="Expiry User",
        account_state=AccountState.INVITED,
        password_state=PasswordState.SETUP_REQUIRED,
        resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        roles=[database.scalar(select(PortalRole).where(PortalRole.name == "user"))],
    )
    database.add(user)
    database.flush()
    raw = f"{'x' * 63}{offset + 2}"
    database.add(
        PortalPasswordSetupToken(
            user_id=user.id,
            token_hash=digest_secret(raw),
            purpose=PasswordActionPurpose.INITIAL_PASSWORD_SETUP,
            state=PasswordActionTokenState.ACTIVE,
            expires_at=utcnow() + timedelta(minutes=offset),
        )
    )
    database.commit()
    response = exchange(client, origin_headers, raw)
    if expected:
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == expected
    else:
        assert response.status_code == 200
