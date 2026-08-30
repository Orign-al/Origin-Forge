import uuid
from contextlib import suppress
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.auth import (
    AUTH_ERROR,
    AuthContext,
    clear_session_cookies,
    client_ip,
    create_session,
    rate_limiter,
    require_preauth_csrf,
    require_recent_reauthentication,
    require_session_csrf,
    revoke_session,
    revoke_user_cli_tokens,
    revoke_user_sessions,
    serialize_user,
    session_response,
    set_session_cookies,
    user_agent,
)
from h100_portal_api.config import get_settings
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import auth_context
from h100_portal_api.enums import (
    AccountState,
    PasswordActionPurpose,
    PasswordActionTokenState,
    PasswordState,
)
from h100_portal_api.models import (
    PortalCliToken,
    PortalPasswordCredential,
    PortalPasswordSetupToken,
    PortalSession,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.rbac import role_names
from h100_portal_api.schemas import (
    ChangePasswordRequest,
    CliTokenCreateRequest,
    LoginRequest,
    PasswordActionExchangeRequest,
    ReauthenticateRequest,
    SetupPasswordRequest,
)
from h100_portal_api.security import (
    digest_secret,
    hash_password,
    normalize_login,
    random_token,
    validate_password,
    verify_password,
)
from h100_portal_api.ssh_keys import ssh_enrollment_status
from h100_portal_api.terminal_service import terminal_registry

router = APIRouter(prefix="/auth", tags=["auth"])


def _auth_failure(db: Session, request: Request, user: PortalUser | None, normalized: str) -> None:
    settings = get_settings()
    if user is not None:
        user.failed_login_count += 1
        if user.failed_login_count >= settings.login_failures_before_lock:
            user.locked_until = utcnow() + timedelta(minutes=settings.login_lock_minutes)
            user.account_state = AccountState.LOCKED
            revoke_user_cli_tokens(db, user.id)
        record_audit(
            db,
            event_type="login.failure",
            actor=normalized,
            actor_role="unknown",
            source_ip=client_ip(request),
            user_agent=user_agent(request),
            object_type="portal_user",
            object_id=str(user.id),
            result="DENIED",
            metadata={"reason": "invalid_credentials"},
        )
    else:
        record_audit(
            db,
            event_type="login.failure",
            actor="anonymous",
            actor_role="anonymous",
            source_ip=client_ip(request),
            user_agent=user_agent(request),
            object_type="login",
            object_id="unknown",
            result="DENIED",
            metadata={"reason": "invalid_credentials"},
        )
    db.commit()


@router.get("/csrf")
def csrf(request: Request, response: Response) -> dict[str, str]:
    # A signed pre-auth token is used by login and setup-password before a session exists.
    from h100_portal_api.security import signed_csrf_token

    token = signed_csrf_token()
    settings = get_settings()
    response.set_cookie(
        settings.csrf_cookie_name,
        token,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"csrf_token": token}


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    require_preauth_csrf(request)
    normalized = "invalid"
    with suppress(ValueError):
        normalized = normalize_login(body.username)
    ip_key = f"ip:{client_ip(request)}"
    account_key = f"account:{normalized}"
    if not rate_limiter.allowed(ip_key, 30, 300) or not rate_limiter.allowed(account_key, 10, 300):
        raise HTTPException(
            status_code=429, detail={"code": "RATE_LIMITED", "message": "请求过于频繁，请稍后重试"}
        )
    user = db.scalar(select(PortalUser).where(PortalUser.normalized_login == normalized))
    credential = (
        db.scalar(
            select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == user.id)
        )
        if user
        else None
    )
    valid = verify_password(credential.password_hash, body.password) if credential else False
    if not valid:
        # Keep a fixed Argon2 verification path for unknown accounts without storing a real password.
        verify_password(
            "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            body.password,
        )
        _auth_failure(db, request, user, normalized)
        raise HTTPException(status_code=401, detail=AUTH_ERROR)
    if (
        user is None
        or user.account_state != AccountState.ACTIVE
        or user.password_state not in {PasswordState.SET, PasswordState.RESET_REQUIRED}
        or (user.locked_until is not None and ensure_utc(user.locked_until) > utcnow())
    ):
        _auth_failure(db, request, user, normalized)
        raise HTTPException(status_code=401, detail=AUTH_ERROR)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    old_raw = request.cookies.get(get_settings().cookie_name)
    if old_raw:
        old_session = db.scalar(
            select(PortalSession).where(PortalSession.session_hash == digest_secret(old_raw))
        )
        if old_session:
            revoke_session(db, old_session)
            terminal_registry.close_for_portal_session(old_session.id)
    new_session, session_raw, csrf_raw = create_session(db, user, request)
    record_audit(
        db,
        event_type="login.success",
        actor=user.normalized_login,
        actor_role=highest_role_safe(user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_user",
        object_id=str(user.id),
        metadata={"session_rotated": True},
    )
    _record_session_created(db, request, user, new_session, reason="login")
    db.commit()
    set_session_cookies(response, session_raw, csrf_raw)
    return {
        "user": serialize_user(user).model_dump(mode="json"),
        "csrf_token": csrf_raw,
        "ssh_enrollment": ssh_enrollment_status(db, user),
    }


def highest_role_safe(user: PortalUser) -> str:
    from h100_portal_api.rbac import highest_role

    return highest_role(user)


def _record_session_created(
    db: Session,
    request: Request,
    user: PortalUser,
    session: PortalSession,
    *,
    reason: str,
) -> None:
    """Record session creation without ever persisting the raw session secret."""
    record_audit(
        db,
        event_type="session.create",
        actor=user.normalized_login,
        actor_role=highest_role_safe(user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_session",
        object_id=str(session.id),
        metadata={"reason": reason},
    )


def _set_password_action_cookie(response: Response, raw_challenge: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.password_action_cookie_name,
        raw_challenge,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/api/v1/auth",
        max_age=settings.password_action_challenge_minutes * 60,
    )


def _clear_password_action_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().password_action_cookie_name, path="/api/v1/auth")


def _password_action_error(candidate: PortalPasswordSetupToken | None) -> tuple[str, str]:
    if candidate is not None:
        if candidate.used_at is not None or candidate.state == PasswordActionTokenState.USED:
            return "PASSWORD_ACTION_USED", "此链接已经使用"
        if ensure_utc(candidate.expires_at) <= utcnow() or (
            candidate.state == PasswordActionTokenState.EXPIRED
        ):
            return "PASSWORD_ACTION_EXPIRED", "此链接已经过期"
        if candidate.revoked_at is not None or candidate.state == PasswordActionTokenState.REVOKED:
            return "PASSWORD_ACTION_REVOKED", "此链接已被新的链接替代"
    return "PASSWORD_ACTION_INVALID", "此链接无效"


def _password_action_account_is_valid(
    candidate: PortalPasswordSetupToken, user: PortalUser
) -> bool:
    if candidate.purpose == PasswordActionPurpose.INITIAL_PASSWORD_SETUP:
        return (
            user.account_state == AccountState.INVITED
            and user.password_state == PasswordState.SETUP_REQUIRED
        )
    return user.account_state == AccountState.ACTIVE and user.password_state in {
        PasswordState.SET,
        PasswordState.RESET_REQUIRED,
    }


@router.post("/password-action/exchange")
def exchange_password_action(
    body: PasswordActionExchangeRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    require_preauth_csrf(request)
    token_digest = digest_secret(body.token)
    if not rate_limiter.allowed(f"password-action-ip:{client_ip(request)}", 30, 300):
        raise HTTPException(
            status_code=429, detail={"code": "RATE_LIMITED", "message": "请求过于频繁，请稍后重试"}
        )
    candidate_ref = db.scalar(
        select(PortalPasswordSetupToken).where(PortalPasswordSetupToken.token_hash == token_digest)
    )
    user = (
        db.scalar(
            select(PortalUser).where(PortalUser.id == candidate_ref.user_id).with_for_update()
        )
        if candidate_ref is not None
        else None
    )
    candidate = (
        db.scalar(
            select(PortalPasswordSetupToken)
            .where(PortalPasswordSetupToken.id == candidate_ref.id)
            .with_for_update()
        )
        if candidate_ref is not None
        else None
    )
    if (
        candidate is None
        or candidate.state != PasswordActionTokenState.ACTIVE
        or candidate.used_at is not None
        or candidate.revoked_at is not None
        or ensure_utc(candidate.expires_at) <= utcnow()
    ):
        if (
            candidate is not None
            and candidate.state == PasswordActionTokenState.ACTIVE
            and ensure_utc(candidate.expires_at) <= utcnow()
        ):
            candidate.state = PasswordActionTokenState.EXPIRED
            candidate.challenge_hash = None
            candidate.challenge_expires_at = None
            db.commit()
        code, message = _password_action_error(candidate)
        raise HTTPException(status_code=400, detail={"code": code, "message": message})
    if user is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "PASSWORD_ACTION_INVALID", "message": "此链接无效"},
        )
    purpose = candidate.purpose
    if not _password_action_account_is_valid(candidate, user):
        candidate.state = PasswordActionTokenState.REVOKED
        candidate.revoked_at = utcnow()
        candidate.challenge_hash = None
        candidate.challenge_expires_at = None
        db.commit()
        raise HTTPException(
            status_code=400,
            detail={"code": "PASSWORD_ACTION_INVALID", "message": "此链接无效"},
        )
    now = utcnow()
    challenge = random_token(48)
    candidate.challenge_hash = digest_secret(challenge)
    candidate.challenge_expires_at = min(
        ensure_utc(candidate.expires_at),
        now + timedelta(minutes=get_settings().password_action_challenge_minutes),
    )
    candidate.exchanged_at = now
    record_audit(
        db,
        event_type="PASSWORD_ACTION_LINK_EXCHANGED",
        actor=user.normalized_login,
        actor_role="anonymous",
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="password_action_token",
        object_id=str(candidate.id),
        metadata={"purpose": purpose},
    )
    db.commit()
    _set_password_action_cookie(response, challenge)
    return {
        "status": "READY",
        "purpose": purpose,
        "username": user.login_name,
        "display_name": user.display_name,
        "expires_at": candidate.expires_at,
        "one_time": True,
    }


