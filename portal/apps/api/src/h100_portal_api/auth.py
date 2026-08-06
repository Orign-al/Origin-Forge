import secrets
import threading
from collections import defaultdict, deque
from datetime import timedelta
from typing import NamedTuple

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from h100_portal_api.config import get_settings
from h100_portal_api.models import (
    PortalPasswordSetupToken,
    PortalSession,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.schemas import RoleResponse, SessionResponse, UserResponse
from h100_portal_api.security import (
    digest_secret,
    digest_user_agent,
    random_token,
    signed_csrf_token,
    validate_signed_csrf,
)

AUTH_ERROR = {"code": "AUTHENTICATION_FAILED", "message": "用户名或密码不正确"}


class AuthContext(NamedTuple):
    user: PortalUser
    session: PortalSession
    session_raw: str


class SlidingRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allowed(self, key: str, limit: int, window_seconds: int) -> bool:
        now = __import__("time").monotonic()
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - window_seconds:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            if len(self._events) > 4096:
                for stale_key in list(self._events)[:512]:
                    if not self._events[stale_key]:
                        del self._events[stale_key]
            return True


rate_limiter = SlidingRateLimiter()


def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "unknown")[:64]


def user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "unknown")[:512]


def ensure_allowed_origin(request: Request) -> None:
    settings = get_settings()
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin:
        if origin not in settings.allowed_origins:
            raise HTTPException(
                status_code=403, detail={"code": "ORIGIN_REJECTED", "message": "请求来源被拒绝"}
            )
        return
    if referer and any(referer.startswith(f"{allowed}/") for allowed in settings.allowed_origins):
        return
    raise HTTPException(
        status_code=403, detail={"code": "ORIGIN_REQUIRED", "message": "缺少受信请求来源"}
    )


def require_preauth_csrf(request: Request) -> None:
    ensure_allowed_origin(request)
    cookie = request.cookies.get(get_settings().csrf_cookie_name)
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise HTTPException(
            status_code=403, detail={"code": "CSRF_REJECTED", "message": "CSRF 校验失败"}
        )
    if not validate_signed_csrf(header):
        raise HTTPException(
            status_code=403, detail={"code": "CSRF_REJECTED", "message": "CSRF 校验失败"}
        )


def require_session_csrf(request: Request, context: AuthContext) -> None:
    ensure_allowed_origin(request)
    cookie = request.cookies.get(get_settings().csrf_cookie_name)
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not secrets.compare_digest(cookie, header):
        raise HTTPException(
            status_code=403, detail={"code": "CSRF_REJECTED", "message": "CSRF 校验失败"}
        )
    if not secrets.compare_digest(digest_secret(header), context.session.csrf_hash):
        raise HTTPException(
            status_code=403, detail={"code": "CSRF_REJECTED", "message": "CSRF 校验失败"}
        )


def set_session_cookies(response: Response, session_raw: str, csrf_raw: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.cookie_name,
        session_raw,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_raw,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(settings.cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")


def serialize_user(user: PortalUser) -> UserResponse:
    return UserResponse(
        id=user.id,
        login_name=user.login_name,
        normalized_login=user.normalized_login,
        display_name=user.display_name,
        unix_username=user.unix_username,
        account_state=user.account_state,
        password_state=user.password_state,
        resource_onboarding_state=user.resource_onboarding_state,
        created_at=user.created_at,
        activated_at=user.activated_at,
        last_login_at=user.last_login_at,
        roles=[RoleResponse(name=role.name, description=role.description) for role in user.roles],
    )


def create_session(
    db: Session, user: PortalUser, request: Request
) -> tuple[PortalSession, str, str]:
    settings = get_settings()
    session_raw = random_token(48)
    csrf_raw = signed_csrf_token()
    now = utcnow()
    session = PortalSession(
        user_id=user.id,
        session_hash=digest_secret(session_raw),
        csrf_hash=digest_secret(csrf_raw),
        source_ip=client_ip(request),
        user_agent_digest=digest_user_agent(user_agent(request)),
        created_at=now,
        last_seen_at=now,
        idle_expires_at=now + timedelta(minutes=settings.session_idle_minutes),
        absolute_expires_at=now + timedelta(hours=settings.session_absolute_hours),
    )
    db.add(session)
    db.flush()
    return session, session_raw, csrf_raw


def session_response(session: PortalSession, current: bool) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        created_at=session.created_at,
        last_seen_at=session.last_seen_at,
        idle_expires_at=session.idle_expires_at,
        absolute_expires_at=session.absolute_expires_at,
        current=current,
        source_ip=session.source_ip,
    )


def load_context(db: Session, request: Request) -> AuthContext:
    raw = request.cookies.get(get_settings().cookie_name)
    if not raw or len(raw) > 256:
        raise HTTPException(
            status_code=401, detail={"code": "AUTH_REQUIRED", "message": "请先登录"}
        )
    session = db.scalar(
        select(PortalSession).where(
            PortalSession.session_hash == digest_secret(raw), PortalSession.revoked_at.is_(None)
        )
    )
    now = utcnow()
    if (
        session is None
        or ensure_utc(session.idle_expires_at) <= now
        or ensure_utc(session.absolute_expires_at) <= now
    ):
        if session is not None:
            session.revoked_at = now
            db.commit()
        raise HTTPException(
            status_code=401, detail={"code": "AUTH_REQUIRED", "message": "请先登录"}
        )
    user = db.get(PortalUser, session.user_id)
    if user is None or user.account_state not in {"ACTIVE"}:
        session.revoked_at = now
        db.commit()
        raise HTTPException(
            status_code=401, detail={"code": "AUTH_REQUIRED", "message": "请先登录"}
        )
    session.last_seen_at = now
    session.idle_expires_at = now + timedelta(minutes=get_settings().session_idle_minutes)
    db.commit()
    return AuthContext(user=user, session=session, session_raw=raw)


def require_recent_reauthentication(context: AuthContext) -> None:
    timestamp = context.session.reauthenticated_at
    if (
        timestamp is None
        or (utcnow() - ensure_utc(timestamp)).total_seconds()
        > get_settings().reauthentication_minutes * 60
    ):
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={"code": "REAUTH_REQUIRED", "message": "高风险操作需要最近重新认证"},
        )


def revoke_user_sessions(db: Session, user_id: object, except_id: object | None = None) -> int:
    statement = update(PortalSession).where(
        PortalSession.user_id == user_id, PortalSession.revoked_at.is_(None)
    )
    if except_id is not None:
        statement = statement.where(PortalSession.id != except_id)
    result = db.execute(statement.values(revoked_at=utcnow()))
    return int(getattr(result, "rowcount", 0) or 0)


def revoke_session(db: Session, session: PortalSession) -> None:
    session.revoked_at = utcnow()


def remove_expired_setup_tokens(db: Session) -> None:
    db.execute(
        delete(PortalPasswordSetupToken).where(
            PortalPasswordSetupToken.expires_at < utcnow(),
            PortalPasswordSetupToken.used_at.is_not(None),
        )
    )
