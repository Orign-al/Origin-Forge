import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.auth import (
    AuthContext,
    client_ip,
    require_recent_reauthentication,
    require_session_csrf,
    user_agent,
)
from h100_portal_api.database import SessionLocal, get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.enums import OperationStatus
from h100_portal_api.models import (
    PortalOperation,
    PortalOperationApproval,
    PortalOperationEvent,
    PortalUser,
    utcnow,
)
from h100_portal_api.operations import (
    HIGH_RISK_OPERATION_TYPES,
    OPERATION_PERMISSIONS,
    WRITE_OPERATION_TYPES,
    can_transition,
    risk_for,
)
from h100_portal_api.schemas import (
    ApprovalRequest,
    OperationCreateRequest,
    OperationResponse,
    OperationSubmitRequest,
)
from h100_portal_api.security import SAFE_TARGET_RE, safe_metadata, safe_target
from h100_portal_api.worker_client import WorkerClientError, call_worker

router = APIRouter(prefix="/operations", tags=["operations"])


def operation_response(operation: PortalOperation) -> OperationResponse:
    return OperationResponse(
        id=operation.id,
        operation_type=operation.operation_type,
        target_type=operation.target_type,
        target_id=operation.target_id,
        requested_by=operation.requested_by,
        approved_by=operation.approved_by,
        request_summary=operation.request_summary,
        risk_level=operation.risk_level,
        status=operation.status,
        created_at=operation.created_at,
        approved_at=operation.approved_at,
        started_at=operation.started_at,
        finished_at=operation.finished_at,
        result_summary=operation.result_summary,
        error_code=operation.error_code,
    )


def validate_operation_payload(operation_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    allowed: dict[str, set[str]] = {
        "user.plan": {"username"},
        "user.stage": {"username"},
        "user.activate": {"username"},
        "user.suspend": {"username"},
        "container.start": {"name"},
        "container.stop": {"name"},
        "container.restart": {"name"},
        "container.rebuild": {"name"},
        "slurm.drain": {"node_name"},
        "slurm.resume": {"node_name"},
        "job.cancel": {"job_id"},
        "quota.update": {"username", "quota_bytes"},
        "ssh_key.add": {"username", "key_type", "fingerprint", "comment_summary"},
        "ssh_key.revoke": {"username", "fingerprint"},
    }
    if operation_type not in allowed:
        raise ValueError("unknown write operation")
    if set(payload) - allowed[operation_type]:
        raise ValueError("payload contains unsupported fields")
    result: dict[str, Any] = {}
    if "username" in allowed[operation_type]:
        username = payload.get("username")
        normalized_username = username.casefold() if isinstance(username, str) else ""
        protected = normalized_username in {"root", "origin-al", "codexops"}
        if (
            not isinstance(username, str)
            or not SAFE_TARGET_RE.fullmatch(username)
            or (
                protected
                and not (operation_type == "user.plan" and normalized_username == "origin-al")
            )
        ):
            raise ValueError("protected or invalid username")
        result["username"] = normalized_username
    if "name" in allowed[operation_type]:
        name = payload.get("name")
        if (
            not isinstance(name, str)
            or not SAFE_TARGET_RE.fullmatch(name)
            or not (name.startswith("gpu-dev-") or name.startswith("h100-"))
        ):
            raise ValueError("invalid managed container name")
        result["name"] = name
    if "node_name" in allowed[operation_type]:
        node = payload.get("node_name")
        if node != "sagsh100server":
            raise ValueError("only the known single node is in scope")
        result["node_name"] = node
    if "job_id" in allowed[operation_type]:
        job_id = payload.get("job_id")
        if not isinstance(job_id, int) or not 0 < job_id < 2**63:
            raise ValueError("invalid job id")
        result["job_id"] = job_id
    if "quota_bytes" in allowed[operation_type]:
        quota = payload.get("quota_bytes")
        if not isinstance(quota, int) or not 0 < quota <= 10 * 1024**4:
            raise ValueError("invalid quota")
        result["quota_bytes"] = quota
    for field in ("key_type", "fingerprint", "comment_summary"):
        if field in payload:
            value = payload[field]
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 255
                or "PRIVATE KEY" in value
            ):
                raise ValueError("invalid SSH metadata")
            result[field] = value[:255]
    return result


def transition(
    operation: PortalOperation, target: OperationStatus, message: str, db: Session
) -> None:
    if not can_transition(operation.status, target):
        raise RuntimeError(f"invalid operation transition {operation.status}->{target}")
    previous = operation.status
    operation.status = target
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=previous,
            to_status=target,
            safe_message=message[:1000],
            created_at=utcnow(),
        )
    )


