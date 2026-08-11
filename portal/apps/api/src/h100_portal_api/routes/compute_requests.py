import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.auth import (
    AuthContext,
    client_ip,
    rate_limiter,
    require_session_csrf,
    user_agent,
)
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordState,
    RiskLevel,
)
from h100_portal_api.models import (
    PortalAllocatorLock,
    PortalComputeLease,
    PortalComputeResourceRequest,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalOperationEvent,
    PortalProvisionPlan,
    PortalResourceReservation,
    PortalStorageResource,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.rbac import highest_role
from h100_portal_api.schemas import (
    ComputeProvisionActionRequest,
    ComputeResourceRequestCancel,
    ComputeResourceRequestCreate,
    ComputeResourceReviewRequest,
)
from h100_portal_api.worker_client import WorkerClientError, call_worker

router = APIRouter(tags=["compute-resource-requests"])

STANDARD_STORAGE_BYTES = 300 * 1024**3
STANDARD_CONTAINER_PROFILE = "STANDARD_8CPU_32GB"
STANDARD_LEASE_SECONDS = 96 * 60 * 60
RESERVATION_LIFETIME = timedelta(hours=24)
ACTIVE_REQUEST_STATES = {
    "DRAFT",
    "REQUESTED",
    "UNDER_REVIEW",
    "APPROVED",
    "PROVISION_PLAN_READY",
}
PROTECTED_USERNAMES = {"root", "origin-al", "codexops"}


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _request_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise _error(404, "COMPUTE_REQUEST_NOT_FOUND", "计算资源申请不存在") from exc


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
    operation_id: uuid.UUID | None = None,
) -> None:
    record_audit(
        db,
        event_type=event_type,
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type=object_type,
        object_id=object_id,
        result=result,
        metadata=metadata,
        operation_id=operation_id,
    )


def _operation(
    db: Session,
    *,
    context: AuthContext,
    operation_type: str,
    target_id: uuid.UUID,
    idempotency_key: str,
    summary: str,
    payload: dict[str, Any],
    status_value: OperationStatus = OperationStatus.SUCCEEDED,
    result_summary: str | None = None,
) -> PortalOperation:
    now = utcnow()
    operation = PortalOperation(
        operation_type=operation_type,
        target_type="compute_resource_request",
        target_id=str(target_id),
        requested_by=context.user.id,
        owner_managed_user_id=None,
        approved_by=context.user.id,
        request_summary=summary,
        validated_payload=payload,
        idempotency_key=f"{operation_type}:{idempotency_key}",
        risk_level=RiskLevel.MEDIUM,
        status=status_value,
        created_at=now,
        approved_at=now,
        started_at=now,
        finished_at=now,
        result_summary=result_summary,
    )
    db.add(operation)
    db.flush()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=None,
            to_status=status_value,
            safe_message=result_summary or "Fixed compute request operation completed",
            created_at=now,
        )
    )
    return operation


def _plan_view(plan: PortalProvisionPlan | None, *, internal: bool) -> dict[str, Any] | None:
    if plan is None:
        return None
    common: dict[str, Any] = {
        "id": str(plan.id),
        "state": plan.state,
        "storage_bytes": plan.storage_bytes,
        "container_profile": plan.container_profile,
        "container_cpus": plan.container_cpus,
        "container_memory_gb": plan.container_memory_gb,
        "container_pids_limit": plan.container_pids_limit,
        "container_gpu": plan.container_gpu,
        "gpu_max": plan.gpu_max,
        "lease_seconds": plan.lease_seconds,
        "lease_state": plan.lease_state,
        "host_ssh": "DISABLED",
        "execution_enabled": False,
        "reservation_expires_at": plan.reservation_expires_at,
        "dry_run_at": plan.dry_run_at,
    }
    if internal:
        common.update(
            {
                "username": plan.username,
                "uid": plan.uid,
                "gid": plan.gid,
                "project_id": plan.project_id,
                "container_name": plan.container_name,
                "container_ssh_port": plan.container_ssh_port,
                "slurm_account": plan.slurm_account,
                "slurm_qos": plan.slurm_qos,
                "shell": plan.shell,
                "password_state": plan.password_state,
                "allocator_result": plan.allocator_result,
                "dry_run_result": plan.dry_run_result,
            }
        )
    return common


