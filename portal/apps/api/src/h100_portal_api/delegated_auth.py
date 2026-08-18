from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.enums import AccountState
from h100_portal_api.models import (
    PortalDelegatedTestSession,
    PortalManagedUser,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.rbac import highest_role, role_names
from h100_portal_api.security import digest_secret, random_token

DELEGATED_CREDENTIAL_PREFIX = "h100dts_"
DELEGATED_TEST_MIN_TTL_SECONDS = 600
DELEGATED_TEST_MAX_TTL_SECONDS = 900
DELEGATED_TEST_SCOPES = frozenset(
    {
        "self.jobs.submit",
        "self.jobs.read",
        "self.jobs.logs.read",
        "self.jobs.cancel",
        "self.container.read",
    }
)

# A delegated credential never inherits the actor's platform permissions.  A
# request must map to one of these explicit effective-user scopes and must also
# pass the effective user's ordinary RBAC permission check.
DELEGATED_PERMISSION_SCOPES = {
    "self.jobs.submit": "self.jobs.submit",
    "self.jobs.read": "self.jobs.read",
    "self.jobs.cancel": "self.jobs.cancel",
    "self.container.read": "self.container.read",
    "self.environment.read": "self.container.read",
}


class DelegatedTestAuthError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class IssuedDelegatedTestSession:
    session: PortalDelegatedTestSession
    credential: str


def issue_delegated_test_session(
    db: Session,
    *,
    actor: PortalUser,
    effective_user: PortalUser,
    scopes: Iterable[str],
    ttl_seconds: int,
    source_ip: str,
    user_agent: str,
    now: datetime | None = None,
) -> IssuedDelegatedTestSession:
    trusted_now = ensure_utc(now or utcnow())
    normalized_scopes = sorted(set(scopes))
    if actor.account_state != AccountState.ACTIVE or highest_role(actor) != "platform_owner":
        raise DelegatedTestAuthError(
            "DELEGATED_TEST_ACTOR_DENIED",
            "only an active platform_owner may create a delegated test session",
        )
    if actor.id == effective_user.id:
        raise DelegatedTestAuthError(
            "DELEGATED_TEST_TARGET_DENIED", "actor and effective user must be distinct"
        )
    if (
        effective_user.account_state != AccountState.ACTIVE
        or role_names(effective_user) != ["user"]
        or db.scalar(
            select(PortalManagedUser.id).where(
                PortalManagedUser.portal_user_id == effective_user.id
            )
        )
        is None
    ):
        raise DelegatedTestAuthError(
            "DELEGATED_TEST_TARGET_DENIED",
            "effective user must be an active ordinary managed user",
        )
    if not normalized_scopes or not set(normalized_scopes).issubset(DELEGATED_TEST_SCOPES):
        raise DelegatedTestAuthError(
            "DELEGATED_TEST_SCOPE_DENIED", "delegated test scope is not allowed"
        )
    if not DELEGATED_TEST_MIN_TTL_SECONDS <= ttl_seconds <= DELEGATED_TEST_MAX_TTL_SECONDS:
        raise DelegatedTestAuthError(
            "DELEGATED_TEST_TTL_DENIED", "delegated test TTL must be between 600 and 900 seconds"
        )

    credential = f"{DELEGATED_CREDENTIAL_PREFIX}{random_token(48)}"
    delegated = PortalDelegatedTestSession(
        actor_user_id=actor.id,
        effective_user_id=effective_user.id,
        token_hash=digest_secret(credential),
        scopes=normalized_scopes,
        ttl_seconds=ttl_seconds,
        created_at=trusted_now,
        expires_at=trusted_now + timedelta(seconds=ttl_seconds),
    )
    db.add(delegated)
    db.flush()
    record_audit(
        db,
        event_type="DELEGATED_TEST_SESSION_CREATED",
        actor=actor.normalized_login,
        actor_role="platform_owner",
        source_ip=source_ip,
        user_agent=user_agent,
        object_type="delegated_test_session",
        object_id=str(delegated.id),
        metadata={
            "actor_user": actor.normalized_login,
            "effective_user": effective_user.normalized_login,
            "scopes": normalized_scopes,
            "created_at": trusted_now.isoformat(),
            "expires_at": ensure_utc(delegated.expires_at).isoformat(),
            "ttl_seconds": ttl_seconds,
        },
    )
    return IssuedDelegatedTestSession(session=delegated, credential=credential)
