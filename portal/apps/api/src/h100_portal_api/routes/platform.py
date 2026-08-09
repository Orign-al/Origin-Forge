from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from h100_portal_api.auth import AuthContext
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalManagedUser,
    PortalOperation,
    PortalSetting,
    PortalSystemSnapshot,
    PortalUser,
    utcnow,
)
from h100_portal_api.worker_client import WorkerClientError, call_worker

router = APIRouter(prefix="/platform", tags=["platform"])


def production_pilot_state(db: Session) -> dict[str, Any]:
    setting = db.get(PortalSetting, "production_pilot")
    if setting is None or not isinstance(setting.value, dict):
        return {
            "status": "OK",
            "state": "NOT_STARTED",
            "mode": "SINGLE_NODE",
            "managed_users": 1,
            "active_managed_user": "origin-pilot",
            "node_name": "sagsh100server",
            "node_state": "DRAIN",
            "scheduler": "UNAVAILABLE",
            "queue": "EMPTY",
            "gpu_capacity": 4,
            "per_user_max_gpu": 1,
        }
    return {"status": "OK", **setting.value, "updated_at": setting.updated_at.isoformat()}


def adapter(
    operation: str, context: AuthContext, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        value = call_worker(operation, payload=payload, requested_by=context.user.normalized_login)
        if value.get("status") not in {"OK", "PARTIAL", "DRY_RUN"}:
            return {"status": "UNKNOWN", "error": value.get("error", {"code": "ADAPTER_FAILED"})}
        return value
    except WorkerClientError as exc:
        return {"status": "UNKNOWN", "error": {"code": exc.code, "message": str(exc)}}


@router.get("/overview")
def overview(
    context: AuthContext = Depends(permission_dependency("platform.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    value = adapter("platform.health.read", context)
    extras = {
        "containers": adapter("containers.list", context),
        "storage": adapter("storage.summary.read", context),
        "registries": adapter("registry.status.read", context),
        "alerts": adapter("monitoring.alerts.read", context),
        "monitoring": adapter("monitoring.summary.read", context),
        "gpu_health": adapter("gpu.health.read", context),
        "ssh_policy": adapter("ssh.policy.read", context),
    }
    portal_states: dict[str, int] = {
        str(state): count
        for state, count in db.execute(
            select(PortalUser.account_state, func.count()).group_by(PortalUser.account_state)
        ).tuples()
    }
    onboarding_states: dict[str, int] = {
        str(state): count
        for state, count in db.execute(
            select(PortalManagedUser.onboarding_state, func.count()).group_by(
                PortalManagedUser.onboarding_state
            )
        ).tuples()
    }
    operation_states: dict[str, int] = {
        str(state): count
        for state, count in db.execute(
            select(PortalOperation.status, func.count()).group_by(PortalOperation.status)
        ).tuples()
    }
    identity = {
        "status": "OK",
        "portal_users": sum(portal_states.values()),
        "managed_linux_users": sum(onboarding_states.values()),
        "account_states": portal_states,
        "onboarding_states": onboarding_states,
    }
    tasks = {
        "status": "OK",
        "pending_approval": operation_states.get("PENDING_APPROVAL", 0),
        "failed": operation_states.get("FAILED", 0),
        "total": sum(operation_states.values()),
    }
    snapshot: dict[str, Any] = {
        "platform": value,
        **extras,
        "identity": identity,
        "tasks": tasks,
        "production_pilot": production_pilot_state(db),
    }
    recent_audit = db.scalars(
        select(PortalAuditEvent).order_by(PortalAuditEvent.timestamp.desc()).limit(12)
    ).all()
    snapshot["recent_audit"] = [
        {
            "event_type": event.event_type,
            "actor": event.actor,
            "result": event.result,
            "timestamp": event.timestamp.isoformat(),
            "object_type": event.object_type,
            "object_id": event.object_id,
        }
        for event in recent_audit
    ]
    data_sections = [
        item for item in snapshot.values() if isinstance(item, dict) and "status" in item
    ]
    overall = (
        "OK"
        if all(item.get("status") in {"OK", "PARTIAL"} for item in data_sections)
        else "PARTIAL"
    )
    db.add(
        PortalSystemSnapshot(
            snapshot_type="overview", status=overall, payload=snapshot, captured_at=utcnow()
        )
    )
    db.commit()
    return {"status": overall, **snapshot}


@router.get("/gpus")
def gpus(context: AuthContext = Depends(permission_dependency("gpu.read"))) -> dict[str, Any]:
    result = adapter("gpu.list", context)
    health = adapter("gpu.health.read", context)
    per_gpu = {
        str(item.get("index")): item.get("dcgm_status", "UNKNOWN")
        for item in health.get("per_gpu", [])
        if isinstance(item, dict)
    }
    if isinstance(result.get("gpus"), list):
        for item in result["gpus"]:
            if isinstance(item, dict):
                item["dcgm_status"] = per_gpu.get(str(item.get("index")), "UNKNOWN")
                item["xid_aer_status"] = health.get("kernel_errors", {}).get("status", "UNKNOWN")
    result["health"] = health
    return result


@router.get("/gpu-health")
def gpu_health(context: AuthContext = Depends(permission_dependency("gpu.read"))) -> dict[str, Any]:
    return adapter("gpu.health.read", context)


@router.get("/systemd-failed")
def systemd_failed(
    context: AuthContext = Depends(permission_dependency("platform.read")),
) -> dict[str, Any]:
    return adapter("systemd.failed.read", context)


@router.get("/isolation")
def isolation(
    context: AuthContext = Depends(permission_dependency("platform.read")),
) -> dict[str, Any]:
    return adapter("gpu_isolation.status.read", context)


@router.get("/storage")
def storage(
    context: AuthContext = Depends(permission_dependency("storage.read")),
) -> dict[str, Any]:
    return adapter("storage.summary.read", context)


@router.get("/quotas")
def quotas(context: AuthContext = Depends(permission_dependency("storage.read"))) -> dict[str, Any]:
    return adapter("quotas.list", context)


@router.get("/registries")
def registries(
    context: AuthContext = Depends(permission_dependency("images.read")),
) -> dict[str, Any]:
    return adapter("registry.status.read", context)


@router.get("/alerts")
def alerts(
    context: AuthContext = Depends(permission_dependency("monitoring.read")),
) -> dict[str, Any]:
    return adapter("monitoring.alerts.read", context)


@router.get("/monitoring")
def monitoring(
    context: AuthContext = Depends(permission_dependency("monitoring.read")),
) -> dict[str, Any]:
    return adapter("monitoring.summary.read", context)
