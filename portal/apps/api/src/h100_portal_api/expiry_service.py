import sys
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from h100_portal_contracts.workspace import container_runtime_gpu_state, workspace_path
from sqlalchemy import exists, or_, select, update
from sqlalchemy.orm import Session, aliased

from h100_portal_api.audit import record_audit
from h100_portal_api.database import SessionLocal
from h100_portal_api.enums import OperationStatus, RiskLevel
from h100_portal_api.models import (
    PortalComputeLease,
    PortalContainer,
    PortalJob,
    PortalManagedUser,
    PortalOperation,
    PortalOperationEvent,
    PortalResourceRecycleItem,
    PortalSshKey,
    PortalStorageResource,
    ensure_utc,
    utcnow,
)
from h100_portal_api.worker_client import WorkerClientError, call_worker

DUE_STATES = {"ACTIVE", "RENEWAL_WINDOW", "RENEWAL_PENDING", "EXPIRED"}
RECYCLE_EVIDENCE_VERSION = "lease-recycle-cleanup-v1"
MAX_AUTOMATIC_ATTEMPTS = 3
AUTOMATIC_RETRY_DELAYS_SECONDS = (60, 300)
CleanupMode = Literal["AUTOMATIC", "PLATFORM_OWNER_RECOVERY"]


def _due_predicate(now: Any) -> Any:
    active_successor = aliased(PortalComputeLease)
    return (
        PortalComputeLease.state.in_(DUE_STATES),
        PortalComputeLease.expires_at <= now,
        PortalComputeLease.recycled_at.is_(None),
        or_(
            PortalComputeLease.state != "EXPIRED",
            ~exists().where(
                active_successor.previous_lease_id == PortalComputeLease.id,
                active_successor.state == "ACTIVE",
                active_successor.expires_at > now,
            ),
        ),
    )


def cleanup_evidence(operation: PortalOperation) -> dict[str, Any] | None:
    result = operation.dry_run_result
    if not isinstance(result, dict):
        return None
    value = result.get("recycle_cleanup")
    if not isinstance(value, dict) or value.get("version") != RECYCLE_EVIDENCE_VERSION:
        return None
    return value


def _operation_event(
    db: Session,
    operation: PortalOperation,
    from_status: str | None,
    to_status: OperationStatus,
    message: str,
) -> None:
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=from_status,
            to_status=to_status.value,
            safe_message=message,
        )
    )


def _running_evidence(mode: CleanupMode, attempt_count: int) -> dict[str, Any]:
    return {
        "recycle_cleanup": {
            "version": RECYCLE_EVIDENCE_VERSION,
            "mode": mode,
            "attempt_count": attempt_count,
            "status": "RUNNING",
            "lease_entitlement": "EXPIRED",
            "raw_argv_recorded": False,
            "data_delete_allowed": False,
        }
    }


def _automatic_operation(
    db: Session,
    managed: PortalManagedUser,
    lease: PortalComputeLease,
    now: datetime,
) -> tuple[PortalOperation, int] | None:
    key = f"lease-expire:{lease.id}"
    operation = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == managed.portal_user_id,
            PortalOperation.idempotency_key == key,
        )
    )
    if operation is None:
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
            started_at=now,
            dry_run_result=_running_evidence("AUTOMATIC", 1),
        )
        db.add(operation)
        db.flush()
        _operation_event(db, operation, None, OperationStatus.RUNNING, "expiry cleanup started")
        return operation, 1

    if operation.status != OperationStatus.FAILED:
        return None
    evidence = cleanup_evidence(operation)
    # A legacy failure has no bounded retry evidence. It therefore remains a
    # manual-review incident instead of being replayed forever after deployment.
    if evidence is None or evidence.get("manual_review_required") is True:
        return None
    try:
        previous_attempts = int(evidence.get("attempt_count", 0))
    except TypeError, ValueError:
        return None
    if previous_attempts < 1 or previous_attempts >= MAX_AUTOMATIC_ATTEMPTS:
        return None
    next_retry_raw = evidence.get("next_retry_at")
    if not isinstance(next_retry_raw, str):
        return None
    try:
        next_retry = ensure_utc(datetime.fromisoformat(next_retry_raw))
    except ValueError:
        return None
    if now < next_retry:
        return None
    attempt_count = previous_attempts + 1
    operation.status = OperationStatus.RUNNING
    operation.started_at = now
    operation.finished_at = None
    operation.error_code = None
    operation.result_summary = None
    operation.dry_run_result = _running_evidence("AUTOMATIC", attempt_count)
    _operation_event(
        db,
        operation,
        OperationStatus.FAILED.value,
        OperationStatus.RUNNING,
        f"automatic cleanup retry {attempt_count} started",
    )
    return operation, attempt_count