def _request_view(
    db: Session, item: PortalComputeResourceRequest, *, internal: bool
) -> dict[str, Any]:
    plan = db.get(PortalProvisionPlan, item.provision_plan_id) if item.provision_plan_id else None
    result: dict[str, Any] = {
        "id": str(item.id),
        "portal_account_id": str(item.portal_account_id),
        "username": item.username,
        "status": item.status,
        "requested_gpu_max": item.requested_gpu_max,
        "requested_storage_bytes": item.requested_storage_bytes,
        "requested_container_profile": item.requested_container_profile,
        "requested_lease_seconds": item.requested_lease_seconds,
        "purpose": item.purpose,
        "user_note": item.user_note,
        "submitted_at": item.submitted_at,
        "review_note": item.review_note,
        "reviewed_at": item.reviewed_at,
        "approved_at": item.approved_at,
        "rejected_at": item.rejected_at,
        "cancelled_at": item.cancelled_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "plan": _plan_view(plan, internal=internal),
    }
    if internal:
        account = db.get(PortalUser, item.portal_account_id)
        result.update(
            {
                "requested_by": str(item.requested_by),
                "managed_user_id": str(item.managed_user_id) if item.managed_user_id else None,
                "reviewed_by": str(item.reviewed_by) if item.reviewed_by else None,
                "portal_user": {
                    "login_name": account.login_name if account else item.username,
                    "display_name": account.display_name if account else item.username,
                    "role": highest_role(account) if account else "UNKNOWN",
                    "account_state": str(account.account_state) if account else "UNKNOWN",
                    "password_state": str(account.password_state) if account else "UNKNOWN",
                    "compute_state": str(account.resource_onboarding_state)
                    if account
                    else "UNKNOWN",
                },
            }
        )
    return result


def _owned_request(
    db: Session,
    request_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    lock: bool = False,
) -> PortalComputeResourceRequest:
    query = select(PortalComputeResourceRequest).where(
        PortalComputeResourceRequest.id == request_id,
        PortalComputeResourceRequest.portal_account_id == owner_id,
    )
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        # A uniform 404 prevents account/request enumeration.
        raise _error(404, "COMPUTE_REQUEST_NOT_FOUND", "计算资源申请不存在")
    return item


def _admin_request(
    db: Session, request_id: uuid.UUID, *, lock: bool = False
) -> PortalComputeResourceRequest:
    query = select(PortalComputeResourceRequest).where(
        PortalComputeResourceRequest.id == request_id
    )
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise _error(404, "COMPUTE_REQUEST_NOT_FOUND", "计算资源申请不存在")
    return item


def _deny_self_administration(item: PortalComputeResourceRequest, context: AuthContext) -> None:
    """Keep human review and allocator actions separate from the applicant."""
    if item.portal_account_id == context.user.id:
        raise _error(
            403,
            "COMPUTE_REQUEST_SELF_ADMINISTRATION_DENIED",
            "申请人不能审批或规划自己的计算资源申请",
        )


def _assert_identity_only(db: Session, account: PortalUser) -> None:
    if account.account_state != AccountState.ACTIVE or account.password_state != PasswordState.SET:
        raise _error(409, "PORTAL_ACCOUNT_NOT_READY", "Portal 账号尚未完成激活")
    managed = db.scalar(
        select(PortalManagedUser.id).where(PortalManagedUser.portal_user_id == account.id)
    )
    if managed is not None or account.resource_onboarding_state != OnboardingState.NOT_ENROLLED:
        raise _error(409, "COMPUTE_ALREADY_PROVISIONED", "已有计算身份不能重复申请首次开户")
    username_owner = db.scalar(
        select(PortalManagedUser.id).where(
            PortalManagedUser.unix_username == account.normalized_login
        )
    )
    if username_owner is not None:
        raise _error(409, "COMPUTE_USERNAME_CONFLICT", "该登录名已被计算身份占用")
    if account.normalized_login in PROTECTED_USERNAMES:
        raise _error(409, "COMPUTE_USERNAME_PROTECTED", "该 Portal 身份不能申请普通计算资源")


