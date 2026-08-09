from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.auth import (
    AuthContext,
    client_ip,
    require_session_csrf,
    user_agent,
)
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import auth_context, permission_dependency
from h100_portal_api.enums import OnboardingState, OperationStatus, RiskLevel
from h100_portal_api.models import (
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalOperationEvent,
    PortalSshKey,
    utcnow,
)
from h100_portal_api.rbac import highest_role
from h100_portal_api.routes.platform import adapter
from h100_portal_api.schemas import ManagedContainerStartRequest
from h100_portal_api.worker_client import WorkerClientError, call_worker

router = APIRouter(prefix="/containers", tags=["containers"])


@router.get("")
def containers(
    context: AuthContext = Depends(permission_dependency("containers.read")),
) -> dict[str, Any]:
    return adapter("containers.list", context)


@router.get("/{name}")
def inspect_container(
    name: str, context: AuthContext = Depends(permission_dependency("containers.read"))
) -> dict[str, Any]:
    try:
        return call_worker(
            "containers.inspect", payload={"name": name}, requested_by=context.user.normalized_login
        )
    except WorkerClientError as exc:
        raise HTTPException(
            status_code=503, detail={"code": exc.code, "message": str(exc)}
        ) from exc


def _start_response(operation: PortalOperation, container: PortalContainer) -> dict[str, Any]:
    return {
        "status": operation.status,
        "operation_id": str(operation.id),
        "container": {
            "name": container.name,
            "state": container.observed_state,
            "gpu": container.safe_spec.get("gpu", "UNKNOWN"),
        },
    }


