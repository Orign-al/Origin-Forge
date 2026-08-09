import sys
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.database import SessionLocal
from h100_portal_api.enums import OperationStatus, RiskLevel
from h100_portal_api.models import (
    PortalComputeLease,
    PortalContainer,
    PortalJob,
    PortalManagedUser,
    PortalOperation,
    PortalResourceRecycleItem,
    PortalSshKey,
    PortalStorageResource,
    ensure_utc,
    utcnow,
)
from h100_portal_api.worker_client import WorkerClientError, call_worker

DUE_STATES = {"ACTIVE", "RENEWAL_WINDOW", "RENEWAL_PENDING", "EXPIRED"}


def _operation(
    db: Session, managed: PortalManagedUser, lease: PortalComputeLease
) -> PortalOperation:
    key = f"lease-expire:{lease.id}"
    existing = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == managed.portal_user_id,
            PortalOperation.idempotency_key == key,
        )
    )
    if existing is not None:
        existing.status = OperationStatus.RUNNING
        existing.started_at = utcnow()
        existing.finished_at = None
        existing.error_code = None
        return existing
    operation = PortalOperation(
        operation_type="lease.expire",
        target_type="compute_lease",
        target_id=str(lease.id),
        requested_by=managed.portal_user_id,
        owner_managed_user_id=managed.id,
        approved_by=None,
        request_summary="可信服务器时钟触发计算租约到期回收",
        validated_payload={
            "managed_user_id": str(managed.id),
            "lease_id": str(lease.id),
            "expires_at": ensure_utc(lease.expires_at).isoformat(),
        },
        idempotency_key=key,
        risk_level=RiskLevel.HIGH,
        status=OperationStatus.RUNNING,
        started_at=utcnow(),
    )
    db.add(operation)
    db.flush()
    return operation


def _activate_successor(
    db: Session, lease: PortalComputeLease, now: Any
) -> PortalComputeLease | None:
    successor = db.scalar(
        select(PortalComputeLease)
        .where(
            PortalComputeLease.previous_lease_id == lease.id,
            PortalComputeLease.state == "APPROVED",
        )
        .with_for_update()
    )
    if successor is None or ensure_utc(successor.expires_at) <= now:
        return None
    lease.state = "EXPIRED"
    lease.expired_at = now
    successor.state = "ACTIVE"
    return successor