def _expire_reservations(db: Session) -> None:
    now = utcnow()
    expired = db.scalars(
        select(PortalResourceReservation)
        .where(
            PortalResourceReservation.state == "RESERVED",
            PortalResourceReservation.expires_at <= now,
        )
        .with_for_update()
    ).all()
    for reservation in expired:
        reservation.state = "RELEASED"
        reservation.active_key = None
        reservation.released_at = now
        plan = db.get(PortalProvisionPlan, reservation.plan_id)
        if plan is not None and plan.state in {"RESERVED", "READY_FOR_PROVISION"}:
            plan.state = "EXPIRED"
            request_item = db.get(PortalComputeResourceRequest, plan.request_id)
            if request_item is not None and request_item.status == "PROVISION_PLAN_READY":
                request_item.status = "APPROVED"
                request_item.provision_plan_id = None


def _allocator_lock(db: Session) -> PortalAllocatorLock:
    row = db.scalar(
        select(PortalAllocatorLock).where(PortalAllocatorLock.id == 1).with_for_update()
    )
    if row is None:
        row = PortalAllocatorLock(id=1, version=1, updated_at=utcnow())
        db.add(row)
        db.flush()
        # The migration seeds this row in production. This branch supports
        # isolated metadata-created test databases.
        row = db.scalar(
            select(PortalAllocatorLock).where(PortalAllocatorLock.id == 1).with_for_update()
        )
    if row is None:  # pragma: no cover - defensive database invariant
        raise _error(503, "ALLOCATOR_LOCK_UNAVAILABLE", "资源分配器锁不可用")
    row.version += 1
    row.updated_at = utcnow()
    return row


def _active_reservations(db: Session) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {
        "UID": set(),
        "GID": set(),
        "PROJECT_ID": set(),
        "SSH_PORT": set(),
        "CONTAINER_NAME": set(),
    }
    rows = db.scalars(
        select(PortalResourceReservation).where(
            PortalResourceReservation.state == "RESERVED",
            PortalResourceReservation.active_key.is_not(None),
        )
    ).all()
    for row in rows:
        result.setdefault(row.resource_type, set()).add(row.resource_value)
    for managed in db.scalars(select(PortalManagedUser)).all():
        result["UID"].add(str(managed.uid))
        result["GID"].add(str(managed.gid))
        if managed.project_id is not None:
            result["PROJECT_ID"].add(str(managed.project_id))
        if managed.container_port is not None:
            result["SSH_PORT"].add(str(managed.container_port))
        if managed.container_name:
            result["CONTAINER_NAME"].add(managed.container_name)
    for container in db.scalars(select(PortalContainer)).all():
        result["CONTAINER_NAME"].add(container.name)
        if container.ssh_port is not None:
            result["SSH_PORT"].add(str(container.ssh_port))
    return result


def _worker_dry_run(
    operation_type: str,
    *,
    payload: dict[str, Any],
    context: AuthContext,
    idempotency_key: str,
) -> dict[str, Any]:
    try:
        result = call_worker(
            operation_type,
            payload=payload,
            requested_by=context.user.normalized_login,
            approved_by=context.user.normalized_login,
            idempotency_key=idempotency_key,
            dry_run=True,
            timeout_seconds=90,
        )
    except WorkerClientError as exc:
        raise _error(503, exc.code, str(exc)) from exc
    if result.get("status") != "DRY_RUN":
        raw_error = result.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        raise _error(
            409,
            str(error.get("code", "PROVISION_DRY_RUN_FAILED")),
            str(error.get("message", "资源规划 dry-run 未通过")),
        )
    return result