def execute_operation(operation_id: uuid.UUID, actor_login: str) -> None:
    with SessionLocal() as db:
        operation = db.get(PortalOperation, operation_id)
        if operation is None or operation.status != OperationStatus.QUEUED:
            return
        transition(operation, OperationStatus.RUNNING, "Worker dry-run started", db)
        operation.started_at = utcnow()
        db.commit()
        try:
            requester = db.get(PortalUser, operation.requested_by)
            approver = db.get(PortalUser, operation.approved_by) if operation.approved_by else None
            result = call_worker(
                operation.operation_type,
                payload=operation.validated_payload,
                requested_by=requester.normalized_login if requester else "portal",
                approved_by=approver.normalized_login if approver else actor_login,
                idempotency_key=operation.idempotency_key,
                dry_run=True,
                timeout_seconds=30,
            )
            if result.get("status") == "DRY_RUN":
                transition(operation, OperationStatus.SUCCEEDED, "dry-run plan validated", db)
                operation.result_summary = "dry-run 计划已通过 Worker schema 和脚本完整性检查"
                operation.worker_execution_id = str(result.get("request_id", "dry-run"))[:64]
                record_audit(
                    db,
                    event_type="worker.execute",
                    actor=actor_login,
                    actor_role="platform_owner",
                    source_ip="local-worker-socket",
                    user_agent="h100-portal-api",
                    object_type="operation",
                    object_id=str(operation.id),
                    result="DRY_RUN",
                    metadata={"operation_type": operation.operation_type},
                    operation_id=operation.id,
                )
            else:
                transition(operation, OperationStatus.FAILED, "Worker rejected dry-run", db)
                operation.error_code = str(result.get("error", {}).get("code", "WORKER_FAILED"))[
                    :64
                ]
                operation.result_summary = "Worker 未执行宿主写操作"
                record_audit(
                    db,
                    event_type="worker.reject",
                    actor=actor_login,
                    actor_role="platform_owner",
                    source_ip="local-worker-socket",
                    user_agent="h100-portal-api",
                    object_type="operation",
                    object_id=str(operation.id),
                    result="DENIED",
                    metadata={"operation_type": operation.operation_type},
                    operation_id=operation.id,
                )
        except (WorkerClientError, RuntimeError) as exc:
            transition(operation, OperationStatus.FAILED, "Worker unavailable or rejected", db)
            operation.error_code = getattr(exc, "code", "WORKER_FAILED")[:64]
            operation.result_summary = "受控 Worker 不可用；未执行宿主写操作"
            record_audit(
                db,
                event_type="worker.reject",
                actor=actor_login,
                actor_role="platform_owner",
                source_ip="local-worker-socket",
                user_agent="h100-portal-api",
                object_type="operation",
                object_id=str(operation.id),
                result="FAILED",
                metadata={
                    "operation_type": operation.operation_type,
                    "error_code": operation.error_code,
                },
                operation_id=operation.id,
            )
        operation.finished_at = utcnow()
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=operation.status,
                to_status=operation.status,
                safe_message="Worker execution recorded",
                created_at=utcnow(),
            )
        )
        db.commit()


@router.get("")
def list_operations(
    context: AuthContext = Depends(permission_dependency("operations.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.scalars(
        select(PortalOperation).order_by(PortalOperation.created_at.desc()).limit(200)
    ).all()
    return {
        "status": "OK",
        "operations": [operation_response(row).model_dump(mode="json") for row in rows],
    }


@router.post("")
def create_operation(
    body: OperationCreateRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("operations.write")),
    db: Session = Depends(get_db),
) -> OperationResponse:
    require_session_csrf(request, context)
    operation_type = body.operation_type
    if operation_type not in WRITE_OPERATION_TYPES:
        raise HTTPException(
            status_code=422,
            detail={"code": "OPERATION_NOT_WRITABLE", "message": "仅允许受控写操作"},
        )
    permission = OPERATION_PERMISSIONS.get(operation_type)
    if permission is None:
        raise HTTPException(
            status_code=403, detail={"code": "FORBIDDEN", "message": "当前账号无权执行此操作"}
        )
    from h100_portal_api.rbac import require_permission

    require_permission(context.user, permission)
    try:
        target_id = safe_target(body.target_id)
        validated = validate_operation_payload(operation_type, body.payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "PAYLOAD_REJECTED", "message": str(exc)}
        ) from exc
    existing = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.idempotency_key == body.idempotency_key,
        )
    )
    if existing:
        return operation_response(existing)
    operation = PortalOperation(
        operation_type=operation_type,
        target_type=body.target_type,
        target_id=target_id,
        requested_by=context.user.id,
        request_summary=body.request_summary[:500],
        validated_payload=safe_metadata(validated),
        idempotency_key=body.idempotency_key,
        risk_level=risk_for(operation_type),
        status=OperationStatus.DRAFT,
        created_at=utcnow(),
    )
    db.add(operation)
    db.flush()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=None,
            to_status=OperationStatus.DRAFT,
            safe_message="Operation draft created; no host action executed",
            created_at=utcnow(),
        )
    )
    record_audit(
        db,
        event_type="operation.draft",
        actor=context.user.normalized_login,
        actor_role="/".join(sorted(role.name for role in context.user.roles)),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="operation",
        object_id=str(operation.id),
        result="SUCCESS",
        metadata={"operation_type": operation_type, "target_type": body.target_type},
        operation_id=operation.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(
            select(PortalOperation).where(
                PortalOperation.requested_by == context.user.id,
                PortalOperation.idempotency_key == body.idempotency_key,
            )
        )
        if existing:
            return operation_response(existing)
        raise HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT", "message": "幂等键冲突"},
        ) from exc
    return operation_response(operation)


