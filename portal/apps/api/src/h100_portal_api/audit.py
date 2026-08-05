import uuid
from typing import Any

from sqlalchemy.orm import Session

from h100_portal_api.models import PortalAuditEvent, utcnow
from h100_portal_api.security import digest_user_agent, safe_metadata


def record_audit(
    db: Session,
    *,
    event_type: str,
    actor: str,
    actor_role: str,
    source_ip: str,
    user_agent: str,
    object_type: str = "platform",
    object_id: str = "platform",
    result: str = "SUCCESS",
    metadata: dict[str, Any] | None = None,
    operation_id: uuid.UUID | None = None,
) -> PortalAuditEvent:
    event = PortalAuditEvent(
        event_type=event_type[:64],
        actor=actor[:64],
        actor_role=actor_role[:32],
        source_ip=source_ip[:64],
        user_agent_digest=digest_user_agent(user_agent),
        object_type=object_type[:64],
        object_id=object_id[:128],
        operation_id=operation_id,
        result=result[:32],
        timestamp=utcnow(),
        safe_metadata=safe_metadata(metadata or {}),
    )
    db.add(event)
    return event