@router.get("/self/compute-request")
def self_compute_request(
    context: AuthContext = Depends(permission_dependency("self.compute_requests.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.scalar(
        select(PortalComputeResourceRequest)
        .where(PortalComputeResourceRequest.portal_account_id == context.user.id)
        .order_by(PortalComputeResourceRequest.created_at.desc())
    )
    return {
        "status": "OK",
        "compute_identity": "NOT_PROVISIONED",
        "request": _request_view(db, item, internal=False) if item else None,
    }


@router.post("/self/compute-request", status_code=status.HTTP_201_CREATED)
def create_self_compute_request(
    body: ComputeResourceRequestCreate,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.compute_requests.create")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    source_ip = client_ip(request)
    if not rate_limiter.allowed(
        f"compute-request-ip:{source_ip}", 30, 300
    ) or not rate_limiter.allowed(f"compute-request-account:{context.user.id}", 10, 300):
        raise _error(429, "COMPUTE_REQUEST_RATE_LIMITED", "计算资源申请提交过于频繁，请稍后重试")
    account = db.scalar(
        select(PortalUser).where(PortalUser.id == context.user.id).with_for_update()
    )
    if account is None:  # pragma: no cover - authenticated FK invariant
        raise _error(404, "PORTAL_ACCOUNT_NOT_FOUND", "Portal 账号不存在")
    _assert_identity_only(db, account)
    existing = db.scalar(
        select(PortalComputeResourceRequest).where(
            PortalComputeResourceRequest.portal_account_id == account.id,
            PortalComputeResourceRequest.active_slot == 1,
        )
    )
    if existing is not None:
        raise _error(409, "ACTIVE_COMPUTE_REQUEST_EXISTS", "已有进行中的计算资源申请")
    now = utcnow()
    item = PortalComputeResourceRequest(
        portal_account_id=account.id,
        requested_by=account.id,
        managed_user_id=None,
        username=account.normalized_login,
        status="REQUESTED",
        active_slot=1,
        requested_gpu_max=body.requested_gpu_max,
        requested_storage_bytes=body.requested_storage_bytes,
        requested_container_profile=body.requested_container_profile,
        requested_lease_seconds=body.requested_lease_seconds,
        purpose=body.purpose,
        user_note=body.user_note,
        submitted_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    try:
        db.flush()
        operation = _operation(
            db,
            context=context,
            operation_type="compute_resource_request.create",
            target_id=item.id,
            idempotency_key=str(body.idempotency_key),
            summary="Ordinary user submitted own standard compute resource request",
            payload={
                "portal_account_id": str(account.id),
                "gpu_max": body.requested_gpu_max,
                "storage_bytes": STANDARD_STORAGE_BYTES,
                "container_profile": STANDARD_CONTAINER_PROFILE,
                "lease_seconds": STANDARD_LEASE_SECONDS,
            },
            result_summary="Compute request entered REQUESTED; no infrastructure was created",
        )
        _audit(
            db,
            request,
            context,
            event_type="COMPUTE_RESOURCE_REQUEST_CREATED",
            object_type="compute_resource_request",
            object_id=str(item.id),
            metadata={
                "gpu_max": item.requested_gpu_max,
                "storage_bytes": item.requested_storage_bytes,
                "container_profile": item.requested_container_profile,
                "lease_seconds": item.requested_lease_seconds,
            },
            operation_id=operation.id,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(409, "ACTIVE_COMPUTE_REQUEST_EXISTS", "已有进行中的计算资源申请") from exc
    return {"status": "REQUESTED", "request": _request_view(db, item, internal=False)}


@router.post("/self/compute-request/{request_id}/cancel")
def cancel_self_compute_request(
    request_id: str,
    body: ComputeResourceRequestCancel,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.compute_requests.cancel")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    item = _owned_request(db, _request_id(request_id), context.user.id, lock=True)
    if item.status != "REQUESTED":
        raise _error(409, "COMPUTE_REQUEST_NOT_CANCELLABLE", "只有待审批申请可以撤回")
    item.status = "CANCELLED"
    item.active_slot = None
    item.cancelled_at = utcnow()
    item.updated_at = item.cancelled_at
    operation = _operation(
        db,
        context=context,
        operation_type="compute_resource_request.cancel",
        target_id=item.id,
        idempotency_key=str(body.idempotency_key),
        summary="Ordinary user cancelled own pending compute request",
        payload={"portal_account_id": str(item.portal_account_id), "expected_state": "REQUESTED"},
        result_summary="Compute request cancelled; no infrastructure existed",
    )
    _audit(
        db,
        request,
        context,
        event_type="COMPUTE_RESOURCE_REQUEST_CANCELLED",
        object_type="compute_resource_request",
        object_id=str(item.id),
        operation_id=operation.id,
    )
    db.commit()
    return {"status": "CANCELLED", "request": _request_view(db, item, internal=False)}


@router.get("/admin/compute-resource-requests")
def admin_compute_requests(
    context: AuthContext = Depends(permission_dependency("compute_requests.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.scalars(
        select(PortalComputeResourceRequest).order_by(
            PortalComputeResourceRequest.submitted_at.desc(),
            PortalComputeResourceRequest.created_at.desc(),
        )
    ).all()
    return {
        "status": "OK",
        "requests": [_request_view(db, item, internal=True) for item in rows],
        "count": len(rows),
    }


@router.get("/admin/compute-resource-requests/{request_id}")
def admin_compute_request_detail(
    request_id: str,
    context: AuthContext = Depends(permission_dependency("compute_requests.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = _admin_request(db, _request_id(request_id))
    return {"status": "OK", "request": _request_view(db, item, internal=True)}


@router.post("/admin/compute-resource-requests/{request_id}/review")
def review_compute_request(
    request_id: str,
    body: ComputeResourceReviewRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.review")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    item = _admin_request(db, _request_id(request_id), lock=True)
    _deny_self_administration(item, context)
    if item.status not in {"REQUESTED", "UNDER_REVIEW"}:
        raise _error(409, "COMPUTE_REQUEST_ALREADY_REVIEWED", "申请当前状态不能再次审批")
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    _assert_identity_only(db, account)
    now = utcnow()
    item.reviewed_at = now
    item.reviewed_by = context.user.id
    item.review_note = body.review_note
    item.updated_at = now
    approved = body.decision == "APPROVE"
    if approved:
        item.status = "APPROVED"
        item.approved_at = now
        event_type = "COMPUTE_RESOURCE_REQUEST_APPROVED"
    else:
        item.status = "REJECTED"
        item.active_slot = None
        item.rejected_at = now
        event_type = "COMPUTE_RESOURCE_REQUEST_REJECTED"
    operation_type = f"compute_resource_request.{body.decision.casefold()}"
    operation = _operation(
        db,
        context=context,
        operation_type=operation_type,
        target_id=item.id,
        idempotency_key=str(body.idempotency_key),
        summary=f"Administrator {body.decision.casefold()}d compute resource request",
        payload={
            "portal_account_id": str(item.portal_account_id),
            "decision": body.decision,
            "gpu_max": item.requested_gpu_max,
            "storage_bytes": item.requested_storage_bytes,
            "container_profile": item.requested_container_profile,
            "lease_seconds": item.requested_lease_seconds,
        },
        result_summary=(
            "Request approved; no provisioning or lease activation performed"
            if approved
            else "Request rejected; no infrastructure existed"
        ),
    )
    _audit(
        db,
        request,
        context,
        event_type=event_type,
        object_type="compute_resource_request",
        object_id=str(item.id),
        metadata={"decision": body.decision, "gpu_max": item.requested_gpu_max},
        operation_id=operation.id,
    )
    db.commit()
    return {"status": item.status, "request": _request_view(db, item, internal=True)}


@router.post("/admin/compute-resource-requests/{request_id}/plan")
def create_provision_plan(
    request_id: str,
    body: ComputeProvisionActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.plan")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    item = _admin_request(db, _request_id(request_id), lock=True)
    _deny_self_administration(item, context)
    if item.status == "PROVISION_PLAN_READY" and item.provision_plan_id:
        existing = db.get(PortalProvisionPlan, item.provision_plan_id)
        if existing is not None and existing.state in {"RESERVED", "READY_FOR_PROVISION"}:
            return {"status": existing.state, "plan": _plan_view(existing, internal=True)}
    if item.status != "APPROVED":
        raise _error(409, "COMPUTE_REQUEST_NOT_APPROVED", "只有已批准申请可以生成资源计划")
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    _assert_identity_only(db, account)
    if account.normalized_login != item.username:
        raise _error(409, "REQUEST_OWNER_BINDING_FAILED", "申请用户名与所属账号不一致")

    _allocator_lock(db)
    _expire_reservations(db)
    exclusions = _active_reservations(db)
    payload = {
        "request_id": str(item.id),
        "portal_account_id": str(item.portal_account_id),
        "username": item.username,
        "requested_gpu_max": item.requested_gpu_max,
        "requested_storage_bytes": item.requested_storage_bytes,
        "requested_container_profile": item.requested_container_profile,
        "requested_lease_seconds": item.requested_lease_seconds,
        "reserved_uids": sorted(int(value) for value in exclusions["UID"]),
        "reserved_gids": sorted(int(value) for value in exclusions["GID"]),
        "reserved_project_ids": sorted(int(value) for value in exclusions["PROJECT_ID"]),
        "reserved_ssh_ports": sorted(int(value) for value in exclusions["SSH_PORT"]),
        "reserved_container_names": sorted(exclusions["CONTAINER_NAME"]),
    }
    worker = _worker_dry_run(
        "compute.provision.plan",
        payload=payload,
        context=context,
        idempotency_key=f"compute-plan:{item.id}:{body.idempotency_key}",
    )
    if worker.get("plan_status") != "READY":
        raise _error(409, "PROVISION_PLAN_CONFLICT", "资源分配器发现冲突，未建立 reservation")
    now = utcnow()
    reservation_expires_at = now + RESERVATION_LIFETIME
    plan = PortalProvisionPlan(  # noqa: S604 - ORM field is a login shell path, not subprocess
        request_id=item.id,
        portal_account_id=item.portal_account_id,
        state="RESERVED",
        username=item.username,
        uid=int(worker["proposed_uid"]),
        gid=int(worker["proposed_gid"]),
        project_id=int(worker["proposed_project_id"]),
        container_name=str(worker["proposed_container_name"]),
        container_ssh_port=int(worker["proposed_ssh_port"]),
        storage_bytes=STANDARD_STORAGE_BYTES,
        container_profile=STANDARD_CONTAINER_PROFILE,
        container_cpus=8,
        container_memory_gb=32,
        container_pids_limit=4096,
        container_gpu=0,
        slurm_account="company",
        slurm_qos="general",
        gpu_max=item.requested_gpu_max,
        lease_seconds=STANDARD_LEASE_SECONDS,
        lease_state="NOT_STARTED",
        host_ssh_enabled=False,
        shell="/usr/sbin/nologin",
        password_state="LOCKED",  # noqa: S106 - lifecycle state, never a credential
        execution_enabled=False,
        reservation_expires_at=reservation_expires_at,
        allocator_result=worker,
        created_by=context.user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(plan)
    db.flush()
    values = {
        "UID": str(plan.uid),
        "GID": str(plan.gid),
        "PROJECT_ID": str(plan.project_id),
        "SSH_PORT": str(plan.container_ssh_port),
        "CONTAINER_NAME": plan.container_name,
    }
    for resource_type, resource_value in values.items():
        db.add(
            PortalResourceReservation(
                plan_id=plan.id,
                request_id=item.id,
                portal_account_id=item.portal_account_id,
                resource_type=resource_type,
                resource_value=resource_value,
                active_key=f"{resource_type}:{resource_value}",
                state="RESERVED",
                reserved_at=now,
                expires_at=reservation_expires_at,
            )
        )
    item.status = "PROVISION_PLAN_READY"
    item.provision_plan_id = plan.id
    item.updated_at = now
    try:
        db.flush()
        operation = _operation(
            db,
            context=context,
            operation_type="compute.provision.plan",
            target_id=item.id,
            idempotency_key=str(body.idempotency_key),
            summary="Allocator created a reserved, non-executable provision plan",
            payload={
                "request_id": str(item.id),
                "portal_account_id": str(item.portal_account_id),
                "gpu_max": plan.gpu_max,
                "storage_bytes": plan.storage_bytes,
                "container_profile": plan.container_profile,
                "lease_seconds": plan.lease_seconds,
                "execution_enabled": False,
            },
            result_summary="UID/GID/project/port/name reserved in Portal DB; host unchanged",
        )
        _audit(
            db,
            request,
            context,
            event_type="PROVISION_PLAN_CREATED",
            object_type="provision_plan",
            object_id=str(plan.id),
            metadata={
                "request_id": str(item.id),
                "uid": plan.uid,
                "gid": plan.gid,
                "project_id": plan.project_id,
                "ssh_port": plan.container_ssh_port,
                "reservation_state": "RESERVED",
                "execution_enabled": False,
            },
            operation_id=operation.id,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(
            409,
            "RESOURCE_RESERVATION_CONFLICT",
            "并发分配产生冲突；未建立资源计划，请重试",
        ) from exc
    return {"status": "RESERVED", "plan": _plan_view(plan, internal=True)}


def _dry_run_payload(
    item: PortalComputeResourceRequest, plan: PortalProvisionPlan
) -> dict[str, Any]:
    return {
        "request_id": str(item.id),
        "plan_id": str(plan.id),
        "portal_account_id": str(item.portal_account_id),
        "username": plan.username,
        "uid": plan.uid,
        "gid": plan.gid,
        "project_id": plan.project_id,
        "ssh_port": plan.container_ssh_port,
        "container_name": plan.container_name,
        "storage_bytes": plan.storage_bytes,
        "container_profile": plan.container_profile,
        "container_cpus": plan.container_cpus,
        "container_memory_gb": plan.container_memory_gb,
        "container_pids_limit": plan.container_pids_limit,
        "container_gpu": plan.container_gpu,
        "slurm_account": plan.slurm_account,
        "slurm_qos": plan.slurm_qos,
        "gpu_max": plan.gpu_max,
        "lease_seconds": plan.lease_seconds,
        "lease_state": plan.lease_state,
        "host_ssh": "DISABLED",
        "shell": plan.shell,
        "password_state": plan.password_state,
        "execution_enabled": False,
    }


@router.post("/admin/compute-resource-requests/{request_id}/dry-run")
def dry_run_provision_plan(
    request_id: str,
    body: ComputeProvisionActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.plan")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    item = _admin_request(db, _request_id(request_id), lock=True)
    _deny_self_administration(item, context)
    if item.status != "PROVISION_PLAN_READY" or item.provision_plan_id is None:
        raise _error(409, "PROVISION_PLAN_NOT_READY", "资源计划尚未建立")
    plan = db.scalar(
        select(PortalProvisionPlan)
        .where(PortalProvisionPlan.id == item.provision_plan_id)
        .with_for_update()
    )
    if plan is None or plan.state not in {"RESERVED", "READY_FOR_PROVISION"}:
        raise _error(409, "PROVISION_PLAN_NOT_READY", "资源计划不可用于 dry-run")
    if plan.request_id != item.id or plan.portal_account_id != item.portal_account_id:
        raise _error(409, "PROVISION_PLAN_BINDING_FAILED", "资源计划与申请归属不一致")
    account = db.get(PortalUser, item.portal_account_id)
    if account is None:
        raise _error(409, "PORTAL_ACCOUNT_NOT_FOUND", "申请所属 Portal 账号不存在")
    _assert_identity_only(db, account)
    now = utcnow()
    if ensure_utc(plan.reservation_expires_at) <= now:
        raise _error(409, "RESOURCE_RESERVATION_EXPIRED", "资源 reservation 已过期")
    reservations = db.scalars(
        select(PortalResourceReservation).where(
            PortalResourceReservation.plan_id == plan.id,
            PortalResourceReservation.state == "RESERVED",
            PortalResourceReservation.active_key.is_not(None),
        )
    ).all()
    expected_reservations = {
        ("UID", str(plan.uid)),
        ("GID", str(plan.gid)),
        ("PROJECT_ID", str(plan.project_id)),
        ("SSH_PORT", str(plan.container_ssh_port)),
        ("CONTAINER_NAME", plan.container_name),
    }
    actual_reservations = {
        (reservation.resource_type, reservation.resource_value) for reservation in reservations
    }
    if actual_reservations != expected_reservations or any(
        reservation.request_id != item.id
        or reservation.portal_account_id != item.portal_account_id
        or reservation.active_key != f"{reservation.resource_type}:{reservation.resource_value}"
        or ensure_utc(reservation.expires_at) <= now
        for reservation in reservations
    ):
        raise _error(409, "RESOURCE_RESERVATION_BINDING_FAILED", "资源 reservation 绑定不完整")
    worker = _worker_dry_run(
        "compute.provision.dry_run",
        payload=_dry_run_payload(item, plan),
        context=context,
        idempotency_key=f"compute-dry-run:{plan.id}:{body.idempotency_key}",
    )
    if worker.get("dry_run_status") != "READY_FOR_PROVISION":
        plan.dry_run_result = worker
        plan.updated_at = utcnow()
        db.commit()
        raise _error(409, "PROVISION_DRY_RUN_CONFLICT", "Provision dry-run 未通过")
    now = utcnow()
    plan.state = "READY_FOR_PROVISION"
    plan.dry_run_result = worker
    plan.dry_run_at = now
    plan.updated_at = now
    operation = _operation(
        db,
        context=context,
        operation_type="compute.provision.dry_run",
        target_id=item.id,
        idempotency_key=str(body.idempotency_key),
        summary="Worker revalidated the exact reserved plan without writes",
        payload={
            "request_id": str(item.id),
            "plan_id": str(plan.id),
            "portal_account_id": str(item.portal_account_id),
            "execution_enabled": False,
        },
        result_summary="READY_FOR_PROVISION; infrastructure side effects remained zero",
    )
    _audit(
        db,
        request,
        context,
        event_type="PROVISION_DRY_RUN_COMPLETED",
        object_type="provision_plan",
        object_id=str(plan.id),
        metadata={
            "request_id": str(item.id),
            "dry_run_status": "READY_FOR_PROVISION",
            "execution_enabled": False,
            "infrastructure_side_effects": "NONE",
            "lease_state": "NOT_STARTED",
        },
        operation_id=operation.id,
    )
    db.commit()
    return {"status": "READY_FOR_PROVISION", "plan": _plan_view(plan, internal=True)}


@router.post("/admin/compute-resource-requests/{request_id}/provision")
def disabled_real_provision(
    request_id: str,
    body: ComputeProvisionActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("compute_requests.plan")),
    db: Session = Depends(get_db),
) -> None:
    require_session_csrf(request, context)
    item = _admin_request(db, _request_id(request_id))
    _deny_self_administration(item, context)
    _audit(
        db,
        request,
        context,
        event_type="PROVISION_EXECUTION_DENIED_NEXT_GATE",
        object_type="compute_resource_request",
        object_id=str(item.id),
        result="DENIED",
        metadata={
            "execution_enabled": False,
            "idempotency_key_present": bool(body.idempotency_key),
        },
    )
    db.commit()
    raise _error(
        409,
        "PROVISION_EXECUTION_DISABLED_NEXT_GATE",
        "正式创建计算环境需要下一阶段管理员确认",
    )


def assert_zero_compute_side_effects(db: Session, account_id: uuid.UUID) -> dict[str, bool]:
    """Reusable acceptance assertion for API tests and deployment diagnostics."""
    managed_ids = select(PortalManagedUser.id).where(PortalManagedUser.portal_user_id == account_id)
    return {
        "managed_user_absent": db.scalar(
            select(PortalManagedUser.id).where(PortalManagedUser.portal_user_id == account_id)
        )
        is None,
        "container_absent": db.scalar(
            select(PortalContainer.id).where(PortalContainer.managed_user_id.in_(managed_ids))
        )
        is None,
        "storage_absent": db.scalar(
            select(PortalStorageResource.id).where(
                PortalStorageResource.owner_managed_user_id.in_(managed_ids)
            )
        )
        is None,
        "lease_absent": db.scalar(
            select(PortalComputeLease.id).where(PortalComputeLease.managed_user_id.in_(managed_ids))
        )
        is None,
    }