def _activate_successor(
    db: Session, lease: PortalComputeLease, now: datetime
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


def _safe_job_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return sorted({item for item in value if isinstance(item, int) and item > 0})


def _update_cancelled_jobs(
    db: Session,
    managed: PortalManagedUser,
    result: dict[str, Any],
    now: datetime,
) -> tuple[list[int], list[int]]:
    pending = _safe_job_ids(result.get("cancelled_pending_job_ids"))
    running = _safe_job_ids(result.get("cancelled_running_job_ids"))
    cancelled = set(pending) | set(running)
    if cancelled:
        db.execute(
            update(PortalJob)
            .where(
                PortalJob.owner_managed_user_id == managed.id,
                PortalJob.slurm_job_id.in_(cancelled),
            )
            .values(state="CANCELLED", finished_at=now)
        )
    return pending, running


def _failure_evidence(
    *,
    mode: CleanupMode,
    attempt_count: int,
    error_code: str,
    result: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    worker_result = result or {}
    worker_retryable = result is None or worker_result.get("cleanup_retryable") is True
    automatic_retry = mode == "AUTOMATIC" and worker_retryable
    retry_index = attempt_count - 1
    has_retry_delay = 0 <= retry_index < len(AUTOMATIC_RETRY_DELAYS_SECONDS)
    next_retry = (
        now + timedelta(seconds=AUTOMATIC_RETRY_DELAYS_SECONDS[retry_index])
        if automatic_retry and has_retry_delay
        else None
    )
    manual_review = next_retry is None
    return {
        "recycle_cleanup": {
            "version": RECYCLE_EVIDENCE_VERSION,
            "mode": mode,
            "attempt_count": attempt_count,
            "status": "FAILED",
            "error_code": error_code,
            "first_failed_step": str(worker_result.get("first_failed_step", "WORKER_TRANSPORT"))[
                :64
            ],
            "lease_entitlement": "EXPIRED",
            "new_access": str(worker_result.get("new_access", "NOT_VERIFIED"))[:32],
            "container_stop": "FAILED",
            "container_key_state": str(worker_result.get("container_key_state", "NOT_VERIFIED"))[
                :32
            ],
            "cancelled_pending_job_ids": _safe_job_ids(
                worker_result.get("cancelled_pending_job_ids")
            ),
            "cancelled_running_job_ids": _safe_job_ids(
                worker_result.get("cancelled_running_job_ids")
            ),
            "data_preserved": worker_result.get("data_preserved") is True,
            "quota_preserved": True,
            "linux_identity_preserved": True,
            "slurm_history_preserved": True,
            "auto_permanent_delete": False,
            "automatic_retry": next_retry is not None,
            "next_retry_at": next_retry.isoformat() if next_retry is not None else None,
            "manual_review_required": manual_review,
            "raw_argv_recorded": False,
        }
    }


def _record_failure(
    db: Session,
    *,
    lease: PortalComputeLease,
    managed: PortalManagedUser,
    container: PortalContainer,
    keys: Sequence[PortalSshKey],
    expected_key_fingerprints: list[str],
    operation: PortalOperation,
    mode: CleanupMode,
    attempt_count: int,
    actor: str,
    actor_role: str,
    error_code: str,
    result: dict[str, Any] | None,
    now: datetime,
) -> None:
    lease.state = "EXPIRED"
    lease.expired_at = lease.expired_at or now
    managed.compute_environment_state = "SUSPENDED"
    container.desired_state = "STOPPED"
    worker_result = result or {}
    if worker_result.get("gpu_allocation_revoked") is True:
        container.gpu_allocation_job_id = None
        container.gpu_allocation_uuid = None
        container.observed_state = "STOPPED"
    observed = worker_result.get("container_key_fingerprints")
    key_suspension_verified = (
        worker_result.get("container_key_state") == "SUSPENDED_BY_RECYCLE"
        and isinstance(observed, list)
        and sorted(str(item) for item in observed) == expected_key_fingerprints
    )
    if key_suspension_verified:
        for key in keys:
            key.container_install_state = "SUSPENDED_BY_RECYCLE"
    _update_cancelled_jobs(db, managed, worker_result, now)
    storage = db.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == managed.id
        )
    )
    if storage is not None:
        storage.state = "PRESERVED"
    operation.status = OperationStatus.FAILED
    operation.finished_at = now
    operation.error_code = error_code[:64]
    operation.worker_execution_id = (
        str(worker_result.get("request_id", ""))[:64] or operation.worker_execution_id
    )
    operation.dry_run_result = _failure_evidence(
        mode=mode,
        attempt_count=attempt_count,
        error_code=error_code,
        result=result,
        now=now,
    )
    evidence = cleanup_evidence(operation) or {}
    operation.result_summary = (
        "Lease entitlement expired; cleanup incomplete; automatic retry scheduled"
        if evidence.get("automatic_retry") is True
        else "Lease entitlement expired; cleanup incomplete; platform_owner recovery required"
    )
    _operation_event(
        db,
        operation,
        OperationStatus.RUNNING.value,
        OperationStatus.FAILED,
        f"cleanup failed safely: {error_code[:64]}",
    )
    record_audit(
        db,
        event_type="LEASE_EXPIRY_CLEANUP_FAILED",
        actor=actor,
        actor_role=actor_role,
        source_ip="local-worker-socket",
        user_agent="h100-portal-lease-expiry",
        object_type="compute_lease",
        object_id=str(lease.id),
        result="FAILED",
        metadata={
            "error_code": error_code[:64],
            "attempt_count": attempt_count,
            "new_access": evidence.get("new_access"),
            "next_retry_at": evidence.get("next_retry_at"),
            "manual_review_required": evidence.get("manual_review_required"),
            "data_preserved": True,
        },
        operation_id=operation.id,
    )