@router.post("/setup-password")
def setup_password(
    body: SetupPasswordRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    require_preauth_csrf(request)
    raw_challenge = request.cookies.get(get_settings().password_action_cookie_name, "")
    candidate_ref = db.scalar(
        select(PortalPasswordSetupToken).where(
            PortalPasswordSetupToken.challenge_hash == digest_secret(raw_challenge)
        )
    )
    user = (
        db.scalar(
            select(PortalUser).where(PortalUser.id == candidate_ref.user_id).with_for_update()
        )
        if candidate_ref is not None
        else None
    )
    candidate = (
        db.scalar(
            select(PortalPasswordSetupToken)
            .where(PortalPasswordSetupToken.id == candidate_ref.id)
            .with_for_update()
        )
        if candidate_ref is not None
        else None
    )
    if (
        candidate is None
        or not raw_challenge
        or candidate.state != PasswordActionTokenState.ACTIVE
        or candidate.used_at is not None
        or candidate.revoked_at is not None
        or ensure_utc(candidate.expires_at) <= utcnow()
        or candidate.challenge_expires_at is None
        or ensure_utc(candidate.challenge_expires_at) <= utcnow()
    ):
        _clear_password_action_cookie(response)
        code, message = _password_action_error(candidate)
        raise HTTPException(
            status_code=400,
            detail={"code": code, "message": message},
        )
    if user is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "PASSWORD_ACTION_INVALID", "message": "此链接无效"},
        )
    if not _password_action_account_is_valid(candidate, user):
        candidate.state = PasswordActionTokenState.REVOKED
        candidate.revoked_at = utcnow()
        candidate.challenge_hash = None
        candidate.challenge_expires_at = None
        db.commit()
        _clear_password_action_cookie(response)
        raise HTTPException(
            status_code=400,
            detail={"code": "PASSWORD_ACTION_INVALID", "message": "此链接无效"},
        )
    try:
        validate_password(body.password, user.normalized_login)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "WEAK_PASSWORD", "message": str(exc)}
        ) from exc
    now = utcnow()
    credential = db.scalar(
        select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == user.id)
    )
    if credential is None:
        credential = PortalPasswordCredential(
            user_id=user.id, password_hash=hash_password(body.password), password_changed_at=now
        )
        db.add(credential)
    else:
        credential.password_hash = hash_password(body.password)
        credential.password_changed_at = now
    candidate.used_at = now
    candidate.state = PasswordActionTokenState.USED
    candidate.challenge_hash = None
    candidate.challenge_expires_at = None
    other_active = db.scalars(
        select(PortalPasswordSetupToken).where(
            PortalPasswordSetupToken.user_id == user.id,
            PortalPasswordSetupToken.id != candidate.id,
            PortalPasswordSetupToken.state == PasswordActionTokenState.ACTIVE,
            PortalPasswordSetupToken.used_at.is_(None),
        )
    ).all()
    db.execute(
        update(PortalPasswordSetupToken)
        .where(
            PortalPasswordSetupToken.user_id == user.id,
            PortalPasswordSetupToken.id != candidate.id,
            PortalPasswordSetupToken.state == PasswordActionTokenState.ACTIVE,
            PortalPasswordSetupToken.used_at.is_(None),
        )
        .values(
            state=PasswordActionTokenState.REVOKED,
            revoked_at=now,
            challenge_hash=None,
            challenge_expires_at=None,
        )
    )
    user.password_state = PasswordState.SET
    initial_setup = candidate.purpose == PasswordActionPurpose.INITIAL_PASSWORD_SETUP
    if initial_setup:
        user.account_state = AccountState.ACTIVE
        user.activated_at = user.activated_at or now
    revoked_sessions = revoke_user_sessions(db, user.id)
    revoked_cli_tokens = revoke_user_cli_tokens(db, user.id)
    terminal_registry.close_for_user(user.id)
    new_session = None
    session_raw = None
    csrf_raw = None
    if initial_setup:
        new_session, session_raw, csrf_raw = create_session(db, user, request)
    revoked_by_purpose = {
        purpose.value: sum(1 for row in other_active if row.purpose == purpose)
        for purpose in PasswordActionPurpose
    }
    for purpose, count in revoked_by_purpose.items():
        if count:
            record_audit(
                db,
                event_type=(
                    "PASSWORD_SETUP_LINK_REVOKED"
                    if purpose == PasswordActionPurpose.INITIAL_PASSWORD_SETUP
                    else "PASSWORD_RESET_LINK_REVOKED"
                ),
                actor=user.normalized_login,
                actor_role=highest_role_safe(user),
                source_ip=client_ip(request),
                user_agent=user_agent(request),
                object_type="portal_user",
                object_id=str(user.id),
                metadata={"purpose": purpose, "revoked_count": count},
            )
    record_audit(
        db,
        event_type="PASSWORD_SETUP_COMPLETED" if initial_setup else "PASSWORD_RESET_COMPLETED",
        actor=user.normalized_login,
        actor_role=highest_role_safe(user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="password_action_token",
        object_id=str(candidate.id),
        metadata={"purpose": candidate.purpose, "single_use": True},
    )
    if initial_setup and new_session is not None:
        _record_session_created(db, request, user, new_session, reason="password_setup")
    else:
        record_audit(
            db,
            event_type="SESSION_REVOKED_AFTER_PASSWORD_RESET",
            actor=user.normalized_login,
            actor_role=highest_role_safe(user),
            source_ip=client_ip(request),
            user_agent=user_agent(request),
            object_type="portal_user",
            object_id=str(user.id),
            metadata={
                "revoked_count": revoked_sessions,
                "revoked_cli_tokens": revoked_cli_tokens,
                "requires_login": True,
            },
        )
    db.commit()
    _clear_password_action_cookie(response)
    if initial_setup and session_raw is not None and csrf_raw is not None:
        set_session_cookies(response, session_raw, csrf_raw)
    else:
        clear_session_cookies(response)
    return {
        "user": serialize_user(user).model_dump(mode="json"),
        "csrf_token": csrf_raw,
        "ssh_enrollment": ssh_enrollment_status(db, user),
        "purpose": candidate.purpose,
        "requires_login": not initial_setup,
    }


@router.get("/me")
def me(
    context: AuthContext = Depends(auth_context), db: Session = Depends(get_db)
) -> dict[str, object]:
    assert context.session is not None
    reauthenticated_at = context.session.reauthenticated_at
    recent_auth_valid_until = (
        ensure_utc(reauthenticated_at) + timedelta(minutes=get_settings().reauthentication_minutes)
        if reauthenticated_at is not None
        else None
    )
    return {
        "user": serialize_user(context.user).model_dump(mode="json"),
        "role": highest_role_safe(context.user),
        "ssh_enrollment": ssh_enrollment_status(db, context.user),
        "recent_auth_valid": bool(
            recent_auth_valid_until is not None and recent_auth_valid_until > utcnow()
        ),
        "recent_auth_valid_until": (
            recent_auth_valid_until.isoformat() if recent_auth_valid_until is not None else None
        ),
    }


def _cli_token_view(token: PortalCliToken) -> dict[str, object]:
    now = utcnow()
    if token.revoked_at is not None:
        state = "REVOKED"
    elif token.expires_at is not None and ensure_utc(token.expires_at) <= now:
        state = "EXPIRED"
    else:
        state = "ACTIVE"
    return {
        "id": str(token.id),
        "label": token.label,
        "state": state,
        "created_at": ensure_utc(token.created_at).isoformat(),
        "expires_at": ensure_utc(token.expires_at).isoformat() if token.expires_at else None,
        "last_used_at": (
            ensure_utc(token.last_used_at).isoformat() if token.last_used_at else None
        ),
        "revoked_at": ensure_utc(token.revoked_at).isoformat() if token.revoked_at else None,
        "scopes": ["self.jobs.submit", "self.jobs.read", "self.jobs.cancel"],
        "owner_bound": True,
    }


def _require_ordinary_cli_token_owner(context: AuthContext) -> None:
    if role_names(context.user) != ["user"]:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CLI_TOKEN_ORDINARY_USER_ONLY",
                "message": "CLI Token仅供普通用户使用",
            },
        )


