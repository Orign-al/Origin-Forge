"""Emit a secret-free Portal-2 acceptance fixture from production read adapters.

Run as root on the H100 host and redirect stdout to a root-controlled report
file. The script reads the Portal environment only to connect to PostgreSQL;
database credentials and all authentication secrets are excluded from output.
"""

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

PORTAL_ENV = Path("/etc/h100-portal/portal.env")


def load_environment() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("snapshot capture must run as root")
    for raw_line in PORTAL_ENV.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def resource_view(user: Any, resource: Any | None) -> dict[str, Any]:
    if resource is None:
        return {
            "unix_username": user.unix_username,
            "onboarding_state": str(user.resource_onboarding_state),
            "gpu_isolation_state": "NOT_APPLIED",
            "container_state": "NOT_CREATED",
        }
    return {
        "unix_username": resource.unix_username,
        "uid": resource.uid,
        "gid": resource.gid,
        "shell": resource.shell,
        "host_access_state": resource.host_access_state,
        "gpu_isolation_state": resource.gpu_isolation_state,
        "onboarding_state": str(resource.onboarding_state),
        "slurm_account": resource.slurm_account,
        "slurm_qos": resource.slurm_qos,
        "project_id": resource.project_id,
        "quota_bytes": resource.quota_bytes,
        "container_name": resource.container_name,
        "container_port": resource.container_port,
    }


def main() -> int:
    load_environment()

    from h100_portal_api.auth import serialize_user
    from h100_portal_api.database import SessionLocal
    from h100_portal_api.models import (
        PortalAuditEvent,
        PortalManagedUser,
        PortalOperation,
        PortalUser,
        utcnow,
    )
    from h100_portal_api.rbac import highest_role
    from h100_portal_api.routes.operations import operation_response
    from h100_portal_api.routes.users import compute_plan_view
    from h100_portal_worker import handlers
    from sqlalchemy import select

    node = handlers.slurm_node()
    current_jobs = handlers.slurm_jobs()
    history = handlers.slurm_history()
    accounts = handlers.slurm_accounts()
    gpu = handlers.gpu_list()
    gpu_health = handlers.gpu_health()
    dcgm = {
        str(item.get("index")): item.get("dcgm_status", "UNKNOWN")
        for item in gpu_health.get("per_gpu", [])
        if isinstance(item, dict)
    }
    for item in gpu.get("gpus", []):
        if isinstance(item, dict):
            item["dcgm_status"] = dcgm.get(str(item.get("index")), "UNKNOWN")
            item["xid_aer_status"] = gpu_health.get("kernel_errors", {}).get("status", "UNKNOWN")
    containers = handlers.containers_list()
    storage = handlers.storage_summary()
    quotas = handlers.quotas_list()
    registries = handlers.registry_status()
    images = handlers.images_list()
    alerts = handlers.monitoring_alerts()
    monitoring = handlers.monitoring_summary()
    systemd = handlers.systemd_failed()
    isolation = handlers.gpu_isolation_status()
    platform = {
        "status": "OK"
        if all(
            item.get("status") in {"OK", "PARTIAL"}
            for item in (node, current_jobs, gpu, systemd, isolation)
        )
        else "PARTIAL",
        "node": node,
        "jobs": current_jobs,
        "gpu": gpu,
        "systemd": systemd,
        "gpu_isolation": isolation,
    }

    with SessionLocal() as db:
        owner = db.scalar(select(PortalUser).where(PortalUser.normalized_login == "origin-al"))
        if owner is None:
            raise RuntimeError("Origin-al portal account is missing")
        resource = db.scalar(
            select(PortalManagedUser).where(PortalManagedUser.portal_user_id == owner.id)
        )
        user_payload = serialize_user(owner).model_dump(mode="json")
        user_payload["linux_identity"] = resource_view(owner, resource)
        user_payload["compute_onboarding"] = compute_plan_view(owner, db)
        operations = db.scalars(
            select(PortalOperation).order_by(PortalOperation.created_at.desc()).limit(200)
        ).all()
        audits = db.scalars(
            select(PortalAuditEvent).order_by(PortalAuditEvent.timestamp.desc()).limit(200)
        ).all()
        account_states = Counter(
            str(value) for value in db.scalars(select(PortalUser.account_state)).all()
        )
        onboarding_states = Counter(
            str(value) for value in db.scalars(select(PortalManagedUser.onboarding_state)).all()
        )
        operation_states = Counter(str(item.status) for item in operations)
        audit_payload = [
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
            for event in audits
        ]
        operation_payload = [
            operation_response(item).model_dump(mode="json") for item in operations
        ]

    identity = {
        "status": "OK",
        "portal_users": sum(account_states.values()),
        "managed_linux_users": sum(onboarding_states.values()),
        "account_states": dict(account_states),
        "onboarding_states": dict(onboarding_states),
    }
    tasks = {
        "status": "OK",
        "pending_approval": operation_states.get("PENDING_APPROVAL", 0),
        "failed": operation_states.get("FAILED", 0),
        "total": sum(operation_states.values()),
    }
    overview = {
        "status": "OK",
        "platform": platform,
        "containers": containers,
        "storage": storage,
        "registries": registries,
        "alerts": alerts,
        "monitoring": monitoring,
        "gpu_health": gpu_health,
        "identity": identity,
        "tasks": tasks,
        "recent_audit": audit_payload[:12],
    }
    container_api: dict[str, Any] = {}
    for item in containers.get("containers", []):
        name = item.get("Names") if isinstance(item, dict) else None
        if isinstance(name, str) and (name.startswith("gpu-dev-") or name.startswith("h100-")):
            container_api[f"/containers/{name}"] = handlers.containers_inspect({"name": name})
    api = {
        "/auth/me": {"user": user_payload, "role": highest_role(owner)},
        "/platform/overview": overview,
        "/platform/gpus": {**gpu, "health": gpu_health},
        "/platform/gpu-health": gpu_health,
        "/platform/alerts": alerts,
        "/platform/monitoring": monitoring,
        "/platform/registries": registries,
        "/platform/storage": storage,
        "/platform/quotas": quotas,
        "/slurm/nodes": node,
        "/slurm/jobs": current_jobs,
        "/slurm/history": history,
        "/slurm/accounts": accounts,
        "/users": {"status": "OK", "users": [user_payload], "count": 1},
        f"/users/{owner.id}": {"status": "OK", "user": user_payload},
        "/containers": containers,
        "/images": {**images, "live_status": images.get("status")},
        "/operations": {"status": "OK", "operations": operation_payload},
        "/audit": {"status": "OK", "events": audit_payload},
        **container_api,
    }
    payload = {
        "captured_at": utcnow().isoformat(),
        "source": "same-run production read adapters and safe PostgreSQL fields",
        "api": api,
        "primary_user_id": str(owner.id),
        "primary_container": next(iter(container_api), "").removeprefix("/containers/"),
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
