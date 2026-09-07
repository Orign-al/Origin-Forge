import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from h100_portal_contracts.workspace import (
    GPU_DEVELOPMENT_PROFILE,
    WORKSPACE_CONTAINER_PATH,
    WORKSPACE_DEFAULT_WORKDIR,
    container_runtime_gpu_state,
    workspace_binding,
    workspace_path,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from h100_portal_api import expiry_service
from h100_portal_api.audit import record_audit
from h100_portal_api.auth import (
    AuthContext,
    client_ip,
    require_recent_reauthentication,
    require_session_csrf,
    user_agent,
)
from h100_portal_api.config import get_settings
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import (
    permission_dependency,
    request_auth_context,
    require_delegated_scope,
)
from h100_portal_api.enums import OperationStatus, RiskLevel
from h100_portal_api.lease_service import (
    RenewalLeaseExpiredError,
    create_lease,
    decide_renewal,
    entitlement,
    lease_view,
    managed_identity_for_user,
    request_renewal,
)
from h100_portal_api.models import (
    PortalComputeLease,
    PortalContainer,
    PortalJob,
    PortalJobGpuApproval,
    PortalJobMemoryApproval,
    PortalLeaseRenewalRequest,
    PortalManagedUser,
    PortalOperation,
    PortalResourceRecycleItem,
    PortalResourceRestoreRequest,
    PortalSshKey,
    PortalStorageResource,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.rbac import highest_role
from h100_portal_api.schemas import (
    JobGpuApprovalDecisionRequest,
    JobMemoryApprovalDecisionRequest,
    LeaseDecisionRequest,
    LeaseRecoveryApplyRequest,
    LeaseRenewalCreateRequest,
    RestoreCreateRequest,
    RestoreDecisionRequest,
    SelfContainerActionRequest,
    SelfJobSubmitRequest,
    SelfTerminalCreateRequest,
    SelfTerminalInputRequest,
    SelfTerminalResizeRequest,
)
from h100_portal_api.self_resources import (
    SelfResourceContext,
    resolve_self_compute_context,
)
from h100_portal_api.terminal_service import (
    TerminalRecord,
    TerminalServiceError,
    terminal_registry,
)
from h100_portal_api.worker_client import WorkerClientError, call_worker
from h100_portal_api.workspace_service import WorkspaceResolution, resolve_workspace

router = APIRouter(tags=["self-service"])

APPROVED_IMAGE_REFS = {
    "nvcr.io#nvidia/cuda:13.2.0-base-ubuntu24.04@"
    "sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a"
}
APPROVED_JOB_IMAGE = next(iter(APPROVED_IMAGE_REFS))
JOB_DEFAULTS = {"cpus": 2, "memory_mb": 4096, "gpu_count": 0, "time_limit_seconds": 1800}
JOB_MEMORY_APPROVAL_THRESHOLD_MB = 32768
JOB_MEMORY_MAX_MB = 486377
JOB_LIMITS = {
    "cpus": {"minimum": 1, "maximum": 8},
    "memory_mb": {
        "minimum": 256,
        "maximum": JOB_MEMORY_MAX_MB,
        "approval_required_above": JOB_MEMORY_APPROVAL_THRESHOLD_MB,
    },
    "gpu_count": {"minimum": 0, "maximum": 4, "approval_required_from": 2},
    "time_limit_seconds": {"minimum": 60, "maximum": 345600},
    "script_bytes": {"maximum": 8192},
}
JOB_TERMINAL_STATES = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "REVOKED",
        "REJECTED",
        "TIMEOUT",
    }
)
JOB_STATES = frozenset(
    {
        "APPROVAL_PENDING",
        "SUBMITTING",
        "PENDING",
        "CONFIGURING",
        "RUNNING",
        "SUSPENDED",
        "STOPPED",
        "COMPLETING",
        "REQUEUED",
        "RESIZING",
        *JOB_TERMINAL_STATES,
        "UNKNOWN",
    }
)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _audit(
    db: Session,
    request: Request,
    context: AuthContext,
    *,
    event_type: str,
    object_type: str,
    object_id: str,
    result: str = "SUCCESS",
    metadata: dict[str, Any] | None = None,
) -> None:
    actor = context.actor
    safe_metadata = dict(metadata or {})
    if context.delegation is not None:
        safe_metadata.update(
            {
                "delegation_id": str(context.delegation.id),
                "actor_user": actor.normalized_login,
                "effective_user": context.user.normalized_login,
                "delegated_scopes": sorted(context.delegated_scopes),
            }
        )
    record_audit(
        db,
        event_type=event_type,
        actor=actor.normalized_login,
        actor_role=highest_role(actor),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type=object_type,
        object_id=object_id,
        result=result,
        metadata=safe_metadata,
    )