@router.get("/cli-tokens")
def cli_tokens(
    db: Session = Depends(get_db), context: AuthContext = Depends(auth_context)
) -> dict[str, object]:
    _require_ordinary_cli_token_owner(context)
    rows = db.scalars(
        select(PortalCliToken)
        .where(PortalCliToken.user_id == context.user.id)
        .order_by(PortalCliToken.created_at.desc())
        .limit(50)
    ).all()
    return {"status": "OK", "tokens": [_cli_token_view(row) for row in rows], "count": len(rows)}


@router.post("/cli-tokens", status_code=status.HTTP_201_CREATED)
def create_cli_token(
    body: CliTokenCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(auth_context),
) -> dict[str, object]:
    require_session_csrf(request, context)
    require_recent_reauthentication(context)
    _require_ordinary_cli_token_owner(context)
    if not rate_limiter.allowed(f"cli-token-create:{context.user.id}", 10, 300):
        raise HTTPException(
            status_code=429,
            detail={"code": "RATE_LIMITED", "message": "CLI Token创建过于频繁"},
        )
    db.scalar(select(PortalUser).where(PortalUser.id == context.user.id).with_for_update())
    rows = db.scalars(
        select(PortalCliToken).where(
            PortalCliToken.user_id == context.user.id,
            PortalCliToken.revoked_at.is_(None),
        )
    ).all()
    now = utcnow()
    active_count = sum(
        1 for row in rows if row.expires_at is None or ensure_utc(row.expires_at) > now
    )
    if active_count >= 10:
        raise HTTPException(
            status_code=409,
            detail={"code": "CLI_TOKEN_LIMIT_REACHED", "message": "最多保留10个有效CLI Token"},
        )
    raw_token = f"h100_cli_{random_token(48)}"
    token = PortalCliToken(
        user_id=context.user.id,
        token_hash=digest_secret(raw_token),
        label=body.label,
        created_at=now,
        expires_at=(
            now + timedelta(days=body.expires_in_days) if body.expires_in_days is not None else None
        ),
    )
    db.add(token)
    db.flush()
    record_audit(
        db,
        event_type="CLI_TOKEN_CREATED",
        actor=context.user.normalized_login,
        actor_role="user",
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="cli_token",
        object_id=str(token.id),
        metadata={
            "label": token.label,
            "expires_at": token.expires_at.isoformat() if token.expires_at else None,
            "ordinary_user_only": True,
        },
    )
    db.commit()
    return {"status": "CREATED", "token": raw_token, "credential": _cli_token_view(token)}


