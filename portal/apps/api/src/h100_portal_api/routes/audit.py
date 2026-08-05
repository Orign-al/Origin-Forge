from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from h100_portal_api.auth import AuthContext
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.models import PortalAuditEvent

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
def audit(
    context: AuthContext = Depends(permission_dependency("audit.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    events = db.scalars(
        select(PortalAuditEvent).order_by(PortalAuditEvent.timestamp.desc()).limit(200)
    ).all()
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