@router.post("/{operation_id}/submit")
def submit_operation(
    operation_id: str,
    body: OperationSubmitRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("operations.write")),
    db: Session = Depends(get_db),
) -> OperationResponse:
    require_session_csrf(request, context)
    try:
        operation = db.get(PortalOperation, uuid.UUID(operation_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "OPERATION_NOT_FOUND", "message": "任务不存在"}
        ) from exc
    if operation is None:
        raise HTTPException(
            status_code=404, detail={"code": "OPERATION_NOT_FOUND", "message": "任务不存在"}
        )
    if operation.status != OperationStatus.DRAFT:
        raise HTTPException(
            status_code=409,
            detail={"code": "INVALID_OPERATION_STATE", "message": "仅草稿可以提交审批"},
        )
    if body.confirmation != operation.target_id:
        raise HTTPException(
            status_code=428,
            detail={"code": "CONFIRMATION_REQUIRED", "message": "请输入准确的对象名确认"},
        )
    if operation.operation_type in HIGH_RISK_OPERATION_TYPES:
        require_recent_reauthentication(context)
    transition(operation, OperationStatus.PENDING_APPROVAL, "submitted for dry-run approval", db)
    record_audit(
        db,
        event_type="operation.submit",
        actor=context.user.normalized_login,
        actor_role="/".join(sorted(role.name for role in context.user.roles)),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="operation",
        object_id=str(operation.id),
        metadata={"operation_type": operation.operation_type, "execution_mode": "dry-run"},
        operation_id=operation.id,
    )
    db.commit()
    return operation_response(operation)


@router.post("/{operation_id}/approval")
def approve_operation(
    operation_id: str,
    body: ApprovalRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    context: AuthContext = Depends(permission_dependency("operations.write")),
    db: Session = Depends(get_db),
) -> OperationResponse:
    require_session_csrf(request, context)
    try:
        operation = db.get(PortalOperation, uuid.UUID(operation_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "OPERATION_NOT_FOUND", "message": "任务不存在"}
        ) from exc
    if operation is None:
        raise HTTPException(
            status_code=404, detail={"code": "OPERATION_NOT_FOUND", "message": "任务不存在"}
        )
    if operation.status != OperationStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail={"code": "INVALID_OPERATION_STATE", "message": "任务不在待审批状态"},
        )
    if body.confirmation != operation.target_id:
        raise HTTPException(
            status_code=428,
            detail={"code": "CONFIRMATION_REQUIRED", "message": "请输入准确的对象名确认"},
        )
    if operation.operation_type in HIGH_RISK_OPERATION_TYPES:
        require_recent_reauthentication(context)
    approval = PortalOperationApproval(
        operation_id=operation.id,
        approver_id=context.user.id,
        decision=body.decision,
        safe_comment=(body.comment or "")[:500],
        decided_at=utcnow(),
    )
    db.add(approval)
    if body.decision == "REJECT":
        transition(operation, OperationStatus.CANCELLED, "approval rejected", db)
    else:
        operation.approved_by = context.user.id
        operation.approved_at = utcnow()
        transition(operation, OperationStatus.APPROVED, "approval granted", db)
        transition(operation, OperationStatus.QUEUED, "queued for controlled Worker dry-run", db)
        background_tasks.add_task(execute_operation, operation.id, context.user.normalized_login)
    record_audit(
        db,
        event_type="operation.approval_simulation",
        actor=context.user.normalized_login,
        actor_role="/".join(sorted(role.name for role in context.user.roles)),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="operation",
        object_id=str(operation.id),
        result=body.decision,
        metadata={"decision": body.decision, "execution_mode": "dry-run"},
        operation_id=operation.id,
    )
    db.commit()
    return operation_response(operation)