def _record_success(
    db: Session,
    *,
    lease: PortalComputeLease,
    managed: PortalManagedUser,
    container: PortalContainer,
    keys: Sequence[PortalSshKey],
    operation: PortalOperation,
    result: dict[str, Any],
    mode: CleanupMode,
    attempt_count: int,
    actor: str,
    actor_role: str,
    now: datetime,
) -> None:
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
    lease.expired_at = lease.expired_at or now
    lease.recycled_at = now
    managed.compute_environment_state = "RECYCLED"
    container.observed_state = "STOPPED"
    container.desired_state = "STOPPED"
    container.gpu_allocation_job_id = None
    container.gpu_allocation_uuid = None
    for key in keys:
        key.container_install_state = "SUSPENDED_BY_RECYCLE"
    storage = db.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == managed.id
        )
    )
    if storage is not None:
        storage.state = "PRESERVED"
    pending, running = _update_cancelled_jobs(db, managed, result, now)
    operation.status = OperationStatus.SUCCEEDED
    operation.finished_at = now
    operation.error_code = None
    operation.worker_execution_id = str(result.get("request_id", ""))[:64] or None
    operation.result_summary = "Lease expired; jobs cancelled; container stopped and recoverable"
    operation.dry_run_result = {
        "recycle_cleanup": {
            "version": RECYCLE_EVIDENCE_VERSION,
            "mode": mode,
            "attempt_count": attempt_count,
            "status": "SUCCEEDED",
            "lease_entitlement": "EXPIRED",
            "new_access": "DENIED",
            "container_stop": "SUCCEEDED",
            "container_key_state": "SUSPENDED_BY_RECYCLE",
            "cancelled_pending_job_ids": pending,
            "cancelled_running_job_ids": running,
            "data_preserved": True,
            "quota_preserved": True,
            "linux_identity_preserved": True,
            "slurm_history_preserved": True,
            "auto_permanent_delete": False,
            "manual_review_required": False,
            "raw_argv_recorded": False,
        }
    }
    _operation_event(
        db,
        operation,
        OperationStatus.RUNNING.value,
        OperationStatus.SUCCEEDED,
        "lease cleanup completed into recycle bin",
    )
    record_audit(
        db,
        event_type="LEASE_EXPIRED",
        actor=actor,
        actor_role=actor_role,
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
        actor=actor,
        actor_role=actor_role,
        source_ip="local-worker-socket",
        user_agent="h100-portal-lease-expiry",
        object_type="resource_recycle_item",
        object_id=str(item.id),
        metadata={"auto_permanent_delete": False},
        operation_id=operation.id,
    )


