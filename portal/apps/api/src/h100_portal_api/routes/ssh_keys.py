import uuid
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from h100_portal_contracts.workspace import (
    CPU_DEVELOPMENT_PROFILE,
    GPU_DEVELOPMENT_PROFILE,
    workspace_path,
)
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.auth import AuthContext, client_ip, require_session_csrf, user_agent
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import auth_context
from h100_portal_api.enums import OnboardingState, OperationStatus, RiskLevel
from h100_portal_api.models import (
    PortalComputeLease,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalOperationEvent,
    PortalSshKey,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.rbac import has_permission, highest_role
from h100_portal_api.runtime_identity import deployment_version
from h100_portal_api.schemas import SshKeyEnrollRequest, SshKeySyncRequest
from h100_portal_api.ssh_keys import (
    SshPublicKeyValidationError,
    contains_private_key_material,
    ssh_enrollment_status,
    ssh_key_response,
    validate_ssh_public_key,
)
from h100_portal_api.worker_client import WorkerClientError, call_worker

router = APIRouter(prefix="/users", tags=["ssh-keys"])

FORBIDDEN_KEY_FIELDS = {
    "private_key",
    "raw_private_key",
    "private_key_password",
    "private_key_path",
    "password",
}


def _target_user(user_id: str, context: AuthContext, db: Session, *, write: bool) -> PortalUser:
    try:
        target_id = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"}
        ) from exc
    target = db.get(PortalUser, target_id)
    permission = "users.write" if write else "users.read"
    if target is None:
        raise HTTPException(
            status_code=404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"}
        )
    if target.id != context.user.id and not has_permission(context.user, permission):
        raise HTTPException(
            status_code=403, detail={"code": "FORBIDDEN", "message": "当前账号无权管理该 SSH Key"}
        )
    return target


def _managed_user(target: PortalUser, db: Session, *, lock: bool = False) -> PortalManagedUser:
    statement = select(PortalManagedUser).where(PortalManagedUser.portal_user_id == target.id)
    if lock:
        statement = statement.with_for_update()
    managed = db.scalar(statement)
    if managed is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "COMPUTE_IDENTITY_NOT_FOUND", "message": "当前账号没有受管计算身份"},
        )
    if managed.onboarding_state not in {OnboardingState.STAGED, OnboardingState.ACTIVE}:
        raise HTTPException(
            status_code=409,
            detail={"code": "COMPUTE_IDENTITY_NOT_STAGED", "message": "计算身份尚未完成 Stage"},
        )
    return managed