def _worker(
    operation_type: str,
    *,
    payload: dict[str, Any],
    context: AuthContext,
    idempotency_key: str,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    try:
        result = call_worker(
            operation_type,
            payload=payload,
            requested_by=context.user.normalized_login,
            approved_by=context.user.normalized_login,
            idempotency_key=idempotency_key,
            dry_run=False,
            timeout_seconds=timeout_seconds,
        )
    except WorkerClientError as exc:
        raise _error(503, exc.code, str(exc)) from exc
    if result.get("status") not in {"OK", "SUCCEEDED"}:
        error = result.get("error", {})
        code = str(error.get("code", "WORKER_OPERATION_FAILED"))
        message = str(error.get("message", "受控 Worker 操作失败"))
        detail: dict[str, Any] = {"code": code, "message": message}
        rollback_status = error.get("rollback_status")
        if rollback_status in {"NOT_REQUIRED", "ROLLED_BACK", "ROLLBACK_FAILED"}:
            detail["rollback_status"] = rollback_status
        if result.get("gpu_allocation_state_known") is True:
            detail.update(
                {
                    "gpu_allocation_state_known": True,
                    "gpu_allocation_job_id": result.get("gpu_allocation_job_id"),
                    "gpu_allocation_uuid": result.get("gpu_allocation_uuid"),
                }
            )
        raise HTTPException(status_code=409, detail=detail)
    return result


def _rollback_restored_resource(
    *,
    payload: dict[str, Any],
    worker_result: dict[str, Any],
    context: AuthContext,
    restore_id: uuid.UUID,
    recycled_lease_id: uuid.UUID,
    recycled_lease_starts_at: datetime,
    recycled_lease_expires_at: datetime,
) -> bool:
    """Return a Worker-restored environment to RECYCLE_BIN after API failure."""

    recycle_payload = {
        key: payload[key]
        for key in (
            "managed_user_id",
            "username",
            "uid",
            "gid",
            "container_name",
            "workspace_path",
            "development_profile",
            "container_gpu",
            "slurm_account",
            "slurm_qos",
            "expected_gpu",
            "host_access",
            "expected_key_fingerprints",
        )
    }
    recycle_payload.update(
        {
            "restore_request_id": str(restore_id),
            "attempted_lease_id": payload["lease_id"],
            "attempted_lease_starts_at": payload["lease_starts_at"],
            "attempted_lease_expires_at": payload["lease_expires_at"],
            "recycle_lease_id": str(recycled_lease_id),
            "recycle_lease_starts_at": ensure_utc(recycled_lease_starts_at).isoformat(),
            "recycle_lease_expires_at": ensure_utc(recycled_lease_expires_at).isoformat(),
            "gpu_allocation_job_id": worker_result.get("gpu_allocation_job_id"),
            "gpu_allocation_uuid": worker_result.get("gpu_allocation_uuid"),
        }
    )
    recycle_payload["expected_gpu"] = container_runtime_gpu_state(
        recycle_payload["gpu_allocation_job_id"], recycle_payload["gpu_allocation_uuid"]
    )
    try:
        rollback = call_worker(
            "resource.restore.rollback",
            payload=recycle_payload,
            requested_by=context.user.normalized_login,
            approved_by=context.user.normalized_login,
            idempotency_key=f"resource-restore-rollback:{restore_id}",
            dry_run=False,
            timeout_seconds=180,
        )
    except WorkerClientError:
        return False
    return bool(
        rollback.get("status") == "SUCCEEDED"
        and rollback.get("container_state") == "STOPPED"
        and rollback.get("gpu_allocation_job_id") is None
        and rollback.get("gpu_allocation_uuid") is None
        and rollback.get("container_key_state") == "SUSPENDED_BY_RECYCLE"
        and rollback.get("data_preserved") is True
    )


def _apply_restore_failure_state(
    db: Session,
    *,
    operation: PortalOperation,
    restore: PortalResourceRestoreRequest,
    item: PortalResourceRecycleItem,
    managed: PortalManagedUser,
    container: PortalContainer,
    suspended_keys: Sequence[PortalSshKey],
    code: str,
    rollback_ok: bool,
) -> None:
    operation.status = OperationStatus.FAILED
    operation.error_code = code[:64]
    operation.finished_at = utcnow()
    operation.rollback_status = "ROLLED_BACK" if rollback_ok else "REQUIRES_MANUAL_REVIEW"
    restore.state = "FAILED"
    if rollback_ok:
        item.state = "RECYCLE_BIN"
        managed.compute_environment_state = "RECYCLED"
        container.desired_state = "STOPPED"
        container.observed_state = "STOPPED"
        container.gpu_allocation_job_id = None
        container.gpu_allocation_uuid = None
        for key in suspended_keys:
            key.container_install_state = "SUSPENDED_BY_RECYCLE"
        storage = db.scalar(
            select(PortalStorageResource).where(
                PortalStorageResource.owner_managed_user_id == managed.id
            )
        )
        if storage is not None:
            storage.state = "PRESERVED"
    else:
        item.state = "FAILED"
        managed.compute_environment_state = "FAILED"
        container.observed_state = "UNKNOWN"


def _new_operation(
    db: Session,
    context: AuthContext,
    *,
    owner_id: uuid.UUID,
    operation_type: str,
    target_type: str,
    target_id: str,
    summary: str,
    payload: dict[str, Any],
    idempotency_key: str,
    risk_level: RiskLevel = RiskLevel.MEDIUM,
    status_value: OperationStatus = OperationStatus.RUNNING,
) -> PortalOperation:
    actor = context.actor
    validated_payload = dict(payload)
    if context.delegation is not None:
        validated_payload["delegated_test_context"] = {
            "delegation_id": str(context.delegation.id),
            "actor_user": actor.normalized_login,
            "effective_user": context.user.normalized_login,
            "scopes": sorted(context.delegated_scopes),
        }
    operation = PortalOperation(
        operation_type=operation_type,
        target_type=target_type,
        target_id=target_id,
        requested_by=actor.id,
        owner_managed_user_id=owner_id,
        approved_by=actor.id if status_value != OperationStatus.PENDING_APPROVAL else None,
        request_summary=summary,
        validated_payload=validated_payload,
        idempotency_key=idempotency_key,
        risk_level=risk_level,
        status=status_value,
        approved_at=utcnow() if status_value != OperationStatus.PENDING_APPROVAL else None,
        started_at=utcnow() if status_value != OperationStatus.PENDING_APPROVAL else None,
    )
    db.add(operation)
    db.flush()
    return operation


def _container_for_owner(
    db: Session, owner_id: uuid.UUID, *, lock: bool = False
) -> PortalContainer:
    query = select(PortalContainer).where(PortalContainer.owner_managed_user_id == owner_id)
    if lock:
        query = query.with_for_update()
    container = db.scalar(query)
    if container is None:
        raise _error(404, "MANAGED_CONTAINER_NOT_FOUND", "开发容器不存在")
    return container


def _container_view(container: PortalContainer, lease_active: bool) -> dict[str, Any]:
    return {
        "id": str(container.id),
        "name": container.name,
        "state": container.observed_state,
        "connection_state": "AVAILABLE"
        if lease_active and container.observed_state == "RUNNING"
        else "DISABLED",
        "profile": container.development_profile,
        "gpu": container.gpu_count,
        "gpu_allocation_state": (
            "ALLOCATED" if container.gpu_allocation_job_id is not None else "NONE"
        ),
        "cpus": container.safe_spec.get("cpus", 8),
        "memory_gb": container.safe_spec.get("memory_gb", 32),
        "pids_limit": container.safe_spec.get("pids_limit", 4096),
        "privileged": False,
        "docker_socket": False,
        "munge": False,
    }


def _execute_restore(
    db: Session,
    request: Request,
    context: AuthContext,
    *,
    restore: PortalResourceRestoreRequest,
    item: PortalResourceRecycleItem,
    managed: PortalManagedUser,
    worker_operation_type: str,
    operation_summary: str,
) -> dict[str, Any]:
    """Restore one owner-bound environment through the fixed Worker boundary."""

    container = _container_for_owner(db, managed.id, lock=True)
    suspended_keys = db.scalars(
        select(PortalSshKey).where(
            PortalSshKey.owner_managed_user_id == managed.id,
            PortalSshKey.active.is_(True),
            PortalSshKey.scope == "CONTAINER",
            PortalSshKey.container_install_state == "SUSPENDED_BY_RECYCLE",
        )
    ).all()
    expected_key_fingerprints = sorted(key.fingerprint_sha256 for key in suspended_keys)
    if not expected_key_fingerprints:
        raise _error(409, "CONTAINER_KEY_RESTORE_FAILED", "没有可恢复的容器 SSH 公钥")
    recycled_lease = db.get(PortalComputeLease, item.lease_id)
    if recycled_lease is None or recycled_lease.owner_managed_user_id != managed.id:
        raise _error(409, "RESTORE_OWNERSHIP_MISMATCH", "原租约所有权不一致")
    lease = create_lease(
        managed_user_id=managed.id,
        starts_at=utcnow(),
        duration_seconds=restore.requested_duration_seconds,
        gpu_count=recycled_lease.gpu_count,
        approved_by=context.user.id,
        restored=True,
    )
    lease.id = uuid.uuid4()
    expected_gpu = "NONE"
    payload = {
        "restore_request_id": str(restore.id),
        "managed_user_id": str(managed.id),
        "username": managed.unix_username,
        "uid": managed.uid,
        "gid": managed.gid,
        "container_name": container.name,
        "workspace_path": str(workspace_path(managed.uid)),
        "development_profile": container.development_profile,
        "container_gpu": container.gpu_count,
        "gpu_allocation_job_id": None,
        "gpu_allocation_uuid": None,
        "slurm_account": managed.slurm_account,
        "slurm_qos": managed.slurm_qos,
        "recycle_lease_id": str(recycled_lease.id),
        "recycle_lease_starts_at": ensure_utc(recycled_lease.starts_at).isoformat(),
        "recycle_lease_expires_at": ensure_utc(recycled_lease.expires_at).isoformat(),
        "lease_id": str(lease.id),
        "lease_starts_at": ensure_utc(lease.starts_at).isoformat(),
        "lease_expires_at": ensure_utc(lease.expires_at).isoformat(),
        "expected_gpu": expected_gpu,
        "host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_key_fingerprints": expected_key_fingerprints,
    }
    operation = _new_operation(
        db,
        context,
        owner_id=managed.id,
        operation_type=worker_operation_type,
        target_type="resource_recycle_item",
        target_id=str(item.id),
        summary=operation_summary,
        payload=payload,
        idempotency_key=f"resource-restore:{restore.id}",
        risk_level=RiskLevel.HIGH,
    )
    restore.state = "RESTORING"
    item.state = "RESTORING"
    db.commit()
    try:
        result = _worker(
            worker_operation_type,
            payload=payload,
            context=context,
            idempotency_key=f"resource-restore:{restore.id}",
            timeout_seconds=180,
        )
    except HTTPException as exc:
        detail = getattr(exc, "detail", {})
        safe_detail = detail if isinstance(detail, dict) else {}
        allocation_job_id = safe_detail.get("gpu_allocation_job_id")
        allocation_uuid = safe_detail.get("gpu_allocation_uuid")
        valid_pair = (allocation_job_id is None and allocation_uuid is None) or (
            container.development_profile == GPU_DEVELOPMENT_PROFILE
            and isinstance(allocation_job_id, int)
            and not isinstance(allocation_job_id, bool)
            and allocation_job_id > 0
            and isinstance(allocation_uuid, str)
            and allocation_uuid.startswith("GPU-")
        )
        if safe_detail.get("gpu_allocation_state_known") is True and valid_pair:
            container.gpu_allocation_job_id = allocation_job_id
            container.gpu_allocation_uuid = allocation_uuid
        rollback_ok = (
            safe_detail.get("rollback_status") == "ROLLED_BACK"
            and safe_detail.get("gpu_allocation_state_known", True) is True
            and allocation_job_id is None
            and allocation_uuid is None
        )
        _apply_restore_failure_state(
            db,
            operation=operation,
            restore=restore,
            item=item,
            managed=managed,
            container=container,
            suspended_keys=suspended_keys,
            code=str(safe_detail.get("code", "RESTORE_WORKER_FAILED")),
            rollback_ok=rollback_ok,
        )
        _audit(
            db,
            request,
            context,
            event_type="RESOURCE_RESTORE_FAILED",
            object_type="resource_restore_request",
            object_id=str(restore.id),
            result="FAILED",
            metadata={
                "error_code": operation.error_code,
                "rollback_status": operation.rollback_status,
            },
        )
        db.commit()
        raise
    observed_fingerprints = result.get("container_key_fingerprints")
    allocation_job_id = result.get("gpu_allocation_job_id")
    allocation_uuid = result.get("gpu_allocation_uuid")
    allocation_valid = allocation_job_id is None and allocation_uuid is None
    if (
        result.get("container_state") != "RUNNING"
        or result.get("container_gpu") != expected_gpu
        or not allocation_valid
        or result.get("container_key_state") != "INSTALLED"
        or not isinstance(observed_fingerprints, list)
        or sorted(str(fingerprint) for fingerprint in observed_fingerprints)
        != expected_key_fingerprints
    ):
        rollback_ok = _rollback_restored_resource(
            payload=payload,
            worker_result=result,
            context=context,
            restore_id=restore.id,
            recycled_lease_id=recycled_lease.id,
            recycled_lease_starts_at=recycled_lease.starts_at,
            recycled_lease_expires_at=recycled_lease.expires_at,
        )
        _apply_restore_failure_state(
            db,
            operation=operation,
            restore=restore,
            item=item,
            managed=managed,
            container=container,
            suspended_keys=suspended_keys,
            code="RESTORE_POSTCONDITION_FAILED",
            rollback_ok=rollback_ok,
        )
        _audit(
            db,
            request,
            context,
            event_type="RESOURCE_RESTORE_FAILED",
            object_type="resource_restore_request",
            object_id=str(restore.id),
            result="FAILED",
            metadata={
                "error_code": "RESTORE_POSTCONDITION_FAILED",
                "rollback_status": operation.rollback_status,
            },
        )
        db.commit()
        message = (
            "恢复安全后置条件失败；环境已返回回收站"
            if rollback_ok
            else "恢复安全后置条件失败；自动回滚未完成，需要人工检查"
        )
        raise _error(409, "RESTORE_POSTCONDITION_FAILED", message)
    try:
        db.add(lease)
        db.flush()
        restore.state = "RESTORED"
        restore.restored_lease_id = lease.id
        item.state = "RESTORED"
        item.restored_at = utcnow()
        managed.compute_environment_state = "ACTIVE"
        container.observed_state = "RUNNING"
        container.desired_state = "RUNNING"
        container.gpu_allocation_job_id = allocation_job_id
        container.gpu_allocation_uuid = allocation_uuid
        for key in suspended_keys:
            key.container_install_state = "INSTALLED"
        storage = db.scalar(
            select(PortalStorageResource).where(
                PortalStorageResource.owner_managed_user_id == managed.id
            )
        )
        if storage is not None:
            storage.state = "ACTIVE"
        operation.status = OperationStatus.SUCCEEDED
        operation.worker_execution_id = str(result.get("request_id", ""))[:64] or None
        operation.finished_at = utcnow()
        operation.result_summary = "Recoverable environment restored with Host access disabled"
        _audit(
            db,
            request,
            context,
            event_type="RESOURCE_RESTORED",
            object_type="resource_restore_request",
            object_id=str(restore.id),
            metadata={"lease_id": str(lease.id), "host_access": "DISABLED"},
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        rollback_ok = _rollback_restored_resource(
            payload=payload,
            worker_result=result,
            context=context,
            restore_id=restore.id,
            recycled_lease_id=recycled_lease.id,
            recycled_lease_starts_at=recycled_lease.starts_at,
            recycled_lease_expires_at=recycled_lease.expires_at,
        )
        persisted_operation = db.get(PortalOperation, operation.id)
        persisted_restore = db.get(PortalResourceRestoreRequest, restore.id)
        persisted_item = db.get(PortalResourceRecycleItem, item.id)
        persisted_managed = db.get(PortalManagedUser, managed.id)
        persisted_container = db.get(PortalContainer, container.id)
        persisted_keys = db.scalars(
            select(PortalSshKey).where(PortalSshKey.id.in_([key.id for key in suspended_keys]))
        ).all()
        if not all(
            value is not None
            for value in (
                persisted_operation,
                persisted_restore,
                persisted_item,
                persisted_managed,
                persisted_container,
            )
        ):
            raise _error(
                409,
                "RESTORE_PERSISTENCE_FAILED",
                "恢复状态提交失败；需要人工核对 Root Worker 状态",
            ) from exc
        assert persisted_operation is not None
        assert persisted_restore is not None
        assert persisted_item is not None
        assert persisted_managed is not None
        assert persisted_container is not None
        _apply_restore_failure_state(
            db,
            operation=persisted_operation,
            restore=persisted_restore,
            item=persisted_item,
            managed=persisted_managed,
            container=persisted_container,
            suspended_keys=persisted_keys,
            code="RESTORE_PERSISTENCE_FAILED",
            rollback_ok=rollback_ok,
        )
        db.commit()
        message = (
            "恢复状态提交失败；环境已返回回收站"
            if rollback_ok
            else "恢复状态提交失败；自动回滚未完成，需要人工检查"
        )
        raise _error(409, "RESTORE_PERSISTENCE_FAILED", message) from exc
    return {
        "status": "RESTORED",
        "restore_request_id": str(restore.id),
        "lease_id": str(lease.id),
    }


def _verified_workspace(
    context: AuthContext, db: Session
) -> tuple[WorkspaceResolution, dict[str, Any]]:
    resolution = resolve_workspace(db, context.user)
    evidence = _worker(
        "self.workspace.check",
        payload=resolution.worker_payload(),
        context=context,
        idempotency_key=f"self-workspace-check:{resolution.workspace_id}",
        timeout_seconds=30,
    )
    resolution.public_contract(evidence)
    return resolution, evidence


@router.get("/self/workspace")
def self_workspace(
    context: AuthContext = Depends(permission_dependency("self.storage.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    resolution, evidence = _verified_workspace(context, db)
    return {"status": "OK", "workspace": resolution.public_contract(evidence)}


@router.get("/self/workspace/check")
def self_workspace_check(
    context: AuthContext = Depends(permission_dependency("self.storage.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    resolution, _evidence = _verified_workspace(context, db)
    return {
        "status": "READY",
        "workspace_id": str(resolution.workspace_id),
        "checks": {
            "mount": "PASS",
            "permissions": "PASS",
            "storage": "PASS",
            "ownership": "PASS",
            "quota": "PASS",
            "same_inode": True,
            "required_directories": "PASS",
        },
    }


@router.get("/self/environment")
def self_environment(
    context: AuthContext = Depends(permission_dependency("self.environment.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    managed = managed_identity_for_user(db, context.user)
    lease = lease_view(db, managed.id)
    container = _container_for_owner(db, managed.id)
    storage = db.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == managed.id
        )
    )
    return {
        "status": "OK",
        "environment": {
            "state": managed.compute_environment_state,
            "gpu_max": 1,
            "host_access": "DISABLED",
            "job_submission": "AVAILABLE" if lease.get("active") else "DISABLED",
            "lease": lease,
            "container": _container_view(container, bool(lease.get("active"))),
            "storage": {
                "quota_bytes": storage.quota_bytes if storage else managed.quota_bytes,
                "state": storage.state if storage else "ACTIVE",
                "workspace": str(workspace_path(managed.uid)),
                "container_mount": str(WORKSPACE_CONTAINER_PATH),
                "default_job_workdir": str(workspace_path(managed.uid) / WORKSPACE_DEFAULT_WORKDIR),
            },
        },
    }


@router.get("/self/lease")
def self_lease(
    context: AuthContext = Depends(permission_dependency("self.lease.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    managed = managed_identity_for_user(db, context.user)
    return {"status": "OK", "lease": lease_view(db, managed.id)}


@router.post("/self/lease/renewals")
def create_renewal(
    body: LeaseRenewalCreateRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.lease.renew.request")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    managed = managed_identity_for_user(db, context.user, lock=True)
    renewal = request_renewal(
        db,
        owner_id=managed.id,
        duration_seconds=body.duration_seconds,
        idempotency_key=str(body.idempotency_key),
        approval_required=managed.lease_renewal_approval_required,
    )
    _audit(
        db,
        request,
        context,
        event_type="LEASE_RENEWAL_REQUESTED",
        object_type="lease_renewal_request",
        object_id=str(renewal.id),
        metadata={
            "duration_seconds": renewal.requested_duration_seconds,
            "approval_required": renewal.approval_required,
        },
    )
    successor = (
        db.get(PortalComputeLease, renewal.resulting_lease_id)
        if renewal.resulting_lease_id is not None
        else None
    )
    if not renewal.approval_required and renewal.state == "REQUESTED":
        try:
            renewal, successor = decide_renewal(
                db,
                request_id=renewal.id,
                decision="APPROVE",
                decided_by=None,
                comment="AUTO_APPROVED_BY_USER_RENEWAL_POLICY",
            )
        except RenewalLeaseExpiredError:
            _audit(
                db,
                request,
                context,
                event_type="LEASE_RENEWAL_CANCELLED",
                object_type="lease_renewal_request",
                object_id=str(renewal.id),
                result="DENIED",
                metadata={
                    "reason": "LEASE_EXPIRED_RESTORE_REQUIRED",
                    "approval_required": False,
                    "policy_source": "portal_managed_user",
                },
            )
            db.commit()
            raise _error(
                409,
                "LEASE_EXPIRED_RESTORE_REQUIRED",
                "租约已过期，请申请恢复",
            ) from None
        _audit(
            db,
            request,
            context,
            event_type="LEASE_RENEWAL_AUTO_APPROVED",
            object_type="lease_renewal_request",
            object_id=str(renewal.id),
            metadata={
                "approval_required": False,
                "policy_source": "portal_managed_user",
                "resulting_lease_id": str(successor.id) if successor else None,
            },
        )
    db.commit()
    return {
        "status": renewal.state,
        "renewal_request_id": str(renewal.id),
        "resulting_lease_id": str(successor.id) if successor else None,
        "approval_required": renewal.approval_required,
    }


@router.get("/self/container")
def self_container(
    context: AuthContext = Depends(permission_dependency("self.container.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    managed = managed_identity_for_user(db, context.user)
    lease = lease_view(db, managed.id)
    return {
        "status": "OK",
        "container": _container_view(
            _container_for_owner(db, managed.id), bool(lease.get("active"))
        ),
    }


@router.get("/self/container/connection")
def self_container_connection(
    context: AuthContext = Depends(permission_dependency("self.connection.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    public_access_host = get_settings().public_access_host
    managed = managed_identity_for_user(db, context.user)
    lease = lease_view(db, managed.id)
    container = _container_for_owner(db, managed.id)
    key = db.scalar(
        select(PortalSshKey).where(
            PortalSshKey.owner_managed_user_id == managed.id,
            PortalSshKey.active.is_(True),
            PortalSshKey.scope == "CONTAINER",
            PortalSshKey.container_install_state == "INSTALLED",
        )
    )
    available = bool(
        lease.get("active")
        and container.observed_state == "RUNNING"
        and key is not None
        and managed.compute_environment_state == "ACTIVE"
    )
    runtime_gpu = container_runtime_gpu_state(
        container.gpu_allocation_job_id, container.gpu_allocation_uuid
    )
    return {
        "status": "OK",
        "connection": {
            "available": available,
            "host": public_access_host,
            "port": container.ssh_port,
            "username": managed.unix_username,
            "authentication": "SSH_PUBLIC_KEY",
            "profile": container.development_profile,
            "gpu": runtime_gpu,
            "key_fingerprint": key.fingerprint_sha256 if key else None,
            "command": (
                f"ssh -i <你的私钥路径> -p {container.ssh_port} "
                f"{managed.unix_username}@{public_access_host}"
            )
            if available
            else None,
            "vscode": (
                f"Host h100-{managed.unix_username}-dev\n"
                f"    HostName {public_access_host}\n"
                f"    Port {container.ssh_port}\n"
                f"    User {managed.unix_username}\n"
                "    IdentityFile <你的私钥路径>"
            )
            if available
            else None,
        },
    }


def _container_action(
    action: str,
    body: SelfContainerActionRequest,
    request: Request,
    context: AuthContext,
    db: Session,
) -> dict[str, Any]:
    require_session_csrf(request, context)
    managed = managed_identity_for_user(db, context.user, lock=True)
    container = _container_for_owner(db, managed.id, lock=True)
    lease_id: uuid.UUID | None = None
    lease_starts_at: datetime | None = None
    lease_deadline: datetime | None = None
    if action in {"start", "restart"}:
        try:
            active, _terminal = entitlement(db, managed.id, lock=True)
        except HTTPException as exc:
            raw_detail: Any = getattr(exc, "detail", None)
            detail = raw_detail if isinstance(raw_detail, dict) else {}
            if detail.get("code") == "LEASE_INACTIVE":
                raise _error(
                    409,
                    "CONTAINER_OPERATION_DENIED_LEASE_INACTIVE",
                    "租约失效时不能启动开发容器",
                ) from exc
            raise
        lease_id = active.id
        lease_starts_at = active.starts_at
        lease_deadline = active.expires_at
        if managed.compute_environment_state != "ACTIVE":
            raise _error(409, "CONTAINER_OPERATION_DENIED_LEASE_INACTIVE", "计算环境不可用")
    else:
        latest_lease = db.scalar(
            select(PortalComputeLease)
            .where(PortalComputeLease.owner_managed_user_id == managed.id)
            .order_by(PortalComputeLease.starts_at.desc())
        )
        if latest_lease is not None:
            lease_id = latest_lease.id
            lease_starts_at = latest_lease.starts_at
            lease_deadline = latest_lease.expires_at
        elif container.gpu_count == 1 and container.gpu_allocation_job_id is not None:
            raise _error(409, "GPU_ALLOCATION_BINDING_REJECTED", "GPU 容器缺少 Lease 绑定")
    operation_type = f"container.{action}"
    key = f"self-container-{action}:{managed.id}:{body.idempotency_key}"
    existing = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.idempotency_key == key,
        )
    )
    if existing is not None:
        return {"status": existing.status, "operation_id": str(existing.id)}
    payload = {
        "managed_user_id": str(managed.id),
        "username": managed.unix_username,
        "uid": managed.uid,
        "gid": managed.gid,
        "name": container.name,
        "workspace_path": str(workspace_path(managed.uid)),
        "development_profile": container.development_profile,
        "container_gpu": container.gpu_count,
        "gpu_allocation_job_id": container.gpu_allocation_job_id,
        "gpu_allocation_uuid": container.gpu_allocation_uuid,
        "slurm_account": managed.slurm_account,
        "slurm_qos": managed.slurm_qos,
        "lease_id": str(lease_id) if lease_id else None,
        "lease_starts_at": (ensure_utc(lease_starts_at).isoformat() if lease_starts_at else None),
        "lease_expires_at": ensure_utc(lease_deadline).isoformat() if lease_deadline else None,
        "expected_gpu": container_runtime_gpu_state(
            container.gpu_allocation_job_id, container.gpu_allocation_uuid
        ),
    }
    operation = _new_operation(
        db,
        context,
        owner_id=managed.id,
        operation_type=operation_type,
        target_type="container",
        target_id=container.name,
        summary=f"用户{action}自己的开发容器",
        payload=payload,
        idempotency_key=key,
    )
    try:
        result = _worker(
            operation_type,
            payload=payload,
            context=context,
            idempotency_key=key,
            timeout_seconds=150,
        )
    except HTTPException as exc:
        operation.status = OperationStatus.FAILED
        operation.finished_at = utcnow()
        detail = getattr(exc, "detail", {})
        safe_detail = detail if isinstance(detail, dict) else {}
        operation.error_code = str(safe_detail.get("code", "WORKER_FAILED"))
        if safe_detail.get("gpu_allocation_state_known") is True:
            allocation_job_id = safe_detail.get("gpu_allocation_job_id")
            allocation_uuid = safe_detail.get("gpu_allocation_uuid")
            valid_pair = (allocation_job_id is None and allocation_uuid is None) or (
                isinstance(allocation_job_id, int)
                and allocation_job_id > 0
                and isinstance(allocation_uuid, str)
                and allocation_uuid.startswith("GPU-")
            )
            if valid_pair:
                container.gpu_allocation_job_id = allocation_job_id
                container.gpu_allocation_uuid = allocation_uuid
                if (
                    allocation_job_id is None
                    and container.development_profile != "STANDARD_8CPU_32GB"
                ):
                    container.desired_state = "STOPPED"
                    container.observed_state = "STOPPED"
        db.commit()
        raise
    expected_state = "STOPPED" if action == "stop" else "RUNNING"
    expected_gpu = "NONE"
    allocation_job_id = result.get("gpu_allocation_job_id")
    allocation_uuid = result.get("gpu_allocation_uuid")
    gpu_postcondition = allocation_job_id is None and allocation_uuid is None
    if (
        result.get("container_state") != expected_state
        or result.get("container_gpu") != expected_gpu
        or not gpu_postcondition
    ):
        operation.status = OperationStatus.FAILED
        operation.error_code = "CONTAINER_POSTCONDITION_FAILED"
        operation.finished_at = utcnow()
        db.commit()
        raise _error(409, "CONTAINER_POSTCONDITION_FAILED", "容器安全后置条件失败")
    container.observed_state = expected_state
    container.desired_state = expected_state
    container.gpu_allocation_job_id = allocation_job_id
    container.gpu_allocation_uuid = allocation_uuid
    operation.status = OperationStatus.SUCCEEDED
    operation.worker_execution_id = str(result.get("request_id", ""))[:64] or None
    operation.finished_at = utcnow()
    _audit(
        db,
        request,
        context,
        event_type=operation_type,
        object_type="container",
        object_id=str(container.id),
        metadata={
            "state": expected_state,
            "gpu": expected_gpu,
            "development_profile": container.development_profile,
            "gpu_allocation_job_id": allocation_job_id,
        },
    )
    db.commit()
    return {"status": "SUCCEEDED", "operation_id": str(operation.id), "state": expected_state}


@router.post("/self/container/start")
def start_self_container(
    body: SelfContainerActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.container.start")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _container_action("start", body, request, context, db)


@router.post("/self/container/stop")
def stop_self_container(
    body: SelfContainerActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.container.stop")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _container_action("stop", body, request, context, db)


@router.post("/self/container/restart")
def restart_self_container(
    body: SelfContainerActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.container.restart")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _container_action("restart", body, request, context, db)


def _terminal_error(exc: TerminalServiceError) -> HTTPException:
    status_code = 404 if exc.code == "TERMINAL_NOT_FOUND" else 409
    if exc.code in {"TERMINAL_WORKER_UNAVAILABLE", "TERMINAL_WORKER_EOF"}:
        status_code = 503
    return _error(status_code, exc.code, str(exc))


def _owned_terminal(
    terminal_id: str,
    context: AuthContext,
    db: Session,
    *,
    require_active: bool,
) -> tuple[Any, TerminalRecord]:
    if context.session is None:
        raise _error(403, "DELEGATED_SCOPE_DENIED", "委托测试会话不能使用网页终端")
    managed = managed_identity_for_user(db, context.user)
    try:
        parsed = uuid.UUID(terminal_id)
    except ValueError as exc:
        raise _error(404, "TERMINAL_NOT_FOUND", "网页终端不存在") from exc
    try:
        record = terminal_registry.owned(
            parsed,
            owner_managed_user_id=managed.id,
            portal_session_id=context.session.id,
        )
    except TerminalServiceError as exc:
        raise _terminal_error(exc) from exc
    if require_active:
        try:
            resources = resolve_self_compute_context(
                db,
                context.user,
                require_running_container=True,
            )
        except HTTPException as exc:
            record.worker.request_close()
            raw_detail: Any = getattr(exc, "detail", None)
            detail = raw_detail if isinstance(raw_detail, dict) else {}
            if detail.get("code") == "LEASE_INACTIVE":
                raise _error(
                    409,
                    "TERMINAL_DENIED_LEASE_INACTIVE",
                    "租约失效时不能使用网页终端",
                ) from exc
            raise _error(409, "TERMINAL_SECURITY_GATE_FAILED", "网页终端安全状态已改变") from exc
        if resources.container.id != record.container_id:
            record.worker.request_close()
            raise _error(409, "TERMINAL_SECURITY_GATE_FAILED", "网页终端安全状态已改变")
    return managed, record


@router.post("/self/container/terminal/sessions")
def create_self_terminal(
    body: SelfTerminalCreateRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.container.terminal")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if context.session is None:
        raise _error(403, "DELEGATED_SCOPE_DENIED", "委托测试会话不能使用网页终端")
    require_session_csrf(request, context)
    resources = resolve_self_compute_context(
        db,
        context.user,
        lock=True,
        require_running_container=True,
    )
    managed = resources.managed
    container = resources.container
    keys = db.scalars(
        select(PortalSshKey).where(
            PortalSshKey.owner_managed_user_id == managed.id,
            PortalSshKey.active.is_(True),
            PortalSshKey.scope == "CONTAINER",
            PortalSshKey.container_install_state == "INSTALLED",
        )
    ).all()
    fingerprints = sorted(key.fingerprint_sha256 for key in keys)
    if not fingerprints:
        raise _error(409, "CONTAINER_KEY_BINDING_REJECTED", "开发容器公钥授权未安装")
    key = f"self-container-terminal:{managed.id}:{body.idempotency_key}"
    existing = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.idempotency_key == key,
        )
    )
    if existing is not None:
        raise _error(409, "TERMINAL_IDEMPOTENCY_REPLAY", "此终端启动请求已经处理")
    payload = {
        "managed_user_id": str(managed.id),
        "username": managed.unix_username,
        "uid": managed.uid,
        "gid": managed.gid,
        "name": container.name,
        "workspace_path": str(workspace_path(managed.uid)),
        "development_profile": container.development_profile,
        "container_gpu": container.gpu_count,
        "gpu_allocation_job_id": container.gpu_allocation_job_id,
        "gpu_allocation_uuid": container.gpu_allocation_uuid,
        "slurm_account": managed.slurm_account,
        "slurm_qos": managed.slurm_qos,
        "lease_id": str(resources.active_lease.id),
        "lease_expires_at": ensure_utc(resources.terminal_lease.expires_at).isoformat(),
        "expected_gpu": container_runtime_gpu_state(
            container.gpu_allocation_job_id, container.gpu_allocation_uuid
        ),
        "host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_key_fingerprints": fingerprints,
        "cols": body.cols,
        "rows": body.rows,
    }
    operation = _new_operation(
        db,
        context,
        owner_id=managed.id,
        operation_type="self.container.terminal",
        target_type="container",
        target_id=container.name,
        summary="用户打开自己开发容器的网页终端",
        payload=payload,
        idempotency_key=key,
        risk_level=RiskLevel.MEDIUM,
    )
    db.commit()
    try:
        record = terminal_registry.open(
            payload=payload,
            owner_managed_user_id=managed.id,
            portal_user_id=context.user.id,
            portal_session_id=context.session.id,
            operation_id=operation.id,
            container_id=container.id,
            container_name=container.name,
            actor=context.user.normalized_login,
            actor_role=highest_role(context.user),
            source_ip=client_ip(request),
            user_agent=user_agent(request),
            idempotency_key=key,
        )
    except TerminalServiceError as exc:
        operation.status = OperationStatus.FAILED
        operation.finished_at = utcnow()
        operation.error_code = exc.code[:64]
        _audit(
            db,
            request,
            context,
            event_type="SELF_CONTAINER_TERMINAL_OPEN_DENIED",
            object_type="container",
            object_id=str(container.id),
            result="DENIED",
            metadata={
                "code": exc.code,
                "gpu": payload["expected_gpu"],
                "host_access": "DISABLED",
            },
        )
        db.commit()
        raise _terminal_error(exc) from exc
    operation.worker_execution_id = str(record.worker.ready.get("request_id", ""))[:64] or None
    operation.result_summary = "Container web terminal opened; input is not logged"
    _audit(
        db,
        request,
        context,
        event_type="SELF_CONTAINER_TERMINAL_OPENED",
        object_type="container",
        object_id=str(container.id),
        metadata={
            "terminal_session_id": str(record.id),
            "container": container.name,
            "uid": managed.uid,
            "gid": managed.gid,
            "gpu": payload["expected_gpu"],
            "host_access": "DISABLED",
            "transport": "AUTHENTICATED_HTTP_LONG_POLL",
            "input_logged": False,
        },
    )
    db.commit()
    return {
        "status": "RUNNING",
        "terminal": {
            "id": str(record.id),
            "operation_id": str(operation.id),
            "container": container.name,
            "username": managed.unix_username,
            "gpu": payload["expected_gpu"],
            "host_access": "DISABLED",
            "state": record.worker.state,
            "expires_at": record.expires_at.isoformat(),
            "idle_timeout_seconds": int(record.worker.ready.get("idle_timeout_seconds", 900)),
            "max_duration_seconds": int(record.worker.ready.get("max_duration_seconds", 3600)),
        },
    }


@router.get("/self/container/terminal/sessions/{terminal_id}/output")
def self_terminal_output(
    terminal_id: str,
    cursor: int = 0,
    context: AuthContext = Depends(permission_dependency("self.container.terminal")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if cursor < 0 or cursor > 2**63 - 1:
        raise _error(422, "TERMINAL_OUTPUT_CURSOR_REJECTED", "终端输出游标无效")
    _managed, record = _owned_terminal(terminal_id, context, db, require_active=True)
    try:
        output = record.worker.output(cursor)
    except TerminalServiceError as exc:
        raise _terminal_error(exc) from exc
    return {"status": "OK", "terminal_id": str(record.id), **output}


@router.post("/self/container/terminal/sessions/{terminal_id}/input")
def self_terminal_input(
    terminal_id: str,
    body: SelfTerminalInputRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.container.terminal")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    require_session_csrf(request, context)
    _managed, record = _owned_terminal(terminal_id, context, db, require_active=True)
    encoded = body.data.encode("utf-8")
    if len(encoded) > 8 * 1024:
        raise _error(422, "TERMINAL_INPUT_REJECTED", "终端输入长度无效")
    try:
        record.worker.send_input(encoded)
    except TerminalServiceError as exc:
        raise _terminal_error(exc) from exc
    return {"status": "ACCEPTED"}


@router.post("/self/container/terminal/sessions/{terminal_id}/resize")
def self_terminal_resize(
    terminal_id: str,
    body: SelfTerminalResizeRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.container.terminal")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    require_session_csrf(request, context)
    _managed, record = _owned_terminal(terminal_id, context, db, require_active=True)
    try:
        record.worker.resize(body.cols, body.rows)
    except TerminalServiceError as exc:
        raise _terminal_error(exc) from exc
    return {"status": "ACCEPTED"}


@router.delete("/self/container/terminal/sessions/{terminal_id}")
def close_self_terminal(
    terminal_id: str,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.container.terminal")),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    require_session_csrf(request, context)
    _managed, record = _owned_terminal(terminal_id, context, db, require_active=False)
    record.worker.request_close()
    return {"status": "CLOSING"}


def _validated_cli_source_path(
    source_path: str | None, username: str
) -> tuple[str | None, str, str, str]:
    if source_path is None:
        return None, "/workspace/projects", "workspace", str(WORKSPACE_DEFAULT_WORKDIR)
    path = PurePosixPath(source_path)
    raw = path.as_posix()
    if (
        not path.is_absolute()
        or raw != source_path
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise _error(422, "JOB_SOURCE_PATH_REJECTED", "脚本路径必须是规范绝对路径")
    workspace_root = PurePosixPath("/workspace")
    home_root = PurePosixPath("/home") / username
    if path != workspace_root and path.is_relative_to(workspace_root):
        scope = "workspace"
        root = workspace_root
    elif path != home_root and path.is_relative_to(home_root):
        scope = "home"
        root = home_root
    else:
        raise _error(
            422,
            "JOB_SOURCE_PATH_REJECTED",
            "脚本只能位于自己的/workspace或/home目录",
        )
    parent = path.parent
    relative = parent.relative_to(root).as_posix()
    return source_path, parent.as_posix(), scope, "." if relative == "." else relative


def _gpu_approval_view(approval: PortalJobGpuApproval | None) -> dict[str, Any] | None:
    if approval is None:
        return None
    return {
        "id": str(approval.id),
        "state": approval.state,
        "requested_gpu_count": approval.requested_gpu_count,
        "approved_gpu_count": approval.approved_gpu_count,
        "model_name": approval.model_name,
        "model_architecture": approval.model_architecture,
        "framework": approval.framework,
        "framework_version": approval.framework_version,
        "parameter_count": approval.parameter_count,
        "workload_description": approval.workload_description,
        "dataset_description": approval.dataset_description,
        "parallel_strategy": approval.parallel_strategy,
        "scaling_justification": approval.scaling_justification,
        "script_sha256": approval.script_sha256,
        "requested_at": ensure_utc(approval.requested_at).isoformat(),
        "reviewed_at": (
            ensure_utc(approval.reviewed_at).isoformat() if approval.reviewed_at else None
        ),
        "reviewed_by": str(approval.reviewed_by) if approval.reviewed_by else None,
        "decision_comment": approval.decision_comment,
    }


def _memory_approval_view(
    approval: PortalJobMemoryApproval | None,
) -> dict[str, Any] | None:
    if approval is None:
        return None
    return {
        "id": str(approval.id),
        "state": approval.state,
        "requested_memory_mb": approval.requested_memory_mb,
        "approved_memory_mb": approval.approved_memory_mb,
        "workload_description": approval.workload_description,
        "memory_breakdown": approval.memory_breakdown,
        "memory_justification": approval.memory_justification,
        "script_sha256": approval.script_sha256,
        "requested_at": ensure_utc(approval.requested_at).isoformat(),
        "reviewed_at": (
            ensure_utc(approval.reviewed_at).isoformat() if approval.reviewed_at else None
        ),
        "reviewed_by": str(approval.reviewed_by) if approval.reviewed_by else None,
        "decision_comment": approval.decision_comment,
    }


def _job_view(job: PortalJob, *, authoritative: bool = True) -> dict[str, Any]:
    workdir = job.workdir_relative_path
    if not workdir.startswith("/"):
        workdir = f"/workspace/{workdir}"
    return {
        "id": str(job.id),
        "slurm_job_id": job.slurm_job_id,
        "name": job.name,
        "state": job.state,
        "reason": job.state_reason,
        "cpus": job.requested_cpus,
        "memory_mb": job.memory_mb,
        "gpu_count": job.gpu_count,
        "gpu_approval": _gpu_approval_view(job.gpu_approval),
        "memory_approval": _memory_approval_view(job.memory_approval),
        "time_limit_seconds": job.time_limit_seconds,
        "script_path": job.script_relative_path,
        "script_snapshot_path": f"/workspace/{job.script_relative_path}",
        "source_path": job.source_path,
        "workdir": workdir,
        "stdout_path": job.stdout_relative_path,
        "stderr_path": job.stderr_relative_path,
        "lease_deadline_at": ensure_utc(job.lease_deadline_at).isoformat(),
        "created_at": ensure_utc(job.created_at).isoformat(),
        "submitted_at": ensure_utc(job.submitted_at).isoformat() if job.submitted_at else None,
        "started_at": ensure_utc(job.started_at).isoformat() if job.started_at else None,
        "finished_at": ensure_utc(job.finished_at).isoformat() if job.finished_at else None,
        "elapsed_seconds": job.elapsed_seconds,
        "exit_code": job.exit_code,
        "authoritative": authoritative,
    }


def _worker_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or value in {"Unknown", "N/A", "None"}:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return ensure_utc(parsed)


def _refresh_job(
    context: AuthContext,
    resources: SelfResourceContext,
    job: PortalJob,
    *,
    required: bool = False,
) -> bool:
    if job.slurm_job_id is None:
        return True
    if job.state in JOB_TERMINAL_STATES and job.finished_at is not None:
        return True
    managed = resources.managed
    try:
        result = call_worker(
            "self.job.status.read",
            payload={
                "portal_job_id": str(job.id),
                "managed_user_id": str(managed.id),
                "username": managed.unix_username,
                "uid": managed.uid,
                "gid": managed.gid,
                "workspace_path": resources.workspace,
                "slurm_job_id": job.slurm_job_id,
            },
            requested_by=context.user.normalized_login,
            idempotency_key=f"self-job-status:{job.id}",
            dry_run=False,
            timeout_seconds=25,
        )
    except WorkerClientError as exc:
        if required:
            raise _error(503, exc.code, "Slurm authoritative Job状态暂时不可用") from exc
        return False
    if result.get("status") != "OK" or result.get("slurm_user") != managed.unix_username:
        if required:
            raise _error(503, "JOB_STATUS_UNAVAILABLE", "Slurm authoritative Job状态暂时不可用")
        return False
    projected_state = str(result.get("job_state", job.state)).split("+", 1)[0].split(None, 1)[0]
    if projected_state in JOB_STATES:
        job.state = projected_state
    reason = result.get("state_reason")
    job.state_reason = str(reason)[:512] if reason else None
    exit_code = result.get("exit_code")
    job.exit_code = str(exit_code)[:32] if exit_code else job.exit_code
    started_at = _worker_timestamp(result.get("started_at"))
    finished_at = _worker_timestamp(result.get("finished_at"))
    if started_at is not None:
        job.started_at = started_at
    elapsed = result.get("elapsed_seconds")
    if isinstance(elapsed, int) and not isinstance(elapsed, bool) and elapsed >= 0:
        job.elapsed_seconds = elapsed
    if job.state in JOB_TERMINAL_STATES:
        job.finished_at = finished_at or job.finished_at or utcnow()
    return True


@router.get("/self/cli-auth")
def self_cli_auth(
    context: AuthContext = Depends(request_auth_context),
) -> dict[str, Any]:
    if not context.is_cli_token or context.cli_token is None:
        raise _error(401, "CLI_TOKEN_REQUIRED", "需要有效的普通用户CLI Token")
    return {
        "status": "AUTHENTICATED",
        "user": {
            "id": str(context.user.id),
            "login_name": context.user.login_name,
            "unix_username": context.user.unix_username,
            "role": "user",
        },
        "credential": {
            "id": str(context.cli_token.id),
            "label": context.cli_token.label,
            "expires_at": (
                ensure_utc(context.cli_token.expires_at).isoformat()
                if context.cli_token.expires_at
                else None
            ),
            "last_used_at": (
                ensure_utc(context.cli_token.last_used_at).isoformat()
                if context.cli_token.last_used_at
                else None
            ),
        },
    }


@router.get("/self/jobs/config")
def self_job_config(
    context: AuthContext = Depends(permission_dependency("self.jobs.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    resources = resolve_self_compute_context(db, context.user)
    username = resources.managed.unix_username
    return {
        "status": "OK",
        "defaults": JOB_DEFAULTS,
        "limits": JOB_LIMITS,
        "allowed_script_roots": ["/workspace", f"/home/{username}"],
        "username": username,
        "cli_version": "1.0.0",
        "sbatch_directives": "REJECTED",
    }


def _job_worker_payload(
    resources: SelfResourceContext,
    job: PortalJob,
    *,
    script_content: str,
    script_sha256: str,
    gpu_approval: PortalJobGpuApproval | None,
    memory_approval: PortalJobMemoryApproval | None,
) -> dict[str, Any]:
    managed = resources.managed
    binding = workspace_binding(managed.unix_username, managed.uid, managed.gid)
    _source, _logical, workdir_scope, workdir_relative = _validated_cli_source_path(
        job.source_path, managed.unix_username
    )
    if gpu_approval is None:
        slurm_qos = managed.slurm_qos
        gpu_approval_contract = None
    else:
        if (
            gpu_approval.state != "APPROVED"
            or gpu_approval.approved_gpu_count != job.gpu_count
            or gpu_approval.script_sha256 != script_sha256
            or gpu_approval.reviewed_by is None
            or gpu_approval.reviewed_at is None
        ):
            raise _error(409, "GPU_APPROVAL_STATE_INVALID", "多GPU审批状态无效")
        slurm_qos = "portal-approved-multigpu" if job.gpu_count > 1 else managed.slurm_qos
        gpu_approval_contract = {
            "approval_id": str(gpu_approval.id),
            "requested_gpu_count": gpu_approval.requested_gpu_count,
            "approved_gpu_count": job.gpu_count,
            "script_sha256": gpu_approval.script_sha256,
            "reviewed_by": str(gpu_approval.reviewed_by),
            "reviewed_at": ensure_utc(gpu_approval.reviewed_at).isoformat(),
        }
    if memory_approval is None:
        memory_approval_contract = None
    else:
        if (
            memory_approval.state != "APPROVED"
            or memory_approval.approved_memory_mb != job.memory_mb
            or memory_approval.script_sha256 != script_sha256
            or memory_approval.reviewed_by is None
            or memory_approval.reviewed_at is None
        ):
            raise _error(409, "MEMORY_APPROVAL_STATE_INVALID", "高内存审批状态无效")
        memory_approval_contract = {
            "approval_id": str(memory_approval.id),
            "requested_memory_mb": memory_approval.requested_memory_mb,
            "approved_memory_mb": job.memory_mb,
            "script_sha256": memory_approval.script_sha256,
            "reviewed_by": str(memory_approval.reviewed_by),
            "reviewed_at": ensure_utc(memory_approval.reviewed_at).isoformat(),
        }
    return {
        **resources.worker_identity(),
        "workspace_path": resources.workspace,
        "quota_root": str(binding.quota_root),
        "home_source": str(binding.canonical_home),
        "home_path": str(binding.compute_home),
        "project_id": managed.project_id,
        "quota_bytes": resources.storage.quota_bytes,
        "portal_job_id": str(job.id),
        "lease_id": str(job.lease_id),
        "name": job.name,
        "script_relative_path": job.script_relative_path,
        "script_content": script_content,
        "script_sha256": script_sha256,
        "workdir_scope": workdir_scope,
        "workdir_relative_path": workdir_relative,
        "stdout_relative_path": job.stdout_relative_path,
        "stderr_relative_path": job.stderr_relative_path,
        "cpus": job.requested_cpus,
        "memory_mb": job.memory_mb,
        "gpu_count": job.gpu_count,
        "time_limit_seconds": job.time_limit_seconds,
        "lease_deadline_at": ensure_utc(job.lease_deadline_at).isoformat(),
        "slurm_account": managed.slurm_account,
        "slurm_qos": slurm_qos,
        "max_gpu": resources.max_gpu,
        "gpu_approval": gpu_approval_contract,
        "memory_approval": memory_approval_contract,
        "image_ref": job.image_ref,
    }


@router.get("/self/jobs")
def self_jobs(
    limit: int = Query(default=20, ge=1, le=200),
    state: str | None = Query(default=None, min_length=1, max_length=32),
    context: AuthContext = Depends(permission_dependency("self.jobs.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    resources = resolve_self_compute_context(db, context.user)
    normalized_state = state.upper() if state else None
    if normalized_state is not None and normalized_state not in JOB_STATES:
        raise _error(422, "JOB_STATE_INVALID", "Job状态过滤值无效")
    rows = db.scalars(
        select(PortalJob)
        .where(PortalJob.owner_managed_user_id == resources.managed.id)
        .order_by(PortalJob.created_at.desc())
        .limit(200)
    ).all()
    views: list[dict[str, Any]] = []
    for row in rows:
        authoritative = _refresh_job(context, resources, row, required=True)
        if normalized_state is None or row.state == normalized_state:
            views.append(_job_view(row, authoritative=authoritative))
            if len(views) >= limit:
                break
    db.commit()
    return {"status": "OK", "jobs": views, "count": len(views)}


@router.post("/self/jobs")
def submit_self_job(
    body: SelfJobSubmitRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.jobs.submit")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    gpu_approval_required = body.gpu_count >= 2
    memory_approval_required = body.memory_mb > JOB_MEMORY_APPROVAL_THRESHOLD_MB
    approval_required = gpu_approval_required or memory_approval_required
    if gpu_approval_required and body.multi_gpu_request is None:
        raise _error(
            422,
            "MULTI_GPU_DETAILS_REQUIRED",
            "申请2至4张GPU必须提交完整的模型、框架、数据与并行策略说明",
        )
    if not gpu_approval_required and body.multi_gpu_request is not None:
        raise _error(422, "MULTI_GPU_DETAILS_UNEXPECTED", "0或1张GPU作业不接受多GPU审批资料")
    if memory_approval_required and body.high_memory_request is None:
        raise _error(
            422,
            "HIGH_MEMORY_DETAILS_REQUIRED",
            "申请超过32 GiB内存必须提交用途、用量拆分与必要性说明",
        )
    if not memory_approval_required and body.high_memory_request is not None:
        raise _error(422, "HIGH_MEMORY_DETAILS_UNEXPECTED", "32 GiB以内作业不接受高内存审批资料")
    if body.image_ref is not None and body.image_ref not in APPROVED_IMAGE_REFS:
        raise _error(422, "IMAGE_NOT_APPROVED", "镜像不在管理员批准清单中")
    resources = resolve_self_compute_context(db, context.user, lock=True)
    managed = resources.managed
    source_path, logical_workdir, _workdir_scope, _workdir_relative = _validated_cli_source_path(
        body.source_path, managed.unix_username
    )
    active = resources.active_lease
    terminal = resources.terminal_lease
    now = utcnow()
    remaining = int((ensure_utc(terminal.expires_at) - now).total_seconds())
    if body.time_limit_seconds > remaining:
        raise _error(422, "JOB_EXCEEDS_LEASE", "作业时限不能超过租约剩余时间")
    if body.gpu_count == 1 and resources.max_gpu < 1:
        raise _error(422, "GPU_LIMIT_EXCEEDED", "当前用户最多申请1张GPU")
    if gpu_approval_required and resources.max_gpu < 1:
        raise _error(422, "GPU_ENTITLEMENT_REQUIRED", "多GPU审批仅面向已有GPU作业权限的用户")
    operation_key = f"self-job-submit:{managed.id}:{body.idempotency_key}"
    existing = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.actor.id,
            PortalOperation.idempotency_key == operation_key,
        )
    )
    if existing is not None:
        job = db.scalar(select(PortalJob).where(PortalJob.operation_id == existing.id))
        if job is None:
            raise _error(409, "IDEMPOTENCY_CONFLICT", "幂等操作缺少作业记录")
        replay_status: str | OperationStatus = existing.status
        if job.state == "APPROVAL_PENDING":
            replay_status = "APPROVAL_PENDING"
        elif existing.status == OperationStatus.SUCCEEDED and job.slurm_job_id is not None:
            replay_status = "SUBMITTED"
        return {"status": replay_status, "job": _job_view(job)}
    job_id = uuid.uuid4()
    script_content = body.script
    if not script_content.startswith("#!"):
        script_content = f"#!/bin/bash\n{script_content}"
    script_bytes = script_content.encode("utf-8")
    if len(script_bytes) > 8 * 1024:
        raise _error(422, "JOB_SCRIPT_REJECTED", "执行脚本不能超过8 KiB")
    script_sha256 = hashlib.sha256(script_bytes).hexdigest()
    script_path = f".portal/job-scripts/{job_id}.sh"
    stdout = f"outputs/{job_id}.out"
    stderr = f"outputs/{job_id}.err"
    image_ref = body.image_ref or APPROVED_JOB_IMAGE
    operation_payload = {
        **resources.worker_identity(),
        "portal_job_id": str(job_id),
        "lease_id": str(active.id),
        "name": body.name,
        "script_relative_path": script_path,
        "script_sha256": script_sha256,
        "script_bytes": len(script_bytes),
        "source_path": source_path,
        "cpus": body.cpus,
        "memory_mb": body.memory_mb,
        "gpu_count": body.gpu_count,
        "time_limit_seconds": body.time_limit_seconds,
        "lease_deadline_at": ensure_utc(terminal.expires_at).isoformat(),
        "max_gpu": resources.max_gpu,
        "image_ref": image_ref,
        "gpu_approval_required": gpu_approval_required,
        "memory_approval_required": memory_approval_required,
    }
    if body.multi_gpu_request is not None:
        operation_payload.update(
            {
                "model_name": body.multi_gpu_request.model_name,
                "framework": body.multi_gpu_request.framework,
                "framework_version": body.multi_gpu_request.framework_version,
            }
        )
    operation = _new_operation(
        db,
        context,
        owner_id=managed.id,
        operation_type="self.job.submit",
        target_type="slurm_job",
        target_id=str(job_id),
        summary=(
            "用户申请受控资源Slurm作业，等待管理员审批"
            if approval_required
            else "用户通过Portal提交自己的Slurm作业"
        ),
        payload=operation_payload,
        idempotency_key=operation_key,
        risk_level=RiskLevel.HIGH if approval_required else RiskLevel.MEDIUM,
        status_value=(
            OperationStatus.PENDING_APPROVAL if approval_required else OperationStatus.RUNNING
        ),
    )
    job = PortalJob(
        id=job_id,
        owner_managed_user_id=managed.id,
        lease_id=active.id,
        operation_id=operation.id,
        name=body.name,
        state="APPROVAL_PENDING" if approval_required else "SUBMITTING",
        script_relative_path=script_path,
        source_path=source_path,
        workdir_relative_path=logical_workdir,
        stdout_relative_path=stdout,
        stderr_relative_path=stderr,
        requested_cpus=body.cpus,
        memory_mb=body.memory_mb,
        gpu_count=body.gpu_count,
        time_limit_seconds=body.time_limit_seconds,
        image_ref=image_ref,
        lease_deadline_at=terminal.expires_at,
    )
    db.add(job)
    db.flush()
    if body.multi_gpu_request is not None:
        gpu_approval = PortalJobGpuApproval(
            portal_job_id=job.id,
            state="PENDING",
            requested_gpu_count=body.gpu_count,
            model_name=body.multi_gpu_request.model_name,
            model_architecture=body.multi_gpu_request.model_architecture,
            framework=body.multi_gpu_request.framework,
            framework_version=body.multi_gpu_request.framework_version,
            parameter_count=body.multi_gpu_request.parameter_count,
            workload_description=body.multi_gpu_request.workload_description,
            dataset_description=body.multi_gpu_request.dataset_description,
            parallel_strategy=body.multi_gpu_request.parallel_strategy,
            scaling_justification=body.multi_gpu_request.scaling_justification,
            script_content=script_content,
            script_sha256=script_sha256,
        )
        db.add(gpu_approval)
        job.gpu_approval = gpu_approval
        _audit(
            db,
            request,
            context,
            event_type="JOB_GPU_APPROVAL_REQUESTED",
            object_type="portal_job",
            object_id=str(job.id),
            metadata={
                "requested_gpu_count": body.gpu_count,
                "model_name": body.multi_gpu_request.model_name,
                "framework": body.multi_gpu_request.framework,
                "script_sha256": script_sha256,
                "worker_called": False,
            },
        )
    if body.high_memory_request is not None:
        memory_approval = PortalJobMemoryApproval(
            portal_job_id=job.id,
            state="PENDING",
            requested_memory_mb=body.memory_mb,
            workload_description=body.high_memory_request.workload_description,
            memory_breakdown=body.high_memory_request.memory_breakdown,
            memory_justification=body.high_memory_request.memory_justification,
            script_content=script_content,
            script_sha256=script_sha256,
        )
        db.add(memory_approval)
        job.memory_approval = memory_approval
        _audit(
            db,
            request,
            context,
            event_type="JOB_MEMORY_APPROVAL_REQUESTED",
            object_type="portal_job",
            object_id=str(job.id),
            metadata={
                "requested_memory_mb": body.memory_mb,
                "script_sha256": script_sha256,
                "worker_called": False,
            },
        )
    if approval_required:
        db.commit()
        return {"status": "APPROVAL_PENDING", "job": _job_view(job)}

    payload = _job_worker_payload(
        resources,
        job,
        script_content=script_content,
        script_sha256=script_sha256,
        gpu_approval=None,
        memory_approval=None,
    )
    try:
        result = _worker(
            "self.job.submit",
            payload=payload,
            context=context,
            idempotency_key=operation_key,
            timeout_seconds=45,
        )
    except HTTPException as exc:
        job.state = "FAILED"
        operation.status = OperationStatus.FAILED
        operation.error_code = str(getattr(exc, "detail", {}).get("code", "JOB_SUBMIT_FAILED"))
        operation.finished_at = utcnow()
        _audit(
            db,
            request,
            context,
            event_type="JOB_SUBMIT_DENIED",
            object_type="portal_job",
            object_id=str(job.id),
            result="DENIED",
            metadata={"gpu_count": body.gpu_count, "time_limit_seconds": body.time_limit_seconds},
        )
        db.commit()
        raise
    slurm_job_id = result.get("slurm_job_id")
    if not isinstance(slurm_job_id, int) or result.get("slurm_user") != managed.unix_username:
        job.state = "FAILED"
        operation.status = OperationStatus.FAILED
        operation.error_code = "JOB_OWNER_POSTCONDITION_FAILED"
        operation.finished_at = utcnow()
        db.commit()
        raise _error(409, "JOB_OWNER_POSTCONDITION_FAILED", "Slurm作业所有者验证失败")
    job.slurm_job_id = slurm_job_id
    job.state = "PENDING"
    job.submitted_at = utcnow()
    operation.status = OperationStatus.SUCCEEDED
    operation.finished_at = utcnow()
    operation.worker_execution_id = str(result.get("request_id", ""))[:64] or None
    _audit(
        db,
        request,
        context,
        event_type="JOB_SUBMIT_ALLOWED",
        object_type="portal_job",
        object_id=str(job.id),
        metadata={
            "slurm_job_id": slurm_job_id,
            "gpu_count": body.gpu_count,
            "source_path": source_path,
            "workdir": logical_workdir,
        },
    )
    db.commit()
    return {"status": "SUBMITTED", "job": _job_view(job)}


def _owned_job(
    db: Session, context: AuthContext, job_id: uuid.UUID, *, lock: bool = False
) -> tuple[SelfResourceContext, PortalJob]:
    resources = resolve_self_compute_context(db, context.user, lock=lock)
    query = select(PortalJob).where(
        PortalJob.id == job_id,
        PortalJob.owner_managed_user_id == resources.managed.id,
    )
    if lock:
        query = query.with_for_update()
    job = db.scalar(query)
    if job is None:
        raise _error(404, "JOB_NOT_FOUND", "作业不存在")
    return resources, job


@router.get("/self/jobs/{job_id}")
def self_job_detail(
    job_id: uuid.UUID,
    context: AuthContext = Depends(permission_dependency("self.jobs.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    resources, job = _owned_job(db, context, job_id)
    _refresh_job(context, resources, job, required=True)
    db.commit()
    return {"status": "OK", "job": _job_view(job)}


@router.get("/self/jobs/{job_id}/logs")
def self_job_logs(
    job_id: uuid.UUID,
    context: AuthContext = Depends(permission_dependency("self.jobs.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_delegated_scope(context, "self.jobs.logs.read")
    resources, job = _owned_job(db, context, job_id)
    _refresh_job(context, resources, job, required=True)
    db.commit()
    if job.slurm_job_id is None:
        raise _error(
            409,
            "JOB_NOT_SUBMITTED",
            "作业尚未通过审批并提交到Slurm，暂时没有运行日志",
        )
    managed = resources.managed
    result = _worker(
        "self.job.logs.read",
        payload={
            "portal_job_id": str(job.id),
            "managed_user_id": str(managed.id),
            "username": managed.unix_username,
            "uid": managed.uid,
            "gid": managed.gid,
            "workspace_path": resources.workspace,
            "stdout_relative_path": job.stdout_relative_path,
            "stderr_relative_path": job.stderr_relative_path,
        },
        context=context,
        idempotency_key=f"self-job-logs:{job.id}",
    )
    return {
        "status": "OK",
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "job": _job_view(job),
    }


@router.post("/self/jobs/{job_id}/cancel")
def cancel_self_job(
    job_id: uuid.UUID,
    body: SelfContainerActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.jobs.cancel")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    resources, job = _owned_job(db, context, job_id, lock=True)
    managed = resources.managed
    if context.delegation is not None:
        operation = db.get(PortalOperation, job.operation_id)
        delegated_context = (
            operation.validated_payload.get("delegated_test_context")
            if operation is not None and isinstance(operation.validated_payload, dict)
            else None
        )
        if not isinstance(delegated_context, dict) or delegated_context.get("delegation_id") != str(
            context.delegation.id
        ):
            raise _error(
                403,
                "DELEGATED_JOB_CANCEL_DENIED",
                "委托测试会话只能取消自己创建的作业",
            )
    _refresh_job(context, resources, job, required=True)
    if job.state in JOB_TERMINAL_STATES:
        raise _error(409, "JOB_ALREADY_TERMINAL", "只能取消非终态Job")
    if job.state == "APPROVAL_PENDING" and job.slurm_job_id is None:
        approvals = [item for item in (job.gpu_approval, job.memory_approval) if item is not None]
        if not approvals or any(item.state not in {"PENDING", "APPROVED"} for item in approvals):
            raise _error(409, "JOB_APPROVAL_STATE_INVALID", "作业资源审批记录状态无效")
        for approval in approvals:
            if approval.state == "PENDING":
                approval.state = "CANCELLED"
        job.state = "CANCELLED"
        job.state_reason = "USER_CANCELLED_BEFORE_APPROVAL"
        job.finished_at = utcnow()
        operation = db.get(PortalOperation, job.operation_id)
        if operation is not None:
            operation.status = OperationStatus.CANCELLED
            operation.finished_at = utcnow()
        _audit(
            db,
            request,
            context,
            event_type="JOB_RESOURCE_APPROVAL_CANCELLED",
            object_type="portal_job",
            object_id=str(job.id),
            metadata={
                "gpu_approval": job.gpu_approval is not None,
                "memory_approval": job.memory_approval is not None,
                "worker_called": False,
                "slurm_job_created": False,
            },
        )
        db.commit()
        return {"status": "CANCELLED", "job": _job_view(job)}
    if job.slurm_job_id is None:
        raise _error(409, "JOB_NOT_SUBMITTED", "作业尚未提交到Slurm")
    key = f"self-job-cancel:{job.id}:{body.idempotency_key}"
    result = _worker(
        "self.job.cancel",
        payload={
            "portal_job_id": str(job.id),
            "managed_user_id": str(managed.id),
            "username": managed.unix_username,
            "uid": managed.uid,
            "gid": managed.gid,
            "workspace_path": resources.workspace,
            "slurm_job_id": job.slurm_job_id,
        },
        context=context,
        idempotency_key=key,
    )
    projected_state = str(result.get("job_state", "")).split("+", 1)[0].split(None, 1)[0]
    authoritative = projected_state in JOB_STATES
    if projected_state in JOB_STATES:
        job.state = projected_state
    if job.state in JOB_TERMINAL_STATES:
        job.finished_at = utcnow()
    _audit(
        db,
        request,
        context,
        event_type="self.job.cancel",
        object_type="portal_job",
        object_id=str(job.id),
        metadata={"slurm_job_id": job.slurm_job_id},
    )
    db.commit()
    return {
        "status": "CANCELLATION_REQUESTED",
        "job": _job_view(job, authoritative=authoritative),
    }


def _admin_gpu_approval_view(
    approval: PortalJobGpuApproval,
    job: PortalJob,
    managed: PortalManagedUser,
    owner: PortalUser,
) -> dict[str, Any]:
    return {
        **(_gpu_approval_view(approval) or {}),
        "portal_job_id": str(job.id),
        "owner": {
            "portal_user_id": str(owner.id),
            "login_name": owner.login_name,
            "display_name": owner.display_name,
            "managed_user_id": str(managed.id),
            "unix_username": managed.unix_username,
        },
        "job": _job_view(job, authoritative=job.slurm_job_id is not None),
    }


@router.get("/admin/job-gpu-approvals")
def admin_job_gpu_approvals(
    state: str | None = Query(default=None, max_length=16),
    context: AuthContext = Depends(permission_dependency("jobs.gpu_approval.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    normalized = state.upper() if state else None
    if normalized is not None and normalized not in {
        "PENDING",
        "APPROVED",
        "REJECTED",
        "CANCELLED",
    }:
        raise _error(422, "GPU_APPROVAL_STATE_INVALID", "多GPU审批状态过滤值无效")
    query = select(PortalJobGpuApproval).order_by(PortalJobGpuApproval.requested_at.desc())
    if normalized is not None:
        query = query.where(PortalJobGpuApproval.state == normalized)
    rows = db.scalars(query.limit(200)).all()
    views: list[dict[str, Any]] = []
    for approval in rows:
        job = db.get(PortalJob, approval.portal_job_id)
        if job is None:
            continue
        managed = db.get(PortalManagedUser, job.owner_managed_user_id)
        owner = db.get(PortalUser, managed.portal_user_id) if managed is not None else None
        if managed is None or owner is None:
            continue
        views.append(_admin_gpu_approval_view(approval, job, managed, owner))
    return {"status": "OK", "approvals": views, "count": len(views)}


@router.post("/admin/job-gpu-approvals/{approval_id}/decision")
def decide_job_gpu_approval(
    approval_id: uuid.UUID,
    body: JobGpuApprovalDecisionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("jobs.gpu_approval.review")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    require_recent_reauthentication(context)
    initial = db.get(PortalJobGpuApproval, approval_id)
    initial_job = db.get(PortalJob, initial.portal_job_id) if initial is not None else None
    if initial is None or initial_job is None:
        raise _error(404, "GPU_APPROVAL_NOT_FOUND", "多GPU审批申请不存在")
    managed = db.get(PortalManagedUser, initial_job.owner_managed_user_id)
    owner = db.get(PortalUser, managed.portal_user_id) if managed is not None else None
    if managed is None or owner is None:
        raise _error(409, "GPU_APPROVAL_OWNER_INVALID", "审批申请缺少有效的资源所有者")

    # Lock the owner resources before the request rows. Submission uses the same
    # owner-first order, avoiding a review/Lease deadlock.
    resources = resolve_self_compute_context(db, owner, lock=True)
    job = db.scalar(select(PortalJob).where(PortalJob.id == initial_job.id).with_for_update())
    if job is None:
        raise _error(404, "GPU_APPROVAL_NOT_FOUND", "多GPU审批申请不存在")
    approval = db.scalar(
        select(PortalJobGpuApproval).where(PortalJobGpuApproval.id == approval_id).with_for_update()
    )
    memory_approval = db.scalar(
        select(PortalJobMemoryApproval)
        .where(PortalJobMemoryApproval.portal_job_id == job.id)
        .with_for_update()
    )
    if approval is None:
        raise _error(404, "GPU_APPROVAL_NOT_FOUND", "多GPU审批申请不存在")
    decision_key = f"{approval.id}:{body.idempotency_key}"
    if approval.decision_idempotency_key == decision_key:
        return {
            "status": approval.state,
            "idempotent_replay": True,
            "approval": _admin_gpu_approval_view(approval, job, managed, owner),
        }
    if approval.state != "PENDING" or job.state != "APPROVAL_PENDING":
        raise _error(409, "GPU_APPROVAL_ALREADY_DECIDED", "多GPU审批申请已被处理")
    if job.slurm_job_id is not None:
        raise _error(409, "GPU_APPROVAL_SLURM_CONFLICT", "待审批作业不应存在Slurm Job")

    now = utcnow()
    operation = db.get(PortalOperation, job.operation_id)
    if body.decision == "REJECT":
        if body.approved_gpu_count is not None:
            raise _error(422, "GPU_APPROVAL_COUNT_UNEXPECTED", "驳回时不得填写批准GPU数量")
        approval.state = "REJECTED"
        approval.reviewed_at = now
        approval.reviewed_by = context.actor.id
        approval.decision_comment = body.comment
        approval.decision_idempotency_key = decision_key
        if memory_approval is not None and memory_approval.state == "PENDING":
            memory_approval.state = "CANCELLED"
        job.state = "REJECTED"
        job.state_reason = body.comment[:512]
        job.finished_at = now
        if operation is not None:
            operation.status = OperationStatus.CANCELLED
            operation.finished_at = now
            operation.result_summary = "多GPU作业申请被管理员驳回"
        _audit(
            db,
            request,
            context,
            event_type="JOB_GPU_APPROVAL_REJECTED",
            object_type="portal_job",
            object_id=str(job.id),
            metadata={
                "approval_id": str(approval.id),
                "requested_gpu_count": approval.requested_gpu_count,
                "owner": owner.normalized_login,
                "worker_called": False,
                "slurm_job_created": False,
            },
        )
        db.commit()
        return {
            "status": "REJECTED",
            "idempotent_replay": False,
            "approval": _admin_gpu_approval_view(approval, job, managed, owner),
        }

    approved_count = body.approved_gpu_count
    if approved_count is None:
        raise _error(422, "GPU_APPROVED_COUNT_REQUIRED", "通过审批时必须填写批准GPU数量")
    if approved_count > approval.requested_gpu_count:
        raise _error(422, "GPU_APPROVED_COUNT_EXCEEDS_REQUEST", "批准GPU数量不能高于用户申请数量")
    if resources.max_gpu < 1:
        raise _error(409, "GPU_ENTITLEMENT_REQUIRED", "用户当前已不具备GPU作业权限")
    if resources.active_lease.id != job.lease_id:
        raise _error(409, "GPU_APPROVAL_LEASE_CHANGED", "原申请租约已变化，请用户重新提交")
    remaining = int((ensure_utc(resources.terminal_lease.expires_at) - now).total_seconds())
    if job.time_limit_seconds > remaining:
        raise _error(409, "JOB_EXCEEDS_LEASE", "审批时租约剩余时间已不足，请用户重新提交")
    script_bytes = approval.script_content.encode("utf-8")
    if hashlib.sha256(script_bytes).hexdigest() != approval.script_sha256:
        raise _error(409, "JOB_SCRIPT_INTEGRITY_FAILED", "待审批脚本快照完整性校验失败")

    approval.reviewed_at = now
    approval.reviewed_by = context.actor.id
    approval.decision_comment = body.comment
    approval.decision_idempotency_key = decision_key
    approval.state = "APPROVED"
    approval.approved_gpu_count = approved_count
    job.gpu_count = approved_count
    slurm_job_id = _submit_job_if_resource_approvals_complete(
        resources=resources,
        job=job,
        gpu_approval=approval,
        memory_approval=memory_approval,
        operation=operation,
        context=context,
        now=now,
    )
    _audit(
        db,
        request,
        context,
        event_type="JOB_GPU_APPROVAL_APPROVED",
        object_type="portal_job",
        object_id=str(job.id),
        metadata={
            "approval_id": str(approval.id),
            "requested_gpu_count": approval.requested_gpu_count,
            "approved_gpu_count": approved_count,
            "owner": owner.normalized_login,
            "slurm_job_id": slurm_job_id,
            "script_sha256": approval.script_sha256,
            "all_approvals_complete": slurm_job_id is not None,
        },
    )
    db.commit()
    return {
        "status": "APPROVED",
        "idempotent_replay": False,
        "approval": _admin_gpu_approval_view(approval, job, managed, owner),
    }


def _admin_memory_approval_view(
    approval: PortalJobMemoryApproval,
    job: PortalJob,
    managed: PortalManagedUser,
    owner: PortalUser,
) -> dict[str, Any]:
    return {
        **(_memory_approval_view(approval) or {}),
        "portal_job_id": str(job.id),
        "owner": {
            "portal_user_id": str(owner.id),
            "login_name": owner.login_name,
            "display_name": owner.display_name,
            "managed_user_id": str(managed.id),
            "unix_username": managed.unix_username,
        },
        "job": _job_view(job, authoritative=job.slurm_job_id is not None),
    }


def _submit_job_if_resource_approvals_complete(
    *,
    resources: SelfResourceContext,
    job: PortalJob,
    gpu_approval: PortalJobGpuApproval | None,
    memory_approval: PortalJobMemoryApproval | None,
    operation: PortalOperation | None,
    context: AuthContext,
    now: datetime,
) -> int | None:
    approvals = [item for item in (gpu_approval, memory_approval) if item is not None]
    if not approvals:
        raise _error(409, "JOB_APPROVAL_STATE_INVALID", "待审批作业缺少资源审批记录")
    if any(item.state == "PENDING" for item in approvals):
        return None
    if any(item.state != "APPROVED" for item in approvals):
        raise _error(409, "JOB_APPROVAL_STATE_INVALID", "作业资源审批状态不允许提交")

    script_content = approvals[0].script_content
    script_sha256 = approvals[0].script_sha256
    if any(
        item.script_content != script_content or item.script_sha256 != script_sha256
        for item in approvals[1:]
    ):
        raise _error(409, "JOB_SCRIPT_INTEGRITY_FAILED", "联合审批的脚本快照不一致")
    if hashlib.sha256(script_content.encode("utf-8")).hexdigest() != script_sha256:
        raise _error(409, "JOB_SCRIPT_INTEGRITY_FAILED", "待审批脚本快照完整性校验失败")

    job.state = "SUBMITTING"
    job.state_reason = None
    payload = _job_worker_payload(
        resources,
        job,
        script_content=script_content,
        script_sha256=script_sha256,
        gpu_approval=gpu_approval,
        memory_approval=memory_approval,
    )
    result = _worker(
        "self.job.submit",
        payload=payload,
        context=context,
        idempotency_key=f"job-resource-approval-submit:{job.id}",
        timeout_seconds=45,
    )
    slurm_job_id = result.get("slurm_job_id")
    if (
        not isinstance(slurm_job_id, int)
        or result.get("slurm_user") != resources.managed.unix_username
    ):
        raise _error(409, "JOB_OWNER_POSTCONDITION_FAILED", "Slurm作业所有者验证失败")

    job.slurm_job_id = slurm_job_id
    job.state = "PENDING"
    job.submitted_at = now
    if operation is not None:
        operation.status = OperationStatus.SUCCEEDED
        operation.approved_by = context.actor.id
        operation.approved_at = now
        operation.started_at = now
        operation.finished_at = now
        operation.worker_execution_id = str(result.get("request_id", ""))[:64] or None
        operation.result_summary = f"所有必需资源审批完成并提交Slurm Job {slurm_job_id}"
        operation.validated_payload = {
            **operation.validated_payload,
            "gpu_approval_id": str(gpu_approval.id) if gpu_approval else None,
            "approved_gpu_count": gpu_approval.approved_gpu_count if gpu_approval else None,
            "memory_approval_id": str(memory_approval.id) if memory_approval else None,
            "approved_memory_mb": (memory_approval.approved_memory_mb if memory_approval else None),
            "final_reviewed_by": str(context.actor.id),
        }
    return slurm_job_id


@router.get("/admin/job-memory-approvals")
def admin_job_memory_approvals(
    state: str | None = Query(default=None, max_length=16),
    context: AuthContext = Depends(permission_dependency("jobs.memory_approval.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    normalized = state.upper() if state else None
    if normalized is not None and normalized not in {
        "PENDING",
        "APPROVED",
        "REJECTED",
        "CANCELLED",
    }:
        raise _error(422, "MEMORY_APPROVAL_STATE_INVALID", "高内存审批状态过滤值无效")
    query = select(PortalJobMemoryApproval).order_by(PortalJobMemoryApproval.requested_at.desc())
    if normalized is not None:
        query = query.where(PortalJobMemoryApproval.state == normalized)
    rows = db.scalars(query.limit(200)).all()
    views: list[dict[str, Any]] = []
    for approval in rows:
        job = db.get(PortalJob, approval.portal_job_id)
        if job is None:
            continue
        managed = db.get(PortalManagedUser, job.owner_managed_user_id)
        owner = db.get(PortalUser, managed.portal_user_id) if managed is not None else None
        if managed is None or owner is None:
            continue
        views.append(_admin_memory_approval_view(approval, job, managed, owner))
    return {"status": "OK", "approvals": views, "count": len(views)}


@router.post("/admin/job-memory-approvals/{approval_id}/decision")
def decide_job_memory_approval(
    approval_id: uuid.UUID,
    body: JobMemoryApprovalDecisionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("jobs.memory_approval.review")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    require_recent_reauthentication(context)
    initial = db.get(PortalJobMemoryApproval, approval_id)
    initial_job = db.get(PortalJob, initial.portal_job_id) if initial is not None else None
    if initial is None or initial_job is None:
        raise _error(404, "MEMORY_APPROVAL_NOT_FOUND", "高内存审批申请不存在")
    managed = db.get(PortalManagedUser, initial_job.owner_managed_user_id)
    owner = db.get(PortalUser, managed.portal_user_id) if managed is not None else None
    if managed is None or owner is None:
        raise _error(409, "MEMORY_APPROVAL_OWNER_INVALID", "审批申请缺少有效的资源所有者")

    resources = resolve_self_compute_context(db, owner, lock=True)
    job = db.scalar(select(PortalJob).where(PortalJob.id == initial_job.id).with_for_update())
    if job is None:
        raise _error(404, "MEMORY_APPROVAL_NOT_FOUND", "高内存审批申请不存在")
    gpu_approval = db.scalar(
        select(PortalJobGpuApproval)
        .where(PortalJobGpuApproval.portal_job_id == job.id)
        .with_for_update()
    )
    approval = db.scalar(
        select(PortalJobMemoryApproval)
        .where(PortalJobMemoryApproval.id == approval_id)
        .with_for_update()
    )
    if approval is None:
        raise _error(404, "MEMORY_APPROVAL_NOT_FOUND", "高内存审批申请不存在")
    decision_key = f"{approval.id}:{body.idempotency_key}"
    if approval.decision_idempotency_key == decision_key:
        return {
            "status": approval.state,
            "idempotent_replay": True,
            "approval": _admin_memory_approval_view(approval, job, managed, owner),
        }
    if approval.state != "PENDING" or job.state != "APPROVAL_PENDING":
        raise _error(409, "MEMORY_APPROVAL_ALREADY_DECIDED", "高内存审批申请已被处理")
    if job.slurm_job_id is not None:
        raise _error(409, "MEMORY_APPROVAL_SLURM_CONFLICT", "待审批作业不应存在Slurm Job")

    now = utcnow()
    operation = db.get(PortalOperation, job.operation_id)
    if body.decision == "REJECT":
        if body.approved_memory_mb is not None:
            raise _error(422, "MEMORY_APPROVAL_VALUE_UNEXPECTED", "驳回时不得填写批准内存")
        approval.state = "REJECTED"
        approval.reviewed_at = now
        approval.reviewed_by = context.actor.id
        approval.decision_comment = body.comment
        approval.decision_idempotency_key = decision_key
        if gpu_approval is not None and gpu_approval.state == "PENDING":
            gpu_approval.state = "CANCELLED"
        job.state = "REJECTED"
        job.state_reason = body.comment[:512]
        job.finished_at = now
        if operation is not None:
            operation.status = OperationStatus.CANCELLED
            operation.finished_at = now
            operation.result_summary = "高内存作业申请被管理员驳回"
        _audit(
            db,
            request,
            context,
            event_type="JOB_MEMORY_APPROVAL_REJECTED",
            object_type="portal_job",
            object_id=str(job.id),
            metadata={
                "approval_id": str(approval.id),
                "requested_memory_mb": approval.requested_memory_mb,
                "owner": owner.normalized_login,
                "worker_called": False,
                "slurm_job_created": False,
            },
        )
        db.commit()
        return {
            "status": "REJECTED",
            "idempotent_replay": False,
            "approval": _admin_memory_approval_view(approval, job, managed, owner),
        }

    approved_memory = body.approved_memory_mb
    if approved_memory is None:
        raise _error(422, "MEMORY_APPROVED_VALUE_REQUIRED", "通过审批时必须填写批准内存")
    if approved_memory > approval.requested_memory_mb:
        raise _error(422, "MEMORY_APPROVED_VALUE_EXCEEDS_REQUEST", "批准内存不能高于用户申请")
    if resources.active_lease.id != job.lease_id:
        raise _error(409, "MEMORY_APPROVAL_LEASE_CHANGED", "原申请租约已变化，请用户重新提交")
    remaining = int((ensure_utc(resources.terminal_lease.expires_at) - now).total_seconds())
    if job.time_limit_seconds > remaining:
        raise _error(409, "JOB_EXCEEDS_LEASE", "审批时租约剩余时间已不足，请用户重新提交")
    if (
        hashlib.sha256(approval.script_content.encode("utf-8")).hexdigest()
        != approval.script_sha256
    ):
        raise _error(409, "JOB_SCRIPT_INTEGRITY_FAILED", "待审批脚本快照完整性校验失败")

    approval.state = "APPROVED"
    approval.approved_memory_mb = approved_memory
    approval.reviewed_at = now
    approval.reviewed_by = context.actor.id
    approval.decision_comment = body.comment
    approval.decision_idempotency_key = decision_key
    job.memory_mb = approved_memory
    slurm_job_id = _submit_job_if_resource_approvals_complete(
        resources=resources,
        job=job,
        gpu_approval=gpu_approval,
        memory_approval=approval,
        operation=operation,
        context=context,
        now=now,
    )
    _audit(
        db,
        request,
        context,
        event_type="JOB_MEMORY_APPROVAL_APPROVED",
        object_type="portal_job",
        object_id=str(job.id),
        metadata={
            "approval_id": str(approval.id),
            "requested_memory_mb": approval.requested_memory_mb,
            "approved_memory_mb": approved_memory,
            "owner": owner.normalized_login,
            "slurm_job_id": slurm_job_id,
            "script_sha256": approval.script_sha256,
            "all_approvals_complete": slurm_job_id is not None,
        },
    )
    db.commit()
    return {
        "status": "APPROVED",
        "idempotent_replay": False,
        "approval": _admin_memory_approval_view(approval, job, managed, owner),
    }


@router.get("/self/storage")
def self_storage(
    context: AuthContext = Depends(permission_dependency("self.storage.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    managed = managed_identity_for_user(db, context.user)
    storage = db.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == managed.id
        )
    )
    quota = storage.quota_bytes if storage else managed.quota_bytes
    used: int | None = None
    try:
        result = call_worker(
            "self.storage.read",
            payload={
                "managed_user_id": str(managed.id),
                "username": managed.unix_username,
                "uid": managed.uid,
                "gid": managed.gid,
                "workspace_path": str(workspace_path(managed.uid)),
                "quota_root": str(
                    workspace_binding(managed.unix_username, managed.uid, managed.gid).quota_root
                ),
                "project_id": managed.project_id,
                "quota_bytes": quota,
            },
            requested_by=context.user.normalized_login,
            idempotency_key=f"self-storage-read:{managed.id}",
            dry_run=False,
            timeout_seconds=70,
        )
        if result.get("status") == "OK" and isinstance(result.get("used_bytes"), int):
            used = int(result["used_bytes"])
    except WorkerClientError:
        pass
    return {
        "status": "OK",
        "storage": {
            "root": str(workspace_path(managed.uid)),
            "paths": [
                str(WORKSPACE_CONTAINER_PATH),
                str(
                    workspace_binding(managed.unix_username, managed.uid, managed.gid).compute_home
                ),
            ],
            "quota_bytes": quota,
            "used_bytes": used,
            "available_bytes": max(0, quota - used)
            if quota is not None and used is not None
            else None,
            "state": storage.state if storage else "ACTIVE",
            "private": True,
        },
    }


@router.get("/self/recycle-bin")
def self_recycle_bin(
    context: AuthContext = Depends(permission_dependency("self.recycle.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    managed = managed_identity_for_user(db, context.user)
    rows = db.scalars(
        select(PortalResourceRecycleItem)
        .where(
            PortalResourceRecycleItem.owner_managed_user_id == managed.id,
            PortalResourceRecycleItem.state.in_(
                {"RECYCLE_BIN", "RESTORE_PENDING", "RESTORING", "FAILED"}
            ),
        )
        .order_by(PortalResourceRecycleItem.recycled_at.desc())
    ).all()
    pending_by_item = {
        restore.recycle_item_id: restore
        for restore in db.scalars(
            select(PortalResourceRestoreRequest).where(
                PortalResourceRestoreRequest.owner_managed_user_id == managed.id,
                PortalResourceRestoreRequest.state == "REQUESTED",
            )
        ).all()
    }
    return {
        "status": "OK",
        "items": [
            {
                "id": str(row.id),
                "resource_name": row.resource_name,
                "state": row.state,
                "expires_at": ensure_utc(row.expires_at).isoformat(),
                "recycled_at": ensure_utc(row.recycled_at).isoformat(),
                "data_preserved": row.data_preserved,
                "auto_permanent_delete": row.auto_permanent_delete,
                "container": "STOPPED",
                "restore_request_id": (
                    str(pending_by_item[row.id].id) if row.id in pending_by_item else None
                ),
                "approval_required": (
                    pending_by_item[row.id].approval_required
                    if row.id in pending_by_item
                    else managed.lease_renewal_approval_required
                ),
            }
            for row in rows
        ],
        "approval_required": managed.lease_renewal_approval_required,
        "auto_permanent_delete": False,
    }


@router.post("/self/recycle-bin/{item_id}/restore-requests")
def create_restore_request(
    item_id: uuid.UUID,
    body: RestoreCreateRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.restore.request")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    managed = managed_identity_for_user(db, context.user, lock=True)
    existing = db.scalar(
        select(PortalResourceRestoreRequest).where(
            PortalResourceRestoreRequest.owner_managed_user_id == managed.id,
            PortalResourceRestoreRequest.idempotency_key == str(body.idempotency_key),
        )
    )
    if existing is not None:
        if (
            existing.recycle_item_id != item_id
            or existing.requested_duration_seconds != body.duration_seconds
        ):
            raise _error(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同恢复申请")
        if existing.state != "REQUESTED":
            return {
                "status": existing.state,
                "restore_request_id": str(existing.id),
                "lease_id": str(existing.restored_lease_id)
                if existing.restored_lease_id is not None
                else None,
                "approval_required": existing.approval_required,
            }
    item = db.scalar(
        select(PortalResourceRecycleItem)
        .where(
            PortalResourceRecycleItem.id == item_id,
            PortalResourceRecycleItem.owner_managed_user_id == managed.id,
            PortalResourceRecycleItem.state.in_({"RECYCLE_BIN", "RESTORE_PENDING", "FAILED"}),
        )
        .with_for_update()
    )
    if item is None:
        raise _error(404, "RECYCLE_ITEM_NOT_FOUND", "回收资源不存在")
    if existing is None:
        pending = db.scalar(
            select(PortalResourceRestoreRequest)
            .where(
                PortalResourceRestoreRequest.owner_managed_user_id == managed.id,
                PortalResourceRestoreRequest.recycle_item_id == item.id,
                PortalResourceRestoreRequest.state == "REQUESTED",
            )
            .order_by(PortalResourceRestoreRequest.requested_at.desc())
            .limit(1)
            .with_for_update()
        )
        if pending is not None:
            if pending.requested_duration_seconds != body.duration_seconds:
                raise _error(
                    409,
                    "RESTORE_ALREADY_PENDING",
                    "已有不同期限的恢复申请等待管理员审批",
                )
            return {
                "status": "REQUESTED",
                "restore_request_id": str(pending.id),
                "lease_id": None,
                "approval_required": pending.approval_required,
            }
        approval_required = managed.lease_renewal_approval_required
        restore = PortalResourceRestoreRequest(
            owner_managed_user_id=managed.id,
            recycle_item_id=item.id,
            state="REQUESTED",
            approval_required=approval_required,
            requested_duration_seconds=body.duration_seconds,
            idempotency_key=str(body.idempotency_key),
        )
        db.add(restore)
        db.flush()
        _audit(
            db,
            request,
            context,
            event_type="RESOURCE_RESTORE_REQUESTED",
            object_type="resource_restore_request",
            object_id=str(restore.id),
            metadata={
                "duration_seconds": body.duration_seconds,
                "approval_required": approval_required,
                "initiator": "OWNER",
                "policy_source": "portal_managed_user",
            },
        )
    else:
        restore = existing
    if restore.approval_required:
        item.state = "RESTORE_PENDING"
        managed.compute_environment_state = "RESTORE_PENDING"
        db.commit()
        return {
            "status": "REQUESTED",
            "restore_request_id": str(restore.id),
            "lease_id": None,
            "approval_required": True,
        }
    restore.decided_at = utcnow()
    restore.decided_by = context.user.id
    restore.decision_comment = "AUTO_APPROVED_BY_USER_RENEWAL_POLICY"
    _audit(
        db,
        request,
        context,
        event_type="RESOURCE_RESTORE_AUTO_APPROVED",
        object_type="resource_restore_request",
        object_id=str(restore.id),
        metadata={
            "approval_required": False,
            "policy_source": "portal_managed_user",
        },
    )
    result = _execute_restore(
        db,
        request,
        context,
        restore=restore,
        item=item,
        managed=managed,
        worker_operation_type="self.resource.restore",
        operation_summary="资源所有者自助恢复可回收计算环境",
    )
    result["approval_required"] = False
    return result


@router.get("/admin/lease-renewals")
def admin_renewals(
    context: AuthContext = Depends(permission_dependency("lease.renewals.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    now = utcnow()
    rows = db.execute(
        select(
            PortalLeaseRenewalRequest,
            PortalComputeLease,
            PortalManagedUser,
            PortalUser,
        )
        .join(PortalComputeLease, PortalComputeLease.id == PortalLeaseRenewalRequest.lease_id)
        .join(
            PortalManagedUser,
            PortalManagedUser.id == PortalLeaseRenewalRequest.owner_managed_user_id,
        )
        .join(PortalUser, PortalUser.id == PortalManagedUser.portal_user_id)
        .order_by(PortalLeaseRenewalRequest.requested_at.desc())
    ).all()
    requests: list[dict[str, Any]] = []
    for renewal, lease, _managed, owner in rows:
        lease_expires_at = ensure_utc(lease.expires_at)
        actionable = (
            renewal.state == "REQUESTED"
            and lease_expires_at > now
            and lease.state in {"ACTIVE", "RENEWAL_WINDOW", "RENEWAL_PENDING"}
        )
        requests.append(
            {
                "id": str(renewal.id),
                "owner_managed_user_id": str(renewal.owner_managed_user_id),
                "username": owner.normalized_login,
                "display_name": owner.display_name,
                "lease_id": str(renewal.lease_id),
                "lease_state": lease.state,
                "lease_expires_at": lease_expires_at.isoformat(),
                "state": renewal.state,
                "approval_required": renewal.approval_required,
                "duration_seconds": renewal.requested_duration_seconds,
                "requested_at": ensure_utc(renewal.requested_at).isoformat(),
                "decided_at": (
                    ensure_utc(renewal.decided_at).isoformat()
                    if renewal.decided_at is not None
                    else None
                ),
                "decision_comment": renewal.decision_comment,
                "resulting_lease_id": (
                    str(renewal.resulting_lease_id)
                    if renewal.resulting_lease_id is not None
                    else None
                ),
                "actionable": actionable,
                "closed_reason": (
                    "LEASE_EXPIRED_RESTORE_REQUIRED"
                    if renewal.state == "REQUESTED" and not actionable
                    else None
                ),
            }
        )
    return {
        "status": "OK",
        "requests": requests,
        "count": len(requests),
        "pending_count": sum(bool(row["actionable"]) for row in requests),
    }


@router.post("/admin/lease-renewals/{request_id}/decision")
def admin_decide_renewal(
    request_id: uuid.UUID,
    body: LeaseDecisionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("lease.renewals.review")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    require_recent_reauthentication(context)
    try:
        renewal, successor = decide_renewal(
            db,
            request_id=request_id,
            decision=body.decision,
            decided_by=context.user.id,
            comment=body.comment,
        )
    except RenewalLeaseExpiredError:
        _audit(
            db,
            request,
            context,
            event_type="LEASE_RENEWAL_CANCELLED",
            object_type="lease_renewal_request",
            object_id=str(request_id),
            result="DENIED",
            metadata={"reason": "LEASE_EXPIRED_RESTORE_REQUIRED"},
        )
        db.commit()
        raise _error(
            409,
            "LEASE_EXPIRED_RESTORE_REQUIRED",
            "租约已过期，请改用恢复流程",
        ) from None
    _audit(
        db,
        request,
        context,
        event_type=f"LEASE_RENEWAL_{renewal.state}",
        object_type="lease_renewal_request",
        object_id=str(renewal.id),
        metadata={"resulting_lease_id": str(successor.id) if successor else None},
    )
    db.commit()
    return {
        "status": renewal.state,
        "renewal_request_id": str(renewal.id),
        "resulting_lease_id": str(successor.id) if successor else None,
    }


def _expiry_operation(db: Session, lease: PortalComputeLease) -> PortalOperation | None:
    return db.scalar(
        select(PortalOperation)
        .where(
            PortalOperation.owner_managed_user_id == lease.owner_managed_user_id,
            PortalOperation.idempotency_key == f"lease-expire:{lease.id}",
        )
        .order_by(PortalOperation.created_at.desc())
    )


@router.get("/admin/lease-recovery-incidents")
def admin_lease_recovery_incidents(
    context: AuthContext = Depends(permission_dependency("lease.recovery")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    now = utcnow()
    rows = db.execute(
        select(PortalComputeLease, PortalManagedUser, PortalContainer)
        .join(
            PortalManagedUser,
            PortalManagedUser.id == PortalComputeLease.owner_managed_user_id,
        )
        .join(
            PortalContainer,
            PortalContainer.owner_managed_user_id == PortalManagedUser.id,
        )
        .where(
            PortalComputeLease.expires_at <= now,
            PortalComputeLease.recycled_at.is_(None),
            PortalComputeLease.state.in_(expiry_service.DUE_STATES),
        )
        .order_by(PortalComputeLease.expires_at)
    ).all()
    incidents: list[dict[str, Any]] = []
    for lease, managed, container in rows:
        operation = _expiry_operation(db, lease)
        evidence = expiry_service.cleanup_evidence(operation) if operation is not None else None
        recycle_item = db.scalar(
            select(PortalResourceRecycleItem).where(PortalResourceRecycleItem.lease_id == lease.id)
        )
        key_states = sorted(
            {
                key.container_install_state
                for key in db.scalars(
                    select(PortalSshKey).where(
                        PortalSshKey.owner_managed_user_id == managed.id,
                        PortalSshKey.active.is_(True),
                    )
                ).all()
            }
        )
        container_ssh_authorization = (
            "KEY_INSTALLED"
            if "INSTALLED" in key_states
            else (
                "SUSPENDED_BY_RECYCLE" if "SUSPENDED_BY_RECYCLE" in key_states else "NOT_INSTALLED"
            )
        )
        operation_status = (
            operation.status.value
            if operation is not None and hasattr(operation.status, "value")
            else (str(operation.status) if operation is not None else None)
        )
        manual_review_required = evidence.get("manual_review_required") if evidence else True
        recovery_available = bool(
            operation is not None
            and operation_status == OperationStatus.FAILED.value
            and manual_review_required is True
            and recycle_item is None
        )
        last_transition_at = (
            operation.finished_at or operation.started_at or operation.created_at
            if operation is not None
            else lease.expired_at or lease.recycled_at or lease.created_at
        )
        incidents.append(
            {
                "lease_id": str(lease.id),
                "portal_user_id": str(managed.portal_user_id),
                "username": managed.unix_username,
                "owner": managed.unix_username,
                "starts_at": ensure_utc(lease.starts_at).isoformat(),
                "expires_at": ensure_utc(lease.expires_at).isoformat(),
                "current_time": ensure_utc(now).isoformat(),
                "time_expired": ensure_utc(lease.expires_at) <= ensure_utc(now),
                "lease_state": lease.state,
                "recycle_state": (
                    recycle_item.state if recycle_item is not None else operation_status
                ),
                "last_transition_at": (
                    ensure_utc(last_transition_at).isoformat()
                    if last_transition_at is not None
                    else None
                ),
                "compute_environment_state": managed.compute_environment_state,
                "container_name": container.name,
                "container_desired_state": container.desired_state,
                "container_observed_state": container.observed_state,
                "connection_authorization_state": "DENIED_EXPIRED_LEASE",
                "container_ssh_authorization_state": container_ssh_authorization,
                "operation_id": str(operation.id) if operation is not None else None,
                "operation_type": operation.operation_type if operation is not None else None,
                "operation_status": operation_status,
                "operation_started_at": (
                    ensure_utc(operation.started_at).isoformat()
                    if operation is not None and operation.started_at is not None
                    else None
                ),
                "operation_finished_at": (
                    ensure_utc(operation.finished_at).isoformat()
                    if operation is not None and operation.finished_at is not None
                    else None
                ),
                "error_code": operation.error_code if operation is not None else None,
                "safe_error_message": (
                    "到期回收未完成；需要平台所有者人工确认后重试。"
                    if operation_status == OperationStatus.FAILED.value
                    else None
                ),
                "attempt_count": evidence.get("attempt_count") if evidence else None,
                "next_retry_at": evidence.get("next_retry_at") if evidence else None,
                "manual_review_required": manual_review_required,
                "recovery_available": recovery_available,
                "data_delete_allowed": False,
            }
        )
    return {"status": "OK", "incidents": incidents, "count": len(incidents)}


@router.post("/admin/compute-leases/{lease_id}/recycle-retry")
def admin_retry_lease_recycle(
    lease_id: uuid.UUID,
    body: LeaseRecoveryApplyRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("lease.recovery")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    require_recent_reauthentication(context)
    if body.confirmation != lease_id:
        raise _error(428, "LEASE_RECOVERY_CONFIRMATION_REQUIRED", "请输入准确 Lease ID 确认")
    lease = db.scalar(
        select(PortalComputeLease).where(PortalComputeLease.id == lease_id).with_for_update()
    )
    if lease is None:
        raise _error(404, "LEASE_NOT_FOUND", "计算租约不存在")
    key = f"lease-recycle-recovery:{lease.id}:{body.idempotency_key}"
    existing = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.idempotency_key == key,
        )
    )
    if existing is not None:
        return {
            "status": existing.status,
            "operation_id": str(existing.id),
            "lease_id": str(lease.id),
            "idempotent_replay": True,
        }
    if ensure_utc(lease.expires_at) > utcnow() or lease.recycled_at is not None:
        raise _error(409, "LEASE_RECOVERY_TARGET_INVALID", "租约不是待恢复的过期资源")
    prior = _expiry_operation(db, lease)
    if prior is None or prior.status != OperationStatus.FAILED:
        raise _error(409, "LEASE_RECOVERY_FAILURE_NOT_FOUND", "不存在失败的到期回收记录")
    recycle_item = db.scalar(
        select(PortalResourceRecycleItem).where(PortalResourceRecycleItem.lease_id == lease.id)
    )
    if recycle_item is not None:
        raise _error(409, "LEASE_ALREADY_RECYCLED", "租约已经进入回收站")
    now = utcnow()
    operation = PortalOperation(
        operation_type="lease.recycle.retry",
        target_type="compute_lease",
        target_id=str(lease.id),
        requested_by=context.user.id,
        owner_managed_user_id=lease.owner_managed_user_id,
        approved_by=context.user.id,
        approved_at=now,
        request_summary="platform_owner approved retry of failed lease recycle cleanup",
        validated_payload={
            "lease_id": str(lease.id),
            "failed_expiry_operation_id": str(prior.id),
            "failed_error_code": prior.error_code,
            "safe_reason": body.safe_reason,
            "data_delete_allowed": False,
        },
        idempotency_key=key,
        risk_level=RiskLevel.HIGH,
        status=OperationStatus.RUNNING,
        started_at=now,
    )
    db.add(operation)
    db.flush()
    succeeded = expiry_service.retry_failed_recycle(
        db,
        lease=lease,
        operation=operation,
        actor=context.user.normalized_login,
    )
    record_audit(
        db,
        event_type=("LEASE_RECOVERY_APPLIED" if succeeded else "LEASE_RECOVERY_FAILED"),
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="compute_lease",
        object_id=str(lease.id),
        operation_id=operation.id,
        result="SUCCESS" if succeeded else "FAILED",
        metadata={
            "prior_operation_id": str(prior.id),
            "container_delete": False,
            "data_delete": False,
        },
    )
    db.commit()
    response = {
        "status": operation.status,
        "operation_id": str(operation.id),
        "lease_id": str(lease.id),
        "idempotent_replay": False,
    }
    if not succeeded:
        raise _error(409, operation.error_code or "LEASE_RECOVERY_FAILED", "租约回收恢复失败")
    return response


@router.get("/admin/restore-requests")
def admin_restore_requests(
    context: AuthContext = Depends(permission_dependency("lease.renewals.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.execute(
        select(PortalResourceRestoreRequest, PortalManagedUser, PortalResourceRecycleItem)
        .join(
            PortalManagedUser,
            PortalManagedUser.id == PortalResourceRestoreRequest.owner_managed_user_id,
        )
        .join(
            PortalResourceRecycleItem,
            PortalResourceRecycleItem.id == PortalResourceRestoreRequest.recycle_item_id,
        )
        .order_by(PortalResourceRestoreRequest.requested_at.desc())
    ).all()
    return {
        "status": "OK",
        "requests": [
            {
                "id": str(restore.id),
                "username": managed.unix_username,
                "resource_name": item.resource_name,
                "state": restore.state,
                "approval_required": restore.approval_required,
                "duration_seconds": restore.requested_duration_seconds,
                "requested_at": ensure_utc(restore.requested_at).isoformat(),
                "decided_at": (
                    ensure_utc(restore.decided_at).isoformat()
                    if restore.decided_at is not None
                    else None
                ),
            }
            for restore, managed, item in rows
        ],
        "count": len(rows),
    }


@router.post("/admin/restore-requests/{request_id}/decision")
def admin_decide_restore(
    request_id: uuid.UUID,
    body: RestoreDecisionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("lease.renewals.review")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    require_recent_reauthentication(context)
    restore = db.scalar(
        select(PortalResourceRestoreRequest)
        .where(PortalResourceRestoreRequest.id == request_id)
        .with_for_update()
    )
    if restore is None:
        raise _error(404, "RESTORE_REQUEST_NOT_FOUND", "恢复申请不存在")
    if restore.state != "REQUESTED":
        raise _error(409, "RESTORE_ALREADY_DECIDED", "恢复申请已处理")
    item = db.scalar(
        select(PortalResourceRecycleItem)
        .where(PortalResourceRecycleItem.id == restore.recycle_item_id)
        .with_for_update()
    )
    if (
        item is None
        or item.owner_managed_user_id != restore.owner_managed_user_id
        or item.state != "RESTORE_PENDING"
    ):
        raise _error(409, "RESTORE_OWNERSHIP_MISMATCH", "恢复资源所有权不一致")
    from h100_portal_api.models import PortalManagedUser

    managed = db.scalar(
        select(PortalManagedUser)
        .where(PortalManagedUser.id == restore.owner_managed_user_id)
        .with_for_update()
    )
    if managed is None:
        raise _error(409, "MANAGED_IDENTITY_NOT_FOUND", "受管身份不存在")
    restore.decided_at = utcnow()
    restore.decided_by = context.user.id
    restore.decision_comment = body.comment
    if body.decision == "REJECT":
        restore.state = "REJECTED"
        item.state = "RECYCLE_BIN"
        managed.compute_environment_state = "RECYCLED"
        _audit(
            db,
            request,
            context,
            event_type="RESOURCE_RESTORE_REJECTED",
            object_type="resource_restore_request",
            object_id=str(restore.id),
        )
        db.commit()
        return {"status": "REJECTED", "restore_request_id": str(restore.id)}
    return _execute_restore(
        db,
        request,
        context,
        restore=restore,
        item=item,
        managed=managed,
        worker_operation_type="resource.restore",
        operation_summary="管理员批准恢复用户的待审批计算环境",
    )