@router.delete("/cli-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_cli_token(
    token_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(auth_context),
) -> None:
    require_session_csrf(request, context)
    _require_ordinary_cli_token_owner(context)
    token = db.scalar(
        select(PortalCliToken)
        .where(PortalCliToken.id == token_id, PortalCliToken.user_id == context.user.id)
        .with_for_update()
    )
    if token is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "CLI_TOKEN_NOT_FOUND", "message": "CLI Token不存在"},
        )
    token.revoked_at = token.revoked_at or utcnow()
    record_audit(
        db,
        event_type="CLI_TOKEN_REVOKED",
        actor=context.user.normalized_login,
        actor_role="user",
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="cli_token",
        object_id=str(token.id),
        metadata={"label": token.label},
    )
    db.commit()


@router.get("/sessions")
def sessions(
    request: Request, db: Session = Depends(get_db), context: AuthContext = Depends(auth_context)
) -> list[dict[str, object]]:
    rows = db.scalars(
        select(PortalSession)
        .where(PortalSession.user_id == context.user.id, PortalSession.revoked_at.is_(None))
        .order_by(PortalSession.created_at.desc())
    )
    raw_hash = digest_secret(request.cookies.get(get_settings().cookie_name, ""))
    return [
        session_response(row, row.session_hash == raw_hash).model_dump(mode="json") for row in rows
    ]


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(auth_context),
) -> None:
    assert context.session is not None
    require_session_csrf(request, context)
    revoke_session(db, context.session)
    terminal_registry.close_for_portal_session(context.session.id)
    record_audit(
        db,
        event_type="logout",
        actor=context.user.normalized_login,
        actor_role=highest_role_safe(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_user",
        object_id=str(context.user.id),
    )
    db.commit()
    clear_session_cookies(response)


@router.post("/reauthenticate")
def reauthenticate(
    body: ReauthenticateRequest,
    request: Request,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(auth_context),
) -> dict[str, bool]:
    assert context.session is not None
    require_session_csrf(request, context)
    credential = db.scalar(
        select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == context.user.id)
    )
    if credential is None or not verify_password(credential.password_hash, body.password):
        record_audit(
            db,
            event_type="reauthentication.failure",
            actor=context.user.normalized_login,
            actor_role=highest_role_safe(context.user),
            source_ip=client_ip(request),
            user_agent=user_agent(request),
            object_type="portal_user",
            object_id=str(context.user.id),
            result="DENIED",
        )
        db.commit()
        raise HTTPException(status_code=401, detail=AUTH_ERROR)
    context.session.reauthenticated_at = utcnow()
    record_audit(
        db,
        event_type="reauthentication.success",
        actor=context.user.normalized_login,
        actor_role=highest_role_safe(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_user",
        object_id=str(context.user.id),
    )
    db.commit()
    return {"reauthenticated": True}


@router.post("/password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(auth_context),
) -> dict[str, bool]:
    require_session_csrf(request, context)
    credential = db.scalar(
        select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == context.user.id)
    )
    if credential is None or not verify_password(credential.password_hash, body.current_password):
        raise HTTPException(status_code=401, detail=AUTH_ERROR)
    try:
        validate_password(body.new_password, context.user.normalized_login)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "WEAK_PASSWORD", "message": str(exc)}
        ) from exc
    credential.password_hash = hash_password(body.new_password)
    credential.password_changed_at = utcnow()
    context.user.password_state = PasswordState.SET
    # Revoke every pre-change session, including the current one, then issue a
    # fresh session in the same transaction. Keeping the old current session
    # alive would not be session rotation.
    revoke_user_sessions(db, context.user.id)
    revoked_cli_tokens = revoke_user_cli_tokens(db, context.user.id)
    terminal_registry.close_for_user(context.user.id)
    new_session, session_raw, csrf_raw = create_session(db, context.user, request)
    new_session.reauthenticated_at = utcnow()
    record_audit(
        db,
        event_type="password.change",
        actor=context.user.normalized_login,
        actor_role=highest_role_safe(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_user",
        object_id=str(context.user.id),
        metadata={
            "all_previous_sessions_revoked": True,
            "all_cli_tokens_revoked": True,
            "revoked_cli_tokens": revoked_cli_tokens,
            "session_rotated": True,
        },
    )
    _record_session_created(db, request, context.user, new_session, reason="password_change")
    db.commit()
    set_session_cookies(response, session_raw, csrf_raw)
    return {"changed": True}


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_other_session(
    session_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(auth_context),
) -> None:
    assert context.session is not None
    require_session_csrf(request, context)
    try:
        import uuid

        target_id = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "SESSION_NOT_FOUND", "message": "会话不存在"}
        ) from exc
    target = db.scalar(
        select(PortalSession).where(
            PortalSession.id == target_id,
            PortalSession.user_id == context.user.id,
            PortalSession.revoked_at.is_(None),
        )
    )
    if target is None:
        raise HTTPException(
            status_code=404, detail={"code": "SESSION_NOT_FOUND", "message": "会话不存在"}
        )
    if target.id == context.session.id:
        revoke_session(db, target)
        clear_session_cookies(response)
    else:
        revoke_session(db, target)
    terminal_registry.close_for_portal_session(target.id)
    record_audit(
        db,
        event_type="session.revoke",
        actor=context.user.normalized_login,
        actor_role=highest_role_safe(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_session",
        object_id=str(target.id),
    )
    db.commit()


@router.post("/sessions/revoke-others")
def revoke_other_sessions(
    request: Request,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(auth_context),
) -> dict[str, int]:
    assert context.session is not None
    require_session_csrf(request, context)
    revoked = revoke_user_sessions(db, context.user.id, except_id=context.session.id)
    terminal_registry.close_for_user(context.user.id, except_session_id=context.session.id)
    record_audit(
        db,
        event_type="session.revoke_others",
        actor=context.user.normalized_login,
        actor_role=highest_role_safe(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_user",
        object_id=str(context.user.id),
        metadata={"revoked_count": revoked},
    )
    db.commit()
    return {"revoked": revoked}