def _private_material_in_request(value: object, *, depth: int = 0) -> bool:
    """Detect forbidden fields and standard private-key armor without retaining it."""
    if depth > 8:
        return False
    if isinstance(value, dict):
        if {str(key).casefold() for key in value} & FORBIDDEN_KEY_FIELDS:
            return True
        return any(_private_material_in_request(item, depth=depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_private_material_in_request(item, depth=depth + 1) for item in value[:100])
    return contains_private_key_material(value)


def _record_rejection(
    db: Session,
    request: Request,
    context: AuthContext,
    target: PortalUser,
    *,
    event_type: str,
    code: str,
) -> None:
    record_audit(
        db,
        event_type=event_type,
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="ssh_public_key",
        object_id=str(target.id),
        result="DENIED",
        metadata={"error_code": code, "body_stored": False},
    )
    db.commit()


def _discard_staging_file(
    *,
    record_id: uuid.UUID,
    operation_id: uuid.UUID,
    content_sha256: str,
    actor: str,
) -> None:
    with suppress(WorkerClientError):
        call_worker(
            "ssh_key.discard",
            payload={
                "record_id": str(record_id),
                "operation_id": str(operation_id),
                "content_sha256": content_sha256,
            },
            requested_by=actor,
            approved_by=actor,
            idempotency_key=f"ssh-key-discard:{record_id}",
            dry_run=False,
            timeout_seconds=10,
        )


@router.get("/{user_id}/ssh-keys")
def list_ssh_keys(
    user_id: str,
    context: AuthContext = Depends(auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    target = _target_user(user_id, context, db, write=False)
    managed = _managed_user(target, db)
    records = db.scalars(
        select(PortalSshKey)
        .where(PortalSshKey.managed_user_id == managed.id)
        .order_by(PortalSshKey.created_at.desc())
    ).all()
    return {
        "status": "OK",
        "keys": [ssh_key_response(record) for record in records],
        "count": len(records),
        "maximum_active_keys": 5,
        "enrollment": ssh_enrollment_status(db, target),
    }


def _sync_failure(
    db: Session,
    request: Request,
    context: AuthContext,
    operation_id: uuid.UUID,
    *,
    code: str,
    rollback_status: str,
) -> None:
    operation = db.get(PortalOperation, operation_id)
    if operation is None or operation.status != OperationStatus.RUNNING:
        return
    operation.status = OperationStatus.FAILED
    operation.error_code = code[:64]
    operation.rollback_status = rollback_status[:32]
    operation.result_summary = "Active Container SSH key sync failed closed"
    operation.finished_at = utcnow()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=OperationStatus.RUNNING,
            to_status=OperationStatus.FAILED,
            safe_message="Active Container SSH key sync failed closed",
            created_at=utcnow(),
        )
    )
    record_audit(
        db,
        event_type="ssh_key.active_container_sync_failed",
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="ssh_public_key_set",
        object_id=str(operation.owner_managed_user_id),
        operation_id=operation.id,
        result="FAILED",
        metadata={"error_code": code[:64], "rollback_status": rollback_status[:32]},
    )
    db.commit()


@router.post("/{user_id}/ssh-keys/sync")
def sync_active_container_ssh_keys(
    user_id: str,
    body: SshKeySyncRequest,
    request: Request,
    context: AuthContext = Depends(auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Install newly validated keys into only the authenticated owner's active Container."""

    require_session_csrf(request, context)
    target = _target_user(user_id, context, db, write=True)
    if target.id != context.user.id:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SSH_KEY_SYNC_OWNER_REQUIRED",
                "message": "运行中容器的 SSH Key 只能由用户本人同步",
            },
        )
    managed = _managed_user(target, db, lock=True)
    if (
        managed.onboarding_state != OnboardingState.ACTIVE
        or managed.compute_environment_state != "ACTIVE"
        or managed.host_access_state != "DISABLED_BY_PLATFORM_POLICY"
        or managed.shell != "/usr/sbin/nologin"
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SSH_KEY_SYNC_ENVIRONMENT_NOT_ACTIVE",
                "message": "只有安全运行中的计算环境可以同步新增容器公钥",
            },
        )
    container = db.scalar(
        select(PortalContainer)
        .where(
            PortalContainer.managed_user_id == managed.id,
            PortalContainer.owner_managed_user_id == managed.id,
        )
        .with_for_update()
    )
    lease = db.scalar(
        select(PortalComputeLease)
        .where(PortalComputeLease.owner_managed_user_id == managed.id)
        .order_by(PortalComputeLease.starts_at.desc())
        .with_for_update()
    )
    if (
        container is None
        or lease is None
        or container.name != managed.container_name
        or container.ssh_port != managed.container_port
        or container.desired_state != "RUNNING"
        or container.observed_state != "RUNNING"
        or container.development_profile not in {CPU_DEVELOPMENT_PROFILE, GPU_DEVELOPMENT_PROFILE}
        or lease.state not in {"ACTIVE", "RENEWAL_WINDOW", "RENEWAL_PENDING"}
        or ensure_utc(lease.expires_at) <= datetime.now(UTC)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SSH_KEY_SYNC_RUNTIME_NOT_READY",
                "message": "容器或 Lease 当前不满足密钥同步条件",
            },
        )
    allocation_pair_valid = (container.gpu_allocation_job_id is None) == (
        container.gpu_allocation_uuid is None
    )
    allocation_profile_valid = (
        container.development_profile == GPU_DEVELOPMENT_PROFILE
        or container.gpu_allocation_job_id is None
    )
    if not allocation_pair_valid or not allocation_profile_valid:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SSH_KEY_SYNC_GPU_BINDING_INCOMPLETE",
                "message": "GPU Development 资源绑定不完整",
            },
        )
    operation_key = f"ssh-key-sync:{managed.id}:{body.idempotency_key}"
    operation = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.idempotency_key == operation_key,
        )
    )
    if operation is not None and (
        operation.operation_type != "ssh_key.sync_active_container"
        or operation.owner_managed_user_id != managed.id
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "IDEMPOTENCY_CONFLICT", "message": "幂等键已用于其他操作"},
        )
    if operation is not None and operation.status == OperationStatus.SUCCEEDED:
        return {
            "status": "INSTALLED",
            "operation_id": str(operation.id),
            "container_state": "RUNNING",
            "key_fingerprints": operation.validated_payload.get("ssh_key_fingerprints", []),
            "idempotent_replay": True,
        }
    if operation is not None and operation.status != OperationStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail={"code": "SSH_KEY_SYNC_RETRY_REQUIRED", "message": "上次同步失败，请重新提交"},
        )
    keys = list(
        db.scalars(
            select(PortalSshKey)
            .where(
                PortalSshKey.managed_user_id == managed.id,
                PortalSshKey.owner_managed_user_id == managed.id,
                PortalSshKey.active.is_(True),
                PortalSshKey.state.in_({"VALIDATED", "INSTALLED"}),
                PortalSshKey.scope == "CONTAINER",
            )
            .order_by(PortalSshKey.created_at, PortalSshKey.id)
            .with_for_update()
        ).all()
    )
    current_keys = [
        key
        for key in keys
        if key.state == "INSTALLED" and key.container_install_state == "INSTALLED"
    ]
    if (
        not current_keys
        or len(keys) <= len(current_keys)
        or keys[: len(current_keys)] != current_keys
        or any(key.host_install_state != "NOT_INSTALLED" for key in keys)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SSH_KEY_SYNC_SET_NOT_READY",
                "message": "没有待安装的新容器公钥，或当前 Key 集合不一致",
            },
        )

    if operation is None:
        concurrent = db.scalar(
            select(PortalOperation).where(
                PortalOperation.owner_managed_user_id == managed.id,
                PortalOperation.operation_type == "ssh_key.sync_active_container",
                PortalOperation.status == OperationStatus.RUNNING,
            )
        )
        if concurrent is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "SSH_KEY_SYNC_IN_PROGRESS", "message": "SSH Key 正在同步"},
            )
        operation = PortalOperation(
            operation_type="ssh_key.sync_active_container",
            target_type="ssh_public_key_set",
            target_id=str(managed.id),
            requested_by=context.user.id,
            owner_managed_user_id=managed.id,
            approved_by=context.user.id,
            request_summary=f"为 {managed.unix_username} 同步新增容器公钥",
            validated_payload={},
            idempotency_key=operation_key,
            risk_level=RiskLevel.MEDIUM,
            status=OperationStatus.RUNNING,
            created_at=utcnow(),
            approved_at=utcnow(),
            started_at=utcnow(),
        )
        db.add(operation)
        db.flush()
        operation.validated_payload = {
            "sync_operation_id": str(operation.id),
            "managed_user_id": str(managed.id),
            "owner_login": context.user.normalized_login,
            "username": managed.unix_username,
            "uid": managed.uid,
            "gid": managed.gid,
            "ssh_port": managed.container_port,
            "container_name": container.name,
            "workspace_path": str(workspace_path(managed.uid)),
            "development_profile": container.development_profile,
            "slurm_account": managed.slurm_account,
            "slurm_qos": managed.slurm_qos,
            "lease_id": str(lease.id),
            "lease_expires_at": ensure_utc(lease.expires_at).isoformat(),
            "gpu_allocation_job_id": container.gpu_allocation_job_id,
            "gpu_allocation_uuid": container.gpu_allocation_uuid,
            "current_ssh_key_record_ids": [str(key.id) for key in current_keys],
            "current_ssh_key_fingerprints": [key.fingerprint_sha256 for key in current_keys],
            "ssh_key_record_ids": [str(key.id) for key in keys],
            "ssh_key_fingerprints": [key.fingerprint_sha256 for key in keys],
            "deployment_version": deployment_version(),
        }
        db.add(
            PortalOperationEvent(
                operation_id=operation.id,
                from_status=None,
                to_status=OperationStatus.RUNNING,
                safe_message="Owner-bound active Container SSH key sync started",
                created_at=utcnow(),
            )
        )

    payload = dict(operation.validated_payload)
    try:
        worker_result = call_worker(
            "ssh_key.sync_active_container",
            payload=payload,
            requested_by=context.user.normalized_login,
            approved_by=context.user.normalized_login,
            idempotency_key=f"ssh-key-sync:{operation.id}",
            dry_run=False,
            timeout_seconds=45,
        )
    except WorkerClientError as exc:
        _sync_failure(
            db,
            request,
            context,
            operation.id,
            code=exc.code,
            rollback_status="UNKNOWN",
        )
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": "Root Worker 无法同步容器公钥"},
        ) from exc
    expected_fingerprints = list(payload["ssh_key_fingerprints"])
    worker_ok = (
        worker_result.get("status") == "SUCCEEDED"
        and worker_result.get("handler") == "ssh_key.sync_active_container"
        and worker_result.get("sync_operation_id") == str(operation.id)
        and worker_result.get("managed_user_id") == str(managed.id)
        and worker_result.get("username") == managed.unix_username
        and worker_result.get("container_name") == container.name
        and worker_result.get("container_state") == "RUNNING"
        and worker_result.get("container_key_fingerprints") == expected_fingerprints
        and worker_result.get("host_authorized_keys") == "ABSENT"
    )
    if not worker_ok:
        code = str(worker_result.get("error", {}).get("code", "SSH_KEY_SYNC_RESULT_INVALID"))
        rollback_status = str(worker_result.get("rollback_status", "UNKNOWN"))
        _sync_failure(
            db,
            request,
            context,
            operation.id,
            code=code,
            rollback_status=rollback_status,
        )
        raise HTTPException(
            status_code=409,
            detail={"code": code[:64], "message": "容器公钥同步失败，已执行安全回滚检查"},
        )

    persisted_operation = db.scalar(
        select(PortalOperation).where(PortalOperation.id == operation.id).with_for_update()
    )
    persisted_keys = list(
        db.scalars(
            select(PortalSshKey)
            .where(
                PortalSshKey.id.in_([uuid.UUID(value) for value in payload["ssh_key_record_ids"]])
            )
            .order_by(PortalSshKey.created_at, PortalSshKey.id)
            .with_for_update()
        ).all()
    )
    if (
        persisted_operation is None
        or persisted_operation.status != OperationStatus.RUNNING
        or [str(key.id) for key in persisted_keys] != payload["ssh_key_record_ids"]
        or any(
            key.owner_managed_user_id != managed.id
            or not key.active
            or key.state not in {"VALIDATED", "INSTALLED"}
            or key.scope != "CONTAINER"
            for key in persisted_keys
        )
    ):
        _sync_failure(
            db,
            request,
            context,
            operation.id,
            code="SSH_KEY_SYNC_CONCURRENT_STATE_CHANGE",
            rollback_status="NOT_REQUIRED",
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SSH_KEY_SYNC_CONCURRENT_STATE_CHANGE",
                "message": "Key 状态发生并发变化",
            },
        )
    installed_at = utcnow()
    for key in persisted_keys:
        key.state = "INSTALLED"
        key.container_install_state = "INSTALLED"
        key.host_install_state = "NOT_INSTALLED"
        key.installed_at = key.installed_at or installed_at
    persisted_operation.status = OperationStatus.SUCCEEDED
    persisted_operation.finished_at = installed_at
    persisted_operation.rollback_status = "NOT_REQUIRED"
    persisted_operation.worker_execution_id = str(worker_result.get("worker_request_id", "worker"))[
        :64
    ]
    persisted_operation.result_summary = "Owner-approved keys installed in active Container"
    db.add(
        PortalOperationEvent(
            operation_id=persisted_operation.id,
            from_status=OperationStatus.RUNNING,
            to_status=OperationStatus.SUCCEEDED,
            safe_message="Owner-approved active Container key set synchronized",
            created_at=installed_at,
        )
    )
    record_audit(
        db,
        event_type="ssh_key.active_container_synced",
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="ssh_public_key_set",
        object_id=str(managed.id),
        operation_id=persisted_operation.id,
        result="SUCCESS",
        metadata={
            "fingerprints": expected_fingerprints,
            "key_count": len(expected_fingerprints),
            "host_authorized_keys": "ABSENT",
        },
    )
    db.commit()
    return {
        "status": "INSTALLED",
        "operation_id": str(persisted_operation.id),
        "container_state": "RUNNING",
        "key_fingerprints": expected_fingerprints,
        "idempotent_replay": bool(worker_result.get("idempotent_replay", False)),
    }


