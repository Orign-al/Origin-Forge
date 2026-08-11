from datetime import timedelta

from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordActionPurpose,
    PasswordActionTokenState,
    PasswordState,
)
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalPasswordCredential,
    PortalPasswordSetupToken,
    PortalRole,
    PortalSession,
    PortalUser,
    utcnow,
)
from h100_portal_api.security import digest_secret, hash_password, random_token
from sqlalchemy import select


def csrf_headers(client, origin_headers):  # type: ignore[no-untyped-def]
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    return {**origin_headers, "X-CSRF-Token": token}


def invite_origin(database, expired: bool = False):  # type: ignore[no-untyped-def]
    owner = database.scalar(select(PortalRole).where(PortalRole.name == "platform_owner"))
    user = PortalUser(
        login_name="Origin-al",
        normalized_login="origin-al",
        display_name="Origin-al",
        unix_username="origin-al",
        account_state=AccountState.INVITED,
        password_state=PasswordState.SETUP_REQUIRED,
        resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        roles=[owner],
    )
    database.add(user)
    database.flush()
    token = random_token(48)
    database.add(
        PortalPasswordSetupToken(
            user_id=user.id,
            token_hash=digest_secret(token),
            purpose=PasswordActionPurpose.INITIAL_PASSWORD_SETUP,
            state=PasswordActionTokenState.ACTIVE,
            expires_at=utcnow() + timedelta(minutes=-1 if expired else 30),
        )
    )
    database.commit()
    return user, token


def active_user(database, role: str = "platform_owner"):  # type: ignore[no-untyped-def]
    selected_role = database.scalar(select(PortalRole).where(PortalRole.name == role))
    user = PortalUser(
        login_name="Origin-al" if role == "platform_owner" else "auditor-user",
        normalized_login="origin-al" if role == "platform_owner" else "auditor-user",
        display_name="Origin-al" if role == "platform_owner" else "Auditor",
        unix_username="origin-al" if role == "platform_owner" else None,
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
            password_hash=hash_password("A long portal passphrase 2026"),
            password_changed_at=utcnow(),
        )
    )
    database.commit()
    return user


def test_first_password_setup_activates_account_and_token_is_single_use(
    client, database, origin_headers
) -> None:  # type: ignore[no-untyped-def]
    user, token = invite_origin(database)
    exchange = client.post(
        "/api/v1/auth/password-action/exchange",
        headers=csrf_headers(client, origin_headers),
        json={"token": token},
    )
    assert exchange.status_code == 200
    response = client.post(
        "/api/v1/auth/setup-password",
        headers={**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]},
        json={
            "password": "这是 Origin-al 的网页长口令 2026",
            "confirmation": "这是 Origin-al 的网页长口令 2026",
        },
    )
    assert response.status_code == 200
    database.expire_all()
    refreshed = database.get(PortalUser, user.id)
    assert refreshed.account_state == AccountState.ACTIVE
    assert refreshed.password_state == PasswordState.SET
    assert refreshed.resource_onboarding_state == OnboardingState.NOT_ENROLLED
    assert client.cookies.get("h100_session")
    audit_types = database.scalars(select(PortalAuditEvent.event_type)).all()
    assert "PASSWORD_SETUP_COMPLETED" in audit_types
    assert "session.create" in audit_types
    replay = client.post(
        "/api/v1/auth/password-action/exchange",
        headers=csrf_headers(client, origin_headers),
        json={"token": token},
    )
    assert replay.status_code == 400
    assert replay.json()["detail"]["code"] == "PASSWORD_ACTION_USED"


def test_expired_setup_token_is_rejected(client, database, origin_headers) -> None:  # type: ignore[no-untyped-def]
    _user, token = invite_origin(database, expired=True)
    response = client.post(
        "/api/v1/auth/password-action/exchange",
        headers=csrf_headers(client, origin_headers),
        json={"token": token},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "PASSWORD_ACTION_EXPIRED"


def test_login_is_case_insensitive_and_rotates_existing_session(
    client, database, origin_headers
) -> None:  # type: ignore[no-untyped-def]
    user = active_user(database)
    first = client.post(
        "/api/v1/auth/login",
        headers=csrf_headers(client, origin_headers),
        json={"username": "ORIGIN-AL", "password": "A long portal passphrase 2026"},
    )
    assert first.status_code == 200
    first_cookie = client.cookies.get("h100_session")
    second = client.post(
        "/api/v1/auth/login",
        headers=csrf_headers(client, origin_headers),
        json={"username": "Origin-Al", "password": "A long portal passphrase 2026"},
    )
    assert second.status_code == 200
    assert client.cookies.get("h100_session") != first_cookie
    sessions = database.scalars(select(PortalSession).where(PortalSession.user_id == user.id)).all()
    assert len(sessions) == 2
    assert sum(session.revoked_at is None for session in sessions) == 1


def test_csrf_and_origin_are_required(client, database) -> None:  # type: ignore[no-untyped-def]
    active_user(database)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "origin-al", "password": "A long portal passphrase 2026"},
    )
    assert response.status_code == 403