def _execute_cleanup(
    db: Session,
    *,
    lease: PortalComputeLease,
    managed: PortalManagedUser,
    operation: PortalOperation,
    mode: CleanupMode,
    attempt_count: int,
    actor: str,
    actor_role: str,
) -> bool:
    now = utcnow()
    container = db.scalar(
        select(PortalContainer)
        .where(PortalContainer.owner_managed_user_id == managed.id)
        .with_for_update()
    )
    if container is None:
        raise RuntimeError("managed container for due lease is missing")
    keys = db.scalars(
        select(PortalSshKey).where(
            PortalSshKey.owner_managed_user_id == managed.id,
            PortalSshKey.active.is_(True),
            PortalSshKey.scope == "CONTAINER",
            PortalSshKey.container_install_state.in_({"INSTALLED", "SUSPENDED_BY_RECYCLE"}),
        )
    ).all()
    expected_key_fingerprints = sorted(key.fingerprint_sha256 for key in keys)
    if not expected_key_fingerprints:
        raise RuntimeError("managed container has no retained Portal SSH key")
    lease.state = "EXPIRED"
    lease.expired_at = lease.expired_at or now
    managed.compute_environment_state = "SUSPENDED"
    container.desired_state = "STOPPED"
    payload = {
        "lease_id": str(lease.id),
        "managed_user_id": str(managed.id),
        "username": managed.unix_username,
        "uid": managed.uid,
        "gid": managed.gid,
        "container_name": container.name,
        "workspace_path": str(workspace_path(managed.uid)),
        "development_profile": container.development_profile,
        "container_gpu": container.gpu_count,
        "gpu_allocation_job_id": container.gpu_allocation_job_id,
        "gpu_allocation_uuid": container.gpu_allocation_uuid,
        "slurm_account": managed.slurm_account,
        "slurm_qos": managed.slurm_qos,
        "expires_at": ensure_utc(lease.expires_at).isoformat(),
        "expected_gpu": container_runtime_gpu_state(
            container.gpu_allocation_job_id, container.gpu_allocation_uuid
        ),
        "host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_key_fingerprints": expected_key_fingerprints,
    }
    result: dict[str, Any] | None = None
    try:
        result = call_worker(
            "resource.recycle",
            payload=payload,
            requested_by=actor,
            approved_by=actor,
            idempotency_key=(
                f"resource-recycle:{lease.id}"
                if mode == "AUTOMATIC"
                else f"resource-recycle-recovery:{lease.id}:{operation.id}"
            ),
            dry_run=False,
            timeout_seconds=180,
        )
    except WorkerClientError as exc:
        _record_failure(
            db,
            lease=lease,
            managed=managed,
            container=container,
            keys=keys,
            expected_key_fingerprints=expected_key_fingerprints,
            operation=operation,
            mode=mode,
            attempt_count=attempt_count,
            actor=actor,
            actor_role=actor_role,
            error_code=exc.code,
            result=None,
            now=utcnow(),
        )
        return False
    observed_fingerprints = result.get("container_key_fingerprints")
    expected_gpu = "NONE"
    success = (
        result.get("status") == "SUCCEEDED"
        and result.get("container_state") == "STOPPED"
        and result.get("container_gpu") == expected_gpu
        and result.get("gpu_allocation_job_id") is None
        and result.get("gpu_allocation_uuid") is None
        and result.get("container_key_state") == "SUSPENDED_BY_RECYCLE"
        and isinstance(observed_fingerprints, list)
        and sorted(str(item) for item in observed_fingerprints) == expected_key_fingerprints
        and result.get("data_preserved") is True
    )
    if not success:
        error_value = result.get("error")
        error = error_value if isinstance(error_value, dict) else {}
        error_code = str(error.get("code", "RECYCLE_FAILED"))[:64]
        _record_failure(
            db,
            lease=lease,
            managed=managed,
            container=container,
            keys=keys,
            expected_key_fingerprints=expected_key_fingerprints,
            operation=operation,
            mode=mode,
            attempt_count=attempt_count,
            actor=actor,
            actor_role=actor_role,
            error_code=error_code,
            result=result,
            now=utcnow(),
        )
        return False
    _record_success(
        db,
        lease=lease,
        managed=managed,
        container=container,
        keys=keys,
        operation=operation,
        result=result,
        mode=mode,
        attempt_count=attempt_count,
        actor=actor,
        actor_role=actor_role,
        now=utcnow(),
    )
    return True


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
    operation_and_attempt = _automatic_operation(db, managed, lease, now)
    if operation_and_attempt is None:
        return False
    operation, attempt_count = operation_and_attempt
    return _execute_cleanup(
        db,
        lease=lease,
        managed=managed,
        operation=operation,
        mode="AUTOMATIC",
        attempt_count=attempt_count,
        actor="portal-lease-worker",
        actor_role="system",
    )


