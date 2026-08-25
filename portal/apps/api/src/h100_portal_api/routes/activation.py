import uuid
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request
from h100_portal_contracts.workspace import (
    CPU_DEVELOPMENT_PROFILE,
    GPU_DEVELOPMENT_PROFILE,
    workspace_path,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
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
from h100_portal_api.enums import OnboardingState, OperationStatus, RiskLevel
from h100_portal_api.lease_service import create_lease
from h100_portal_api.models import (
    PortalComputeLease,
    PortalComputeResourceRequest,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalOperationEvent,
    PortalProvisionPlan,
    PortalSshKey,
    PortalStorageResource,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.rbac import highest_role
from h100_portal_api.runtime_identity import deployment_version
from h100_portal_api.schemas import SelfComputeActivateRequest
from h100_portal_api.worker_client import WorkerClientError, call_worker

router = APIRouter(tags=["self-service"])

LEASE_SECONDS = 96 * 60 * 60
ACTIVATION_RESUME_AFTER = timedelta(minutes=15)
TERMINAL_OPERATION_STATES = {
    OperationStatus.SUCCEEDED,
    OperationStatus.FAILED,
    OperationStatus.ROLLED_BACK,
    OperationStatus.CANCELLED,
}


class ActivationTarget(NamedTuple):
    account: PortalUser
    managed: PortalManagedUser
    request: PortalComputeResourceRequest
    plan: PortalProvisionPlan
    stage_operation: PortalOperation
    container: PortalContainer
    storage: PortalStorageResource
    keys: list[PortalSshKey]


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _audit(
    db: Session,
    request: Request,
    context: AuthContext,
    *,
    event_type: str,
    operation_id: uuid.UUID,
    managed_user_id: uuid.UUID,
    result: str,
    metadata: dict[str, Any],
) -> None:
    record_audit(
        db,
        event_type=event_type,
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="compute_identity",
        object_id=str(managed_user_id),
        operation_id=operation_id,
        result=result,
        metadata=metadata,
    )


def _safe_staged_container(container: PortalContainer) -> bool:
    spec = container.safe_spec if isinstance(container.safe_spec, dict) else {}
    expected_gpu = "NONE" if container.gpu_count == 0 else "SLURM_ALLOCATED_1"
    return bool(
        container.development_profile in {CPU_DEVELOPMENT_PROFILE, GPU_DEVELOPMENT_PROFILE}
        and container.gpu_count
        == (1 if container.development_profile == GPU_DEVELOPMENT_PROFILE else 0)
        and container.gpu_allocation_job_id is None
        and container.gpu_allocation_uuid is None
        and spec.get("gpu") == expected_gpu
        and spec.get("privileged") is False
        and spec.get("host_network") is False
        and spec.get("host_pid") is False
        and spec.get("host_ipc") is False
        and spec.get("docker_socket") is False
        and spec.get("munge") is False
        and spec.get("cpus") == 8
        and spec.get("memory_gb") == 32
        and spec.get("pids_limit") == 4096
        and spec.get("host_authorized_keys") == "ABSENT"
        and spec.get("container_authorized_keys") == "ABSENT"
        and spec.get("lease_state") == "NOT_STARTED"
    )


def _load_staged_target(db: Session, context: AuthContext, *, lock: bool) -> ActivationTarget:
    managed_query = select(PortalManagedUser).where(
        PortalManagedUser.portal_user_id == context.user.id
    )
    if lock:
        managed_query = managed_query.with_for_update()
    managed = db.scalar(managed_query)
    if managed is None:
        raise _error(404, "MANAGED_IDENTITY_NOT_FOUND", "当前账号没有受管计算身份")

    account_query = select(PortalUser).where(PortalUser.id == context.user.id)
    if lock:
        account_query = account_query.with_for_update()
    account = db.scalar(account_query)
    if account is None:
        raise _error(404, "ACCOUNT_NOT_FOUND", "当前账号不存在")

    request_query = (
        select(PortalComputeResourceRequest)
        .where(
            PortalComputeResourceRequest.portal_account_id == context.user.id,
            PortalComputeResourceRequest.managed_user_id == managed.id,
        )
        .order_by(PortalComputeResourceRequest.created_at.desc())
    )
    if lock:
        request_query = request_query.with_for_update()
    compute_request = db.scalar(request_query)
    if compute_request is None or compute_request.provision_plan_id is None:
        raise _error(409, "STAGED_COMPUTE_REQUEST_NOT_FOUND", "未找到已完成 Stage 的计算申请")

    plan_query = select(PortalProvisionPlan).where(
        PortalProvisionPlan.id == compute_request.provision_plan_id,
        PortalProvisionPlan.request_id == compute_request.id,
        PortalProvisionPlan.portal_account_id == context.user.id,
    )
    if lock:
        plan_query = plan_query.with_for_update()
    plan = db.scalar(plan_query)
    if plan is None:
        raise _error(409, "STAGED_ATTEMPT_NOT_FOUND", "当前 Provision Attempt 绑定不完整")

    stage_operations = list(
        db.scalars(
            select(PortalOperation)
            .where(
                PortalOperation.operation_type == "compute.provision.stage",
                PortalOperation.target_id == str(compute_request.id),
                PortalOperation.status == OperationStatus.SUCCEEDED,
            )
            .order_by(PortalOperation.finished_at.desc())
        ).all()
    )
    stage_operation = next(
        (
            operation
            for operation in stage_operations
            if operation.owner_managed_user_id == managed.id
            and isinstance(operation.validated_payload, dict)
            and operation.validated_payload.get("plan_id") == str(plan.id)
            and operation.validated_payload.get("request_id") == str(compute_request.id)
        ),
        None,
    )
    if stage_operation is None:
        raise _error(409, "STAGED_OPERATION_NOT_FOUND", "当前 Provision Stage 绑定不完整")
    stage_payload = stage_operation.validated_payload
    if not isinstance(stage_payload.get("dry_run_operation_id"), str):
        raise _error(409, "STAGED_OPERATION_NOT_FOUND", "当前 Provision Dry-run 绑定不完整")

    container_query = select(PortalContainer).where(
        PortalContainer.owner_managed_user_id == managed.id,
        PortalContainer.managed_user_id == managed.id,
    )
    storage_query = select(PortalStorageResource).where(
        PortalStorageResource.owner_managed_user_id == managed.id
    )
    key_query = (
        select(PortalSshKey)
        .where(
            PortalSshKey.owner_managed_user_id == managed.id,
            PortalSshKey.managed_user_id == managed.id,
            PortalSshKey.active.is_(True),
            PortalSshKey.state.in_({"VALIDATED", "INSTALLED"}),
            PortalSshKey.scope == "CONTAINER",
        )
        .order_by(PortalSshKey.created_at, PortalSshKey.id)
    )
    if lock:
        container_query = container_query.with_for_update()
        storage_query = storage_query.with_for_update()
        key_query = key_query.with_for_update()
    container = db.scalar(container_query)
    storage = db.scalar(storage_query)
    keys = list(db.scalars(key_query).all())
    if container is None or storage is None:
        raise _error(409, "STAGED_RESOURCES_INCOMPLETE", "已 Stage 的计算资源绑定不完整")
    if not keys:
        raise _error(409, "SSH_KEY_REQUIRED", "请先登记有效的开发容器 SSH 公钥")

    any_lease = db.scalar(
        select(PortalComputeLease.id).where(PortalComputeLease.owner_managed_user_id == managed.id)
    )
    if any_lease is not None:
        raise _error(409, "LEASE_ALREADY_EXISTS", "计算身份已有 Lease，不能重复激活")

    state_valid = (
        account.resource_onboarding_state == OnboardingState.STAGED
        and managed.onboarding_state == OnboardingState.STAGED
        and managed.compute_environment_state == "STAGED"
        and managed.shell == "/usr/sbin/nologin"
        and managed.host_access_state == "DISABLED_BY_PLATFORM_POLICY"
        and managed.ssh_key_state in {"VALIDATED", "REQUIRED_BEFORE_ACTIVATION"}
        and managed.ssh_key_count >= len(keys) >= 1
        and managed.unix_username == compute_request.username == plan.username
        and managed.uid == plan.uid
        and managed.gid == plan.gid
        and managed.project_id == plan.project_id
        and managed.container_name == plan.container_name == container.name
        and managed.container_port == plan.container_ssh_port == container.ssh_port
        and compute_request.status == "KEY_ENROLLMENT_PENDING"
        and plan.state == "STAGED"
        and plan.lease_seconds == LEASE_SECONDS
        and plan.lease_state == "NOT_STARTED"
        and plan.host_ssh_enabled is False
        and plan.shell == "/usr/sbin/nologin"
        and plan.password_state == "LOCKED"  # noqa: S105 -- lifecycle state, not a secret
        and plan.container_profile == container.development_profile
        and plan.container_gpu == container.gpu_count
        and plan.container_cpus == 8
        and plan.container_memory_gb == 32
        and plan.container_pids_limit == 4096
        and container.desired_state == "STOPPED"
        and container.observed_state == "STOPPED"
        and storage.state == "STAGED"
        and storage.root_path == str(workspace_path(managed.uid))
        and _safe_staged_container(container)
        and all(
            key.host_install_state == "NOT_INSTALLED"
            and key.container_install_state == "NOT_INSTALLED"
            for key in keys
        )
    )
    if not state_valid:
        raise _error(
            409,
            "ACTIVATION_PREREQUISITES_FAILED",
            "计算环境尚未满足安全激活条件",
        )
    return ActivationTarget(
        account,
        managed,
        compute_request,
        plan,
        stage_operation,
        container,
        storage,
        keys,
    )


def _active_response(db: Session, managed: PortalManagedUser) -> dict[str, Any]:
    lease = db.scalar(
        select(PortalComputeLease)
        .where(PortalComputeLease.owner_managed_user_id == managed.id)
        .order_by(PortalComputeLease.starts_at.desc())
    )
    container = db.scalar(
        select(PortalContainer).where(PortalContainer.owner_managed_user_id == managed.id)
    )
    if (
        lease is None
        or container is None
        or lease.state not in {"ACTIVE", "RENEWAL_WINDOW", "RENEWAL_PENDING"}
        or ensure_utc(lease.expires_at) <= utcnow()
        or container.observed_state != "RUNNING"
        or managed.compute_environment_state != "ACTIVE"
    ):
        raise _error(409, "ACTIVE_STATE_INCONSISTENT", "计算环境激活状态不完整，已安全停止续期")
    return {
        "status": "ALREADY_ACTIVE",
        "idempotent_replay": True,
        "environment_state": "ACTIVE",
        "container_state": "RUNNING",
        "ssh_state": "READY",
        "lease": {
            "id": str(lease.id),
            "state": lease.state,
            "starts_at": ensure_utc(lease.starts_at).isoformat(),
            "expires_at": ensure_utc(lease.expires_at).isoformat(),
            "duration_seconds": lease.duration_seconds,
        },
    }


def _worker_payload(target: ActivationTarget, operation: PortalOperation) -> dict[str, Any]:
    stage_payload = target.stage_operation.validated_payload
    return {
        "activation_operation_id": str(operation.id),
        "managed_user_id": str(target.managed.id),
        "portal_account_id": str(target.account.id),
        "owner_login": target.account.normalized_login,
        "request_id": str(target.request.id),
        "plan_id": str(target.plan.id),
        "stage_operation_id": str(target.stage_operation.id),
        "dry_run_operation_id": str(stage_payload["dry_run_operation_id"]),
        "username": target.managed.unix_username,
        "uid": target.managed.uid,
        "gid": target.managed.gid,
        "project_id": target.plan.project_id,
        "ssh_port": target.plan.container_ssh_port,
        "container_name": target.container.name,
        "workspace_path": str(workspace_path(target.managed.uid)),
        "development_profile": target.container.development_profile,
        "container_gpu": target.container.gpu_count,
        "slurm_account": target.plan.slurm_account,
        "slurm_qos": target.plan.slurm_qos,
        "ssh_key_record_ids": [str(key.id) for key in target.keys],
        "ssh_key_fingerprints": [key.fingerprint_sha256 for key in target.keys],
        "gpu_max": target.plan.gpu_max,
        "lease_seconds": LEASE_SECONDS,
        "expected_compute_state": "STAGED",
        "expected_container_state": "STOPPED",
        "expected_host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_shell": "/usr/sbin/nologin",
        "expected_password_state": "LOCKED",
        "expected_gpu": "NONE" if target.container.gpu_count == 0 else "SLURM_ALLOCATED_1",
        "lease_id": str(uuid.uuid4()),
        "deployment_version": deployment_version(),
    }


def _rollback_worker(
    context: AuthContext, operation: PortalOperation, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        return call_worker(
            "compute.activate.self.rollback",
            payload=payload,
            requested_by=context.user.normalized_login,
            approved_by=context.user.normalized_login,
            idempotency_key=f"compute-activate-rollback:{operation.id}",
            dry_run=False,
            timeout_seconds=180,
        )
    except WorkerClientError as exc:
        return {
            "status": "ERROR",
            "error": {"code": exc.code, "message": "激活安全回滚 Worker 暂不可用"},
            "rollback_status": "UNKNOWN",
        }


def _record_failure(
    db: Session,
    request: Request,
    context: AuthContext,
    operation_id: uuid.UUID,
    *,
    code: str,
    rollback_status: str,
) -> None:
    db.rollback()
    operation = db.get(PortalOperation, operation_id)
    if operation is None or operation.status in TERMINAL_OPERATION_STATES:
        return
    operation.status = OperationStatus.FAILED
    operation.error_code = code[:64]
    operation.rollback_status = rollback_status[:32]
    operation.result_summary = "Owner-bound activation failed; Lease was not started"
    operation.finished_at = utcnow()
    managed_user_id = operation.owner_managed_user_id
    if managed_user_id is not None:
        container = db.scalar(
            select(PortalContainer).where(PortalContainer.owner_managed_user_id == managed_user_id)
        )
        if container is not None:
            container.desired_state = "STOPPED"
            container.observed_state = (
                "STOPPED" if rollback_status in {"NOT_REQUIRED", "ROLLED_BACK"} else "UNKNOWN"
            )
            if rollback_status in {"NOT_REQUIRED", "ROLLED_BACK"}:
                container.gpu_allocation_job_id = None
                container.gpu_allocation_uuid = None
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=OperationStatus.RUNNING,
            to_status=OperationStatus.FAILED,
            safe_message="Activation failed closed before Lease commit",
            created_at=utcnow(),
        )
    )
    if managed_user_id is not None:
        _audit(
            db,
            request,
            context,
            event_type="COMPUTE_SELF_ACTIVATION_FAILED",
            operation_id=operation.id,
            managed_user_id=managed_user_id,
            result="FAILED",
            metadata={
                "error_code": code,
                "rollback_result": rollback_status,
                "lease_started": False,
                "host_access": "DISABLED",
                "deployment_version": operation.validated_payload.get("deployment_version"),
            },
        )
    db.commit()


def _parse_worker_lease(result: dict[str, Any]) -> tuple[datetime, datetime]:
    try:
        starts_at = ensure_utc(
            datetime.fromisoformat(str(result["lease_starts_at"]).replace("Z", "+00:00"))
        )
        expires_at = ensure_utc(
            datetime.fromisoformat(str(result["lease_expires_at"]).replace("Z", "+00:00"))
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _error(
            409, "ACTIVATION_WORKER_RESULT_INVALID", "激活 Worker 未返回有效 Lease 时间"
        ) from exc
    if expires_at - starts_at != timedelta(seconds=LEASE_SECONDS) or expires_at <= utcnow():
        raise _error(409, "ACTIVATION_LEASE_CONTRACT_FAILED", "激活 Lease 时间不符合96小时策略")
    return starts_at, expires_at


def _success_response(
    operation: PortalOperation, lease: PortalComputeLease, *, replay: bool
) -> dict[str, Any]:
    return {
        "status": "ACTIVE",
        "operation_id": str(operation.id),
        "idempotent_replay": replay,
        "environment_state": "ACTIVE",
        "container_state": "RUNNING",
        "ssh_state": "READY",
        "lease": {
            "id": str(lease.id),
            "state": lease.state,
            "starts_at": ensure_utc(lease.starts_at).isoformat(),
            "expires_at": ensure_utc(lease.expires_at).isoformat(),
            "duration_seconds": lease.duration_seconds,
        },
    }


def _in_flight_response(operation: PortalOperation) -> dict[str, Any]:
    """Return the existing top-level action without dispatching a second Worker write."""
    return {
        "status": "ACTIVATING",
        "operation_id": str(operation.id),
        "idempotent_replay": True,
        "environment_state": "STAGED",
        "container_state": "ACTIVATING",
        "ssh_state": "INSTALLING",
        "lease": None,
    }


@router.post("/self/compute/activate")
def activate_self_compute(
    body: SelfComputeActivateRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.compute.activate")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Activate only the authenticated user's own already-STAGED environment."""
    require_session_csrf(request, context)
    managed = db.scalar(
        select(PortalManagedUser)
        .where(PortalManagedUser.portal_user_id == context.user.id)
        .with_for_update()
    )
    if managed is None:
        raise _error(404, "MANAGED_IDENTITY_NOT_FOUND", "当前账号没有受管计算身份")
    if managed.onboarding_state == OnboardingState.ACTIVE:
        return _active_response(db, managed)

    operation_key = f"compute-activate-self:{managed.id}:{body.idempotency_key}"
    operation = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.idempotency_key == operation_key,
        )
    )
    if operation is not None and (
        operation.operation_type != "compute.activate.self"
        or operation.owner_managed_user_id != managed.id
    ):
        raise _error(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于其他操作")
    execute_operation = False
    if operation is None:
        in_flight = db.scalar(
            select(PortalOperation).where(
                PortalOperation.owner_managed_user_id == managed.id,
                PortalOperation.operation_type == "compute.activate.self",
                PortalOperation.status == OperationStatus.RUNNING,
            )
        )
        if in_flight is not None:
            operation = in_flight
            started_at = ensure_utc(operation.started_at or operation.created_at)
            if started_at > utcnow() - ACTIVATION_RESUME_AFTER:
                db.commit()
                return _in_flight_response(operation)
            execute_operation = True
        else:
            target = _load_staged_target(db, context, lock=True)
            now = utcnow()
            operation = PortalOperation(
                operation_type="compute.activate.self",
                target_type="compute_identity",
                target_id=str(target.managed.id),
                requested_by=context.user.id,
                owner_managed_user_id=target.managed.id,
                approved_by=context.user.id,
                request_summary="用户激活自己的已 Stage 开发容器",
                validated_payload={},
                idempotency_key=operation_key,
                risk_level=RiskLevel.MEDIUM,
                status=OperationStatus.RUNNING,
                created_at=now,
                approved_at=now,
                started_at=now,
            )
            db.add(operation)
            db.flush()
            operation.validated_payload = _worker_payload(target, operation)
            db.add(
                PortalOperationEvent(
                    operation_id=operation.id,
                    from_status=None,
                    to_status=OperationStatus.RUNNING,
                    safe_message="Owner-bound activation preflight started",
                    created_at=now,
                )
            )
            db.commit()
            execute_operation = True
    elif operation.status == OperationStatus.SUCCEEDED:
        lease = db.scalar(
            select(PortalComputeLease)
            .where(PortalComputeLease.owner_managed_user_id == managed.id)
            .order_by(PortalComputeLease.starts_at.desc())
        )
        if lease is None:
            raise _error(409, "ACTIVATION_STATE_INCONSISTENT", "激活操作缺少 Lease 记录")
        return _success_response(operation, lease, replay=True)
    elif operation.status == OperationStatus.RUNNING:
        started_at = ensure_utc(operation.started_at or operation.created_at)
        if started_at > utcnow() - ACTIVATION_RESUME_AFTER:
            db.commit()
            return _in_flight_response(operation)
        execute_operation = True
    else:
        raise _error(409, "ACTIVATION_RETRY_REQUIRED", "上次激活失败，请重新点击激活")

    if not execute_operation:  # pragma: no cover - defensive invariant
        raise _error(409, "ACTIVATION_STATE_INCONSISTENT", "激活操作状态不完整")

    # Do not hold the owner row lock across the Root Worker call. Fresh
    # concurrent replays return the existing operation above. Only an operation
    # older than the bounded recovery threshold may resume the idempotent Worker
    # lifecycle after an API process interruption.
    db.commit()
    payload = dict(operation.validated_payload)
    try:
        worker_result = call_worker(
            "compute.activate.self",
            payload=payload,
            requested_by=context.user.normalized_login,
            approved_by=context.user.normalized_login,
            idempotency_key=f"compute-activate:{operation.id}",
            dry_run=False,
            timeout_seconds=240,
        )
    except WorkerClientError as exc:
        rollback = _rollback_worker(context, operation, payload)
        rollback_status = (
            "ROLLED_BACK"
            if rollback.get("status") == "SUCCEEDED"
            and rollback.get("rollback_status") == "ROLLED_BACK"
            else "UNKNOWN"
        )
        _record_failure(
            db,
            request,
            context,
            operation.id,
            code=exc.code,
            rollback_status=rollback_status,
        )
        raise _error(503, exc.code, "受控激活 Worker 暂不可用；Lease 未启动") from exc

    expected_fingerprints = payload.get("ssh_key_fingerprints")
    worker_ok = (
        worker_result.get("status") == "SUCCEEDED"
        and worker_result.get("handler") == "compute.activate.self"
        and worker_result.get("activation_operation_id") == str(operation.id)
        and worker_result.get("managed_user_id") == str(managed.id)
        and worker_result.get("username") == managed.unix_username
        and worker_result.get("container_name") == managed.container_name
        and worker_result.get("container_state") == "RUNNING"
        and worker_result.get("container_gpu") == payload.get("expected_gpu")
        and worker_result.get("container_cpus") == 8
        and worker_result.get("container_memory_gb") == 32
        and worker_result.get("container_pids_limit") == 4096
        and worker_result.get("container_privileged") is False
        and worker_result.get("docker_socket") == "ABSENT"
        and worker_result.get("munge") == "ABSENT"
        and worker_result.get("host_namespaces") == "DISABLED"
        and worker_result.get("host_authorized_keys") == "ABSENT"
        and worker_result.get("host_shell") == "/usr/sbin/nologin"
        and worker_result.get("host_password") == "LOCKED"
        and worker_result.get("container_key_fingerprints") == expected_fingerprints
        and worker_result.get("deployment_version") == payload.get("deployment_version")
    )
    allocation_job_id = worker_result.get("gpu_allocation_job_id")
    allocation_uuid = worker_result.get("gpu_allocation_uuid")
    allocation_ok = (
        allocation_job_id is None and allocation_uuid is None
        if payload.get("development_profile") == CPU_DEVELOPMENT_PROFILE
        else isinstance(allocation_job_id, int)
        and allocation_job_id > 0
        and isinstance(allocation_uuid, str)
        and allocation_uuid.startswith("GPU-")
    )
    worker_ok = worker_ok and allocation_ok
    if not worker_ok:
        error = worker_result.get("error", {})
        code = str(error.get("code", "ACTIVATION_WORKER_RESULT_INVALID"))[:64]
        reported_rollback = worker_result.get("rollback_status")
        if worker_result.get("status") == "ERROR" and reported_rollback in {
            "NOT_REQUIRED",
            "ROLLED_BACK",
        }:
            rollback_status = str(reported_rollback)
        else:
            rollback = _rollback_worker(context, operation, payload)
            rollback_status = (
                "ROLLED_BACK"
                if rollback.get("status") == "SUCCEEDED"
                and rollback.get("rollback_status") == "ROLLED_BACK"
                else "UNKNOWN"
            )
        _record_failure(
            db,
            request,
            context,
            operation.id,
            code=code,
            rollback_status=rollback_status,
        )
        raise _error(409, code, "计算环境激活失败；Lease 未启动")

    lease_id = uuid.UUID(str(payload["lease_id"]))
    try:
        persisted_operation = db.scalar(
            select(PortalOperation).where(PortalOperation.id == operation.id).with_for_update()
        )
        if persisted_operation is None:
            raise _error(409, "ACTIVATION_CONCURRENT_STATE_CHANGE", "激活状态发生并发变化")
        if persisted_operation.status == OperationStatus.SUCCEEDED:
            persisted_lease = db.scalar(
                select(PortalComputeLease)
                .where(PortalComputeLease.owner_managed_user_id == managed.id)
                .order_by(PortalComputeLease.starts_at.desc())
            )
            if persisted_lease is None:
                raise _error(409, "ACTIVATION_STATE_INCONSISTENT", "激活操作缺少 Lease 记录")
            db.commit()
            return _success_response(persisted_operation, persisted_lease, replay=True)
        if persisted_operation.status != OperationStatus.RUNNING:
            raise _error(409, "ACTIVATION_CONCURRENT_STATE_CHANGE", "激活状态发生并发变化")

        starts_at, expires_at = _parse_worker_lease(worker_result)
        target = _load_staged_target(db, context, lock=True)
        if [str(key.id) for key in target.keys] != payload.get("ssh_key_record_ids"):
            raise _error(409, "ACTIVATION_KEY_SET_CHANGED", "激活期间 SSH 公钥集合发生变化")

        lease = create_lease(
            managed_user_id=target.managed.id,
            starts_at=starts_at,
            duration_seconds=LEASE_SECONDS,
            gpu_count=target.plan.gpu_max,
            approved_by=context.user.id,
            state="ACTIVE",
        )
        lease.id = lease_id
        if ensure_utc(lease.expires_at) != expires_at:
            raise _error(409, "ACTIVATION_LEASE_CONTRACT_FAILED", "Worker 与 Portal Lease 不一致")
        db.add(lease)
        target.account.resource_onboarding_state = OnboardingState.ACTIVE
        target.managed.onboarding_state = OnboardingState.ACTIVE
        target.managed.compute_environment_state = "ACTIVE"
        target.managed.ssh_key_state = "INSTALLED"
        target.managed.compute_activated_at = starts_at
        target.request.status = "ACTIVE"
        target.request.active_slot = None
        target.container.desired_state = "RUNNING"
        target.container.observed_state = "RUNNING"
        target.container.gpu_allocation_job_id = allocation_job_id
        target.container.gpu_allocation_uuid = allocation_uuid
        target.container.safe_spec = {
            **target.container.safe_spec,
            "authorized_keys": "INSTALLED",
            "container_authorized_keys": "INSTALLED",
            "host_authorized_keys": "ABSENT",
            "lease_state": "ACTIVE",
        }
        target.storage.state = "ACTIVE"
        for key in target.keys:
            key.state = "INSTALLED"
            key.container_install_state = "INSTALLED"
            key.host_install_state = "NOT_INSTALLED"
            key.installed_at = starts_at
        persisted_operation.status = OperationStatus.SUCCEEDED
        persisted_operation.finished_at = starts_at
        persisted_operation.worker_execution_id = str(
            worker_result.get("worker_request_id", "worker")
        )[:64]
        persisted_operation.rollback_status = "NOT_REQUIRED"
        persisted_operation.result_summary = (
            "Owner-bound Container activation succeeded; 96-hour Lease started"
        )
        persisted_operation.validated_payload = {
            **persisted_operation.validated_payload,
            "lease_starts_at": starts_at.isoformat(),
            "lease_expires_at": expires_at.isoformat(),
        }
        db.add(
            PortalOperationEvent(
                operation_id=persisted_operation.id,
                from_status=OperationStatus.RUNNING,
                to_status=OperationStatus.SUCCEEDED,
                safe_message="Container key installed, container healthy, and Lease committed",
                created_at=starts_at,
            )
        )
        _audit(
            db,
            request,
            context,
            event_type="COMPUTE_SELF_ACTIVATED",
            operation_id=persisted_operation.id,
            managed_user_id=target.managed.id,
            result="SUCCESS",
            metadata={
                "request_id": str(target.request.id),
                "attempt_number": target.plan.attempt_number,
                "container_state": "RUNNING",
                "host_access": "DISABLED",
                "lease_starts_at": starts_at.isoformat(),
                "lease_expires_at": expires_at.isoformat(),
                "deployment_version": payload.get("deployment_version"),
            },
        )
        db.flush()
        db.commit()
    except Exception as exc:
        db.rollback()
        persisted_operation = db.get(PortalOperation, operation.id)
        if (
            persisted_operation is not None
            and persisted_operation.status == OperationStatus.SUCCEEDED
        ):
            persisted_lease = db.scalar(
                select(PortalComputeLease)
                .where(PortalComputeLease.owner_managed_user_id == managed.id)
                .order_by(PortalComputeLease.starts_at.desc())
            )
            if persisted_lease is not None:
                return _success_response(persisted_operation, persisted_lease, replay=True)
        rollback = _rollback_worker(context, operation, payload)
        rollback_status = (
            "ROLLED_BACK"
            if rollback.get("status") == "SUCCEEDED"
            and rollback.get("rollback_status") == "ROLLED_BACK"
            else "UNKNOWN"
        )
        detail = exc.detail if isinstance(exc, HTTPException) else None
        code = (
            str(detail.get("code", "ACTIVATION_PERSISTENCE_FAILED"))
            if isinstance(detail, dict)
            else "ACTIVATION_PERSISTENCE_FAILED"
        )
        _record_failure(
            db,
            request,
            context,
            operation.id,
            code=code,
            rollback_status=rollback_status,
        )
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, SQLAlchemyError):
            raise _error(409, code, "激活状态提交失败，已恢复安全 STAGED 状态") from exc
        raise _error(409, code, "激活未能安全完成，Lease 未启动") from exc

    return _success_response(
        persisted_operation,
        lease,
        replay=bool(worker_result.get("idempotent_replay")),
    )