@router.post("/{name}/start")
def start_owned_managed_container(
    name: str,
    body: ManagedContainerStartRequest,
    request: Request,
    context: AuthContext = Depends(auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Start only the authenticated account's activated, GPU-less Pilot container."""
    require_session_csrf(request, context)
    managed = db.scalar(
        select(PortalManagedUser)
        .where(PortalManagedUser.portal_user_id == context.user.id)
        .with_for_update()
    )
    if managed is None or managed.container_name != name:
        raise HTTPException(
            status_code=404,
            detail={"code": "MANAGED_CONTAINER_NOT_FOUND", "message": "受管容器不存在"},
        )
    container = db.scalar(
        select(PortalContainer)
        .where(
            PortalContainer.managed_user_id == managed.id,
            PortalContainer.name == name,
        )
        .with_for_update()
    )
    if container is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "MANAGED_CONTAINER_NOT_FOUND", "message": "受管容器不存在"},
        )

    operation_key = f"container-start:{managed.id}:{body.idempotency_key}"
    existing = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.idempotency_key == operation_key,
        )
    )
    if existing is not None:
        if (
            existing.operation_type != "container.start"
            or existing.target_type != "container"
            or existing.target_id != name
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "IDEMPOTENCY_CONFLICT", "message": "幂等键已用于其他操作"},
            )
        return _start_response(existing, container)

    installed_container_keys = int(
        db.scalar(
            select(func.count(PortalSshKey.id)).where(
                PortalSshKey.managed_user_id == managed.id,
                PortalSshKey.state == "INSTALLED",
                PortalSshKey.active.is_(True),
                PortalSshKey.scope.in_({"CONTAINER", "BOTH"}),
            )
        )
        or 0
    )
    safe_spec = container.safe_spec if isinstance(container.safe_spec, dict) else {}
    safe_contract = (
        safe_spec.get("gpu") == "NONE"
        and safe_spec.get("privileged") is False
        and safe_spec.get("host_network") is False
        and safe_spec.get("host_pid") is False
        and safe_spec.get("host_ipc") is False
        and safe_spec.get("docker_socket") is False
        and safe_spec.get("authorized_keys") == "INSTALLED"
    )
    if (
        managed.onboarding_state != OnboardingState.ACTIVE
        or managed.ssh_key_state != "INSTALLED"
        or managed.ssh_key_count < 1
        or managed.shell != "/bin/bash"
        or managed.host_access_state != "ENABLED"
        or container.observed_state != "STOPPED"
        or container.desired_state != "STOPPED"
        or installed_container_keys < 1
        or not safe_contract
    ):
        record_audit(
            db,
            event_type="container.start.denied",
            actor=context.user.normalized_login,
            actor_role=highest_role(context.user),
            source_ip=client_ip(request),
            user_agent=user_agent(request),
            object_type="container",
            object_id=name,
            result="DENIED",
            metadata={
                "compute_state": str(managed.onboarding_state),
                "ssh_key_state": managed.ssh_key_state,
                "container_state": container.observed_state,
                "safe_contract": safe_contract,
            },
        )
        db.commit()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CONTAINER_START_STATE_REJECTED",
                "message": "容器启动要求 ACTIVE、INSTALLED Key 和安全的 STOPPED 容器",
            },
        )

    operation = PortalOperation(
        operation_type="container.start",
        target_type="container",
        target_id=name,
        requested_by=context.user.id,
        owner_managed_user_id=managed.id,
        approved_by=context.user.id,
        request_summary=f"启动 {managed.unix_username} 的受管开发容器",
        validated_payload={
            "name": name,
            "username": managed.unix_username,
            "managed_user_id": str(managed.id),
            "expected_compute_state": "ACTIVE",
            "expected_container_state": "STOPPED",
            "expected_ssh_key_state": "INSTALLED",
        },
        idempotency_key=operation_key,
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.RUNNING,
        created_at=utcnow(),
        approved_at=utcnow(),
        started_at=utcnow(),
    )
    db.add(operation)
    db.flush()
    try:
        worker_result = call_worker(
            "container.start",
            payload=operation.validated_payload,
            requested_by=context.user.normalized_login,
            approved_by=context.user.normalized_login,
            idempotency_key=operation_key,
            dry_run=False,
            timeout_seconds=180,
        )
    except WorkerClientError as exc:
        container.desired_state = "STOPPED"
        container.observed_state = "UNKNOWN"
        operation.status = OperationStatus.FAILED
        operation.error_code = exc.code[:64]
        operation.finished_at = utcnow()
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=OperationStatus.RUNNING,
                to_status=OperationStatus.FAILED,
                safe_message="Managed container start Worker call failed closed",
                created_at=utcnow(),
            )
        )
        db.commit()
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": "受控容器启动 Worker 暂不可用"},
        ) from exc

    if not (
        worker_result.get("status") == "SUCCEEDED"
        and worker_result.get("handler") == "container.start"
        and worker_result.get("name") == name
        and worker_result.get("username") == managed.unix_username
        and worker_result.get("container_state") == "RUNNING"
        and worker_result.get("container_gpu") == "NONE"
    ):
        worker_error = worker_result.get("error", {})
        code = str(worker_error.get("code", "CONTAINER_START_FAILED"))[:64]
        if code == "CONTAINER_STOP_RECOVERY_FAILED":
            container.desired_state = "STOPPED"
            container.observed_state = "UNKNOWN"
        operation.status = OperationStatus.FAILED
        operation.error_code = code
        operation.finished_at = utcnow()
        operation.result_summary = "受控容器启动被拒绝；Portal 未记录 RUNNING"
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=OperationStatus.RUNNING,
                to_status=OperationStatus.FAILED,
                safe_message="Managed container start failed closed",
                created_at=utcnow(),
            )
        )
        record_audit(
            db,
            event_type="container.start.failed",
            actor=context.user.normalized_login,
            actor_role=highest_role(context.user),
            source_ip=client_ip(request),
            user_agent=user_agent(request),
            object_type="container",
            object_id=name,
            operation_id=operation.id,
            result="FAILED",
            metadata={"error_code": code},
        )
        db.commit()
        raise HTTPException(
            status_code=409,
            detail={"code": code, "message": "受控容器启动未通过安全校验"},
        )

    container.desired_state = "RUNNING"
    container.observed_state = "RUNNING"
    operation.status = OperationStatus.SUCCEEDED
    operation.worker_execution_id = str(worker_result.get("request_id", "worker"))[:64]
    operation.result_summary = "受管开发容器已启动并通过 GPU NONE 与健康状态复核"
    operation.finished_at = utcnow()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=OperationStatus.RUNNING,
            to_status=OperationStatus.SUCCEEDED,
            safe_message="Managed GPU-less container reached healthy RUNNING state",
            created_at=utcnow(),
        )
    )
    record_audit(
        db,
        event_type="container.start",
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="container",
        object_id=name,
        operation_id=operation.id,
        result="SUCCESS",
        metadata={"state": "RUNNING", "gpu": "NONE", "managed_user_id": str(managed.id)},
    )
    db.commit()
    return _start_response(operation, container)