def retry_failed_recycle(
    db: Session,
    *,
    lease: PortalComputeLease,
    operation: PortalOperation,
    actor: str,
) -> bool:
    now = utcnow()
    if ensure_utc(lease.expires_at) > now or lease.recycled_at is not None:
        raise ValueError("lease is not an expired unrecycled recovery target")
    managed = db.scalar(
        select(PortalManagedUser)
        .where(PortalManagedUser.id == lease.owner_managed_user_id)
        .with_for_update()
    )
    if managed is None:
        raise ValueError("managed identity for recovery is missing")
    operation.status = OperationStatus.RUNNING
    operation.started_at = now
    operation.dry_run_result = _running_evidence("PLATFORM_OWNER_RECOVERY", 1)
    _operation_event(
        db,
        operation,
        None,
        OperationStatus.RUNNING,
        "platform_owner recycle recovery started",
    )
    return _execute_cleanup(
        db,
        lease=lease,
        managed=managed,
        operation=operation,
        mode="PLATFORM_OWNER_RECOVERY",
        attempt_count=1,
        actor=actor,
        actor_role="platform_owner",
    )


def process_due_leases(limit: int = 32) -> tuple[int, int]:
    processed = 0
    recycled = 0
    with SessionLocal() as db:
        due_ids = db.scalars(
            select(PortalComputeLease.id)
            .where(*_due_predicate(utcnow()))
            .order_by(PortalComputeLease.expires_at)
            .limit(limit)
        ).all()
    for lease_id in due_ids:
        with SessionLocal() as db:
            lease = db.scalar(
                select(PortalComputeLease)
                .where(PortalComputeLease.id == lease_id, *_due_predicate(utcnow()))
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