def _recycle_one(db: Session, lease: PortalComputeLease) -> bool:
    now = utcnow()
    managed = db.scalar(
        select(PortalManagedUser)
        .where(PortalManagedUser.id == lease.owner_managed_user_id)
        .with_for_update()
    )
    if managed is None:
        raise RuntimeError("managed identity for due lease is missing")
    if _activate_successor(db, lease, now) is not None:
        record_audit(
            db,
            event_type="LEASE_RENEWAL_ACTIVATED",
            actor="portal-lease-worker",
            actor_role="system",
            source_ip="local-worker-socket",
            user_agent="h100-portal-lease-expiry",
            object_type="compute_lease",
            object_id=str(lease.id),
            metadata={"recycled": False},
        )
        return False
    container = db.scalar(
        select(PortalContainer)
        .where(PortalContainer.owner_managed_user_id == managed.id)
        .with_for_update()
    )
    if container is None:
        raise RuntimeError("managed container for due lease is missing")
    operation = _operation(db, managed, lease)
    payload = {
        "lease_id": str(lease.id),
        "managed_user_id": str(managed.id),
        "username": managed.unix_username,
        "uid": managed.uid,
        "gid": managed.gid,
        "container_name": container.name,
        "expires_at": ensure_utc(lease.expires_at).isoformat(),
        "expected_gpu": "NONE",
        "host_access": "DISABLED_BY_PLATFORM_POLICY",
    }
    lease.state = "EXPIRED"
    lease.expired_at = lease.expired_at or now
    try:
        result = call_worker(
            "resource.recycle",
            payload=payload,
            requested_by="portal-lease-worker",
            approved_by="portal-lease-worker",
            idempotency_key=f"resource-recycle:{lease.id}",
            dry_run=False,
            timeout_seconds=180,
        )
    except WorkerClientError as exc:
        operation.status = OperationStatus.FAILED
        operation.error_code = exc.code[:64]
        operation.finished_at = utcnow()
        return False
    if (
        result.get("status") != "SUCCEEDED"
        or result.get("container_state") != "STOPPED"
        or result.get("container_gpu") != "NONE"
        or result.get("container_key_state") != "SUSPENDED_BY_RECYCLE"
        or result.get("data_preserved") is not True
    ):
        operation.status = OperationStatus.FAILED
        error = result.get("error", {})
        operation.error_code = str(error.get("code", "RECYCLE_FAILED"))[:64]
        operation.finished_at = utcnow()
        return False
    item = db.scalar(
        select(PortalResourceRecycleItem).where(PortalResourceRecycleItem.lease_id == lease.id)
    )
    if item is None:
        item = PortalResourceRecycleItem(
            owner_managed_user_id=managed.id,
            lease_id=lease.id,
            container_id=container.id,
            state="RECYCLE_BIN",
            resource_name=container.name,
            image_digest=container.image_digest,
            retained_spec=container.safe_spec,
            connection_state="DISABLED",
            data_preserved=True,
            auto_permanent_delete=False,
            expires_at=lease.expires_at,
            recycled_at=now,
        )
        db.add(item)
        db.flush()
    lease.state = "RECYCLE_BIN"
    lease.recycled_at = now
    managed.compute_environment_state = "RECYCLED"
    container.observed_state = "STOPPED"
    container.desired_state = "STOPPED"
    for key in db.scalars(
        select(PortalSshKey).where(
            PortalSshKey.owner_managed_user_id == managed.id,
            PortalSshKey.active.is_(True),
        )
    ):
        key.container_install_state = "SUSPENDED_BY_RECYCLE"
    storage = db.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == managed.id
        )
    )
    if storage is not None:
        storage.state = "PRESERVED"
    cancelled = set(result.get("cancelled_pending_job_ids", [])) | set(
        result.get("cancelled_running_job_ids", [])
    )
    if cancelled:
        db.execute(
            update(PortalJob)
            .where(
                PortalJob.owner_managed_user_id == managed.id,
                PortalJob.slurm_job_id.in_(cancelled),
            )
            .values(state="CANCELLED", finished_at=now)
        )
    operation.status = OperationStatus.SUCCEEDED
    operation.finished_at = now
    operation.worker_execution_id = str(result.get("request_id", ""))[:64] or None
    operation.result_summary = "Lease expired; jobs cancelled; container stopped and recoverable"
    record_audit(
        db,
        event_type="LEASE_EXPIRED",
        actor="portal-lease-worker",
        actor_role="system",
        source_ip="local-worker-socket",
        user_agent="h100-portal-lease-expiry",
        object_type="compute_lease",
        object_id=str(lease.id),
        metadata={"container_stopped": True, "data_preserved": True},
        operation_id=operation.id,
    )
    record_audit(
        db,
        event_type="RESOURCE_RECYCLED",
        actor="portal-lease-worker",
        actor_role="system",
        source_ip="local-worker-socket",
        user_agent="h100-portal-lease-expiry",
        object_type="resource_recycle_item",
        object_id=str(item.id),
        metadata={"auto_permanent_delete": False},
        operation_id=operation.id,
    )
    return True


def process_due_leases(limit: int = 32) -> tuple[int, int]:
    processed = 0
    recycled = 0
    with SessionLocal() as db:
        due_ids = db.scalars(
            select(PortalComputeLease.id)
            .where(
                PortalComputeLease.state.in_(DUE_STATES),
                PortalComputeLease.expires_at <= utcnow(),
                PortalComputeLease.recycled_at.is_(None),
            )
            .order_by(PortalComputeLease.expires_at)
            .limit(limit)
        ).all()
    for lease_id in due_ids:
        with SessionLocal() as db:
            lease = db.scalar(
                select(PortalComputeLease)
                .where(
                    PortalComputeLease.id == lease_id,
                    PortalComputeLease.state.in_(DUE_STATES),
                    PortalComputeLease.expires_at <= utcnow(),
                    PortalComputeLease.recycled_at.is_(None),
                )
                .with_for_update()
            )
            if lease is None:
                continue
            processed += 1
            recycled += int(_recycle_one(db, lease))
            db.commit()
    return processed, recycled


def main() -> int:
    try:
        processed, recycled = process_due_leases()
    except Exception as exc:
        print(f"lease expiry worker failed: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    print(f"processed={processed} recycled={recycled}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