@router.post("/{user_id}/ssh-keys", status_code=201)
def enroll_ssh_key(
    user_id: str,
    body: dict[str, Any],
    request: Request,
    context: AuthContext = Depends(auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    target = _target_user(user_id, context, db, write=True)
    if _private_material_in_request(body):
        _record_rejection(
            db,
            request,
            context,
            target,
            event_type="SSH_PRIVATE_KEY_UPLOAD_REJECTED",
            code="SSH_PRIVATE_KEY_UPLOAD_REJECTED",
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SSH_PRIVATE_KEY_UPLOAD_REJECTED",
                "message": "只允许提交 SSH 公钥；平台不会接收或保存私钥",
            },
        )
    try:
        enrollment = SshKeyEnrollRequest.model_validate(body)
        validated = validate_ssh_public_key(enrollment.public_key, enrollment.comment)
    except ValidationError as exc:
        _record_rejection(
            db,
            request,
            context,
            target,
            event_type="ssh_key.validation_rejected",
            code="SSH_KEY_ENROLLMENT_REJECTED",
        )
        raise HTTPException(
            status_code=422,
            detail={"code": "SSH_KEY_ENROLLMENT_REJECTED", "message": "SSH Key 请求字段无效"},
        ) from exc
    except SshPublicKeyValidationError as exc:
        event_type = (
            "SSH_PRIVATE_KEY_UPLOAD_REJECTED"
            if exc.code == "SSH_PRIVATE_KEY_UPLOAD_REJECTED"
            else "ssh_key.validation_rejected"
        )
        _record_rejection(db, request, context, target, event_type=event_type, code=exc.code)
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    if highest_role(context.user) == "user" and enrollment.scope != "CONTAINER":
        _record_rejection(
            db,
            request,
            context,
            target,
            event_type="ssh_key.scope_rejected",
            code="HOST_KEY_SCOPE_FORBIDDEN",
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "HOST_KEY_SCOPE_FORBIDDEN", "message": "普通用户密钥仅可用于开发容器"},
        )
    if enrollment.key_type != validated.key_type:
        _record_rejection(
            db,
            request,
            context,
            target,
            event_type="ssh_key.validation_rejected",
            code="SSH_PUBLIC_KEY_TYPE_MISMATCH",
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SSH_PUBLIC_KEY_TYPE_MISMATCH",
                "message": "请求 key type 与 SSH 公钥内容不一致",
            },
        )
    if (
        enrollment.client_fingerprint_sha256 is not None
        and enrollment.client_fingerprint_sha256 != validated.fingerprint_sha256
    ):
        _record_rejection(
            db,
            request,
            context,
            target,
            event_type="ssh_key.validation_rejected",
            code="SSH_KEY_FINGERPRINT_MISMATCH",
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SSH_KEY_FINGERPRINT_MISMATCH",
                "message": "浏览器与服务器计算的 fingerprint 不一致",
            },
        )

    managed = _managed_user(target, db, lock=True)
    if (
        managed.host_access_state == "DISABLED_BY_PLATFORM_POLICY"
        and enrollment.scope != "CONTAINER"
    ):
        _record_rejection(
            db,
            request,
            context,
            target,
            event_type="ssh_key.scope_rejected",
            code="HOST_KEY_SCOPE_FORBIDDEN",
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "HOST_KEY_SCOPE_FORBIDDEN", "message": "受管用户密钥仅可用于开发容器"},
        )
    active_count = int(
        db.scalar(
            select(func.count(PortalSshKey.id)).where(
                PortalSshKey.managed_user_id == managed.id,
                PortalSshKey.state.in_({"VALIDATED", "INSTALLED"}),
                PortalSshKey.active.is_(True),
            )
        )
        or 0
    )
    if active_count >= 5:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SSH_KEY_LIMIT_REACHED",
                "message": "每个计算身份最多保留 5 把有效 Key",
            },
        )
    duplicate = db.scalar(
        select(PortalSshKey).where(PortalSshKey.fingerprint_sha256 == validated.fingerprint_sha256)
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SSH_KEY_FINGERPRINT_CONFLICT",
                "message": "该 fingerprint 已登记或保留在撤销历史中",
            },
        )

    record_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    now = utcnow()
    operation = PortalOperation(
        id=operation_id,
        operation_type="ssh_key.enroll",
        target_type="ssh_public_key",
        target_id=str(record_id),
        requested_by=context.user.id,
        owner_managed_user_id=managed.id,
        approved_by=context.user.id,
        request_summary=f"为 {managed.unix_username} 登记自助 SSH 公钥",
        validated_payload={
            "record_id": str(record_id),
            "managed_user_id": str(managed.id),
            "username": managed.unix_username,
            "key_type": validated.key_type,
            "fingerprint_sha256": validated.fingerprint_sha256,
            "scope": enrollment.scope,
            "generation_method": enrollment.generation_method,
        },
        idempotency_key=f"ssh-key-enroll:{record_id}",
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.RUNNING,
        created_at=now,
        approved_at=now,
        started_at=now,
    )
    db.add(operation)
    db.flush()
    try:
        worker_result = call_worker(
            "ssh_key.prepare",
            payload={
                "record_id": str(record_id),
                "operation_id": str(operation_id),
                "managed_user_id": str(managed.id),
                "username": managed.unix_username,
                "public_key": validated.public_key,
                "key_type": validated.key_type,
                "fingerprint_sha256": validated.fingerprint_sha256,
                "content_sha256": validated.content_sha256,
                "scope": enrollment.scope,
            },
            requested_by=context.user.normalized_login,
            approved_by=context.user.normalized_login,
            idempotency_key=f"ssh-key-enroll:{record_id}",
            dry_run=False,
            timeout_seconds=15,
        )
    except WorkerClientError as exc:
        db.rollback()
        _discard_staging_file(
            record_id=record_id,
            operation_id=operation_id,
            content_sha256=validated.content_sha256,
            actor=context.user.normalized_login,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": "Root Worker 无法准备受控公钥记录"},
        ) from exc
    if (
        worker_result.get("status") != "SUCCEEDED"
        or worker_result.get("record_id") != str(record_id)
        or worker_result.get("fingerprint_sha256") != validated.fingerprint_sha256
        or worker_result.get("content_sha256") != validated.content_sha256
    ):
        db.rollback()
        _discard_staging_file(
            record_id=record_id,
            operation_id=operation_id,
            content_sha256=validated.content_sha256,
            actor=context.user.normalized_login,
        )
        code = str(worker_result.get("error", {}).get("code", "SSH_KEY_PREPARE_FAILED"))
        raise HTTPException(
            status_code=422,
            detail={"code": code[:64], "message": "Root Worker 拒绝了公钥记录"},
        )

    record = PortalSshKey(
        id=record_id,
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        key_type=validated.key_type,
        fingerprint_sha256=validated.fingerprint_sha256,
        public_key=validated.public_key,
        comment=validated.comment,
        scope=enrollment.scope,
        state="VALIDATED",
        host_install_state="NOT_INSTALLED",
        container_install_state="NOT_INSTALLED",
        generation_method=enrollment.generation_method,
        created_by=context.user.id,
        enrollment_operation_id=operation.id,
        staging_file_name=f"{record_id}.pub",
        content_sha256=validated.content_sha256,
        approved_by=context.user.id,
        approved_at=now,
        validated_at=now,
        installed_at=None,
        active=True,
        created_at=now,
        revoked_at=None,
    )
    db.add(record)
    managed.ssh_key_count = active_count + 1
    managed.ssh_key_state = (
        "VALIDATED" if managed.onboarding_state == OnboardingState.STAGED else "INSTALLED"
    )
    operation.status = OperationStatus.SUCCEEDED
    operation.finished_at = utcnow()
    operation.worker_execution_id = str(worker_result.get("request_id", "worker"))[:64]
    operation.result_summary = "SSH 公钥已验证并写入 root-owned staging；尚未安装 authorized_keys"
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=OperationStatus.RUNNING,
            to_status=OperationStatus.SUCCEEDED,
            safe_message="Public key validated and staged; private key was never received",
            created_at=utcnow(),
        )
    )
    record_audit(
        db,
        event_type="ssh_key.enrolled",
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="ssh_public_key",
        object_id=str(record.id),
        operation_id=operation.id,
        result="SUCCESS",
        metadata={
            "managed_user_id": str(managed.id),
            "key_type": record.key_type,
            "fingerprint_sha256": record.fingerprint_sha256,
            "scope": record.scope,
            "generation_method": record.generation_method,
            "private_key_received": False,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _discard_staging_file(
            record_id=record_id,
            operation_id=operation_id,
            content_sha256=validated.content_sha256,
            actor=context.user.normalized_login,
        )
        raise HTTPException(
            status_code=409,
            detail={"code": "SSH_KEY_FINGERPRINT_CONFLICT", "message": "SSH Key 记录发生冲突"},
        ) from exc
    return {
        "status": "VALIDATED",
        "key": ssh_key_response(record),
        "private_key_received": False,
        "authorized_keys_installed": False,
    }