def test_non_owner_cannot_create_operation(client, database, origin_headers) -> None:  # type: ignore[no-untyped-def]
    active_user(database, role="auditor")
    login = client.post(
        "/api/v1/auth/login",
        headers=csrf_headers(client, origin_headers),
        json={"username": "auditor-user", "password": "A long portal passphrase 2026"},
    )
    assert login.status_code == 200
    response = client.post(
        "/api/v1/operations",
        headers={**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]},
        json={
            "operation_type": "user.plan",
            "target_type": "user",
            "target_id": "example-user",
            "request_summary": "dry-run user plan",
            "payload": {"username": "example-user"},
            "idempotency_key": "auditor-denied-0001",
        },
    )
    assert response.status_code == 403


def test_account_can_revoke_other_sessions_without_revoking_current(
    client, database, origin_headers
) -> None:  # type: ignore[no-untyped-def]
    active_user(database)
    login = client.post(
        "/api/v1/auth/login",
        headers=csrf_headers(client, origin_headers),
        json={"username": "origin-al", "password": "A long portal passphrase 2026"},
    )
    assert login.status_code == 200
    response = client.post(
        "/api/v1/auth/sessions/revoke-others",
        headers={**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]},
    )
    assert response.status_code == 200
    assert response.json() == {"revoked": 0}
    assert client.get("/api/v1/auth/me").status_code == 200


def test_password_change_revokes_current_session_and_rotates_cookie(
    client, database, origin_headers
) -> None:  # type: ignore[no-untyped-def]
    user = active_user(database)
    login = client.post(
        "/api/v1/auth/login",
        headers=csrf_headers(client, origin_headers),
        json={"username": "origin-al", "password": "A long portal passphrase 2026"},
    )
    assert login.status_code == 200
    old_cookie = client.cookies["h100_session"]
    response = client.post(
        "/api/v1/auth/password",
        headers={**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]},
        json={
            "current_password": "A long portal passphrase 2026",
            "new_password": "A newly rotated portal passphrase 2026",
            "confirmation": "A newly rotated portal passphrase 2026",
        },
    )
    assert response.status_code == 200
    assert client.cookies["h100_session"] != old_cookie
    sessions = database.scalars(select(PortalSession).where(PortalSession.user_id == user.id)).all()
    # The assertion below is deliberately made from the user row, not a raw
    # cookie, so a stale current session cannot remain usable.
    assert sum(session.revoked_at is None for session in sessions) == 1
    assert client.get("/api/v1/auth/me").status_code == 200


def test_owner_sees_real_identity_counts_and_empty_image_inventory(
    client, database, origin_headers
) -> None:  # type: ignore[no-untyped-def]
    active_user(database)
    login = client.post(
        "/api/v1/auth/login",
        headers=csrf_headers(client, origin_headers),
        json={"username": "origin-al", "password": "A long portal passphrase 2026"},
    )
    assert login.status_code == 200
    overview = client.get("/api/v1/platform/overview")
    assert overview.status_code == 200
    assert overview.json()["identity"]["portal_users"] == 1
    assert overview.json()["identity"]["managed_linux_users"] == 0
    images = client.get("/api/v1/images")
    assert images.status_code == 200
    assert images.json()["images"] == []
    assert images.json()["live_status"] == "UNKNOWN"


def test_owner_operation_stays_draft_until_submit(
    client, database, origin_headers, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    active_user(database)
    login = client.post(
        "/api/v1/auth/login",
        headers=csrf_headers(client, origin_headers),
        json={"username": "origin-al", "password": "A long portal passphrase 2026"},
    )
    assert login.status_code == 200
    headers = {**origin_headers, "X-CSRF-Token": client.cookies["h100_csrf"]}
    monkeypatch.setattr(
        "h100_portal_api.routes.operations.call_worker",
        lambda *_args, **_kwargs: {
            "status": "DRY_RUN",
            "plan_status": "READY",
            "execution_enabled": False,
            "proposed_username": "origin-pilot",
            "conflicts": [],
            "validation_results": [],
        },
    )
    created = client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "operation_type": "user.plan",
            "target_type": "compute_identity",
            "target_id": "origin-pilot",
            "request_summary": "Origin-pilot resource plan only",
            "payload": {"username": "origin-pilot"},
            "idempotency_key": "origin-pilot-plan-0001",
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == OperationStatus.DRAFT
    operation_id = created.json()["id"]
    submitted = client.post(
        f"/api/v1/operations/{operation_id}/submit",
        headers=headers,
        json={"confirmation": "origin-pilot"},
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == OperationStatus.PENDING_APPROVAL
    database.expire_all()
    audit_types = database.scalars(select(PortalAuditEvent.event_type)).all()
    assert "operation.draft" in audit_types
    assert "operation.submit" in audit_types
