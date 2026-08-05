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
    require_session_csrf,
    revoke_session,
    revoke_user_sessions,
    serialize_user,
    session_response,
    set_session_cookies,
    user_agent,
)
from h100_portal_api.config import get_settings
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import auth_context
from h100_portal_api.enums import AccountState, PasswordState
from h100_portal_api.models import (
    PortalPasswordCredential,
    PortalPasswordSetupToken,
    PortalSession,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    ReauthenticateRequest,
    SetupPasswordRequest,
)
from h100_portal_api.security import (
    digest_secret,
    hash_password,
    normalize_login,
    validate_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _auth_failure(db: Session, request: Request, user: PortalUser | None, normalized: str) -> None:
    settings = get_settings()
    if user is not None:
        user.failed_login_count += 1
        if user.failed_login_count >= settings.login_failures_before_lock:
            user.locked_until = utcnow() + timedelta(minutes=settings.login_lock_minutes)
            user.account_state = AccountState.LOCKED
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
        or user.password_state != PasswordState.SET
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
    _session, session_raw, csrf_raw = create_session(db, user, request)
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
    db.commit()
    set_session_cookies(response, session_raw, csrf_raw)
    return {"user": serialize_user(user).model_dump(mode="json"), "csrf_token": csrf_raw}


def highest_role_safe(user: PortalUser) -> str:
    from h100_portal_api.rbac import highest_role

    return highest_role(user)


@router.get("/setup-status")
def setup_status(token: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    if len(token) > 256:
        return {"valid": False}
    candidate = db.scalar(
        select(PortalPasswordSetupToken).where(
            PortalPasswordSetupToken.token_hash == digest_secret(token)
        )
    )
    valid = bool(
        candidate
        and candidate.used_at is None
        and candidate.revoked_at is None
        and ensure_utc(candidate.expires_at) > utcnow()
    )
    return {"valid": valid}


@router.post("/setup-password")
def setup_password(
    body: SetupPasswordRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    require_preauth_csrf(request)
    candidate = db.scalar(
        select(PortalPasswordSetupToken).where(
            PortalPasswordSetupToken.token_hash == digest_secret(body.token)
        )
    )
    if (
        candidate is None
        or candidate.used_at is not None
        or candidate.revoked_at is not None
        or ensure_utc(candidate.expires_at) <= utcnow()
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "SETUP_TOKEN_INVALID", "message": "设置链接无效或已过期"},
        )
    user = db.get(PortalUser, candidate.user_id)
    if user is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "SETUP_TOKEN_INVALID", "message": "设置链接无效或已过期"},
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
    db.execute(
        update(PortalPasswordSetupToken)
        .where(
            PortalPasswordSetupToken.user_id == user.id,
            PortalPasswordSetupToken.id != candidate.id,
            PortalPasswordSetupToken.used_at.is_(None),
        )
        .values(revoked_at=now)
    )
    user.password_state = PasswordState.SET
    user.account_state = AccountState.ACTIVE
    user.activated_at = user.activated_at or now
    revoke_user_sessions(db, user.id)
    _session, session_raw, csrf_raw = create_session(db, user, request)
    record_audit(
        db,
        event_type="password.setup",
        actor=user.normalized_login,
        actor_role=highest_role_safe(user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_user",
        object_id=str(user.id),
        metadata={"one_time_token": True},
    )
    db.commit()
    set_session_cookies(response, session_raw, csrf_raw)
    return {"user": serialize_user(user).model_dump(mode="json"), "csrf_token": csrf_raw}


@router.get("/me")
def me(context: AuthContext = Depends(auth_context)) -> dict[str, object]:
    return {
        "user": serialize_user(context.user).model_dump(mode="json"),
        "role": highest_role_safe(context.user),
    }


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
    require_session_csrf(request, context)
    revoke_session(db, context.session)
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
    revoke_user_sessions(db, context.user.id, except_id=context.session.id)
    context.session.reauthenticated_at = utcnow()
    record_audit(
        db,
        event_type="password.change",
        actor=context.user.normalized_login,
        actor_role=highest_role_safe(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_user",
        object_id=str(context.user.id),
    )
    db.commit()
    clear_session_cookies(response)
    # Password changes rotate the current session as well.
    _new_session, raw, csrf_raw = create_session(db, context.user, request)
    db.commit()
    set_session_cookies(response, raw, csrf_raw)
    return {"changed": True}


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_other_session(
    session_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(auth_context),
) -> None:
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
    require_session_csrf(request, context)
    revoked = revoke_user_sessions(db, context.user.id, except_id=context.session.id)
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
