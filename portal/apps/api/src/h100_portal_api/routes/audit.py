from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.auth import (
    AuthContext,
    client_ip,
    require_session_csrf,
    user_agent,
)
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.models import PortalAuditEvent
from h100_portal_api.rbac import highest_role
from h100_portal_api.schemas import PageAccessRequest

router = APIRouter(prefix="/audit", tags=["audit"])


@router.post("/page-access", status_code=204)
def page_access(
    body: PageAccessRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("platform.read")),
    db: Session = Depends(get_db),
) -> None:
    require_session_csrf(request, context)
    # The schema only accepts a bounded portal-relative path. Query values are
    # intentionally not recorded so setup tokens and other secrets cannot enter audit.
    path = body.path.split("?", 1)[0]
    record_audit(
        db,
        event_type="page.access",
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_page",
        object_id=path,
    )
    db.commit()


@router.get("")
def audit(
    context: AuthContext = Depends(permission_dependency("audit.read")),
    db: Session = Depends(get_db),
    event_type: str | None = Query(default=None, max_length=64),
    actor: str | None = Query(default=None, max_length=64),
    result: str | None = Query(default=None, max_length=32),
) -> dict[str, Any]:
    statement = select(PortalAuditEvent).order_by(PortalAuditEvent.timestamp.desc()).limit(200)
    if event_type:
        statement = statement.where(PortalAuditEvent.event_type == event_type)
    if actor:
        statement = statement.where(PortalAuditEvent.actor == actor)
    if result:
        statement = statement.where(PortalAuditEvent.result == result)
    events = db.scalars(statement).all()
    return {
        "status": "OK",
        "events": [
            {
                "event_id": str(event.event_id),
                "event_type": event.event_type,
                "actor": event.actor,
                "actor_role": event.actor_role,
                "source_ip": event.source_ip,
                "object_type": event.object_type,
                "object_id": event.object_id,
                "operation_id": str(event.operation_id) if event.operation_id else None,
                "result": event.result,
                "timestamp": event.timestamp.isoformat(),
                "safe_metadata": event.safe_metadata,
            }
            for event in events
        ],
    }
