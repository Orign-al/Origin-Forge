import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
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
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import permission_dependency
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
    PortalLeaseRenewalRequest,
    PortalManagedUser,
    PortalOperation,
    PortalResourceRecycleItem,
    PortalResourceRestoreRequest,
    PortalSshKey,
    PortalStorageResource,
    ensure_utc,
    utcnow,
)
from h100_portal_api.rbac import highest_role
from h100_portal_api.schemas import (
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
from h100_portal_api.terminal_service import (
    TerminalRecord,
    TerminalServiceError,
    terminal_registry,
)
from h100_portal_api.worker_client import WorkerClientError, call_worker

router = APIRouter(tags=["self-service"])

APPROVED_HOST = "10.82.36.1"
APPROVED_IMAGE_REFS = {
    "nvcr.io#nvidia/cuda:13.2.0-base-ubuntu24.04@"
    "sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a"
}


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _relative_path(value: str, field: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise _error(422, "PATH_OUTSIDE_WORKSPACE", f"{field}必须位于用户工作区")
    normalized = str(path)
    if "\x00" in normalized or normalized.startswith("/"):
        raise _error(422, "PATH_OUTSIDE_WORKSPACE", f"{field}必须位于用户工作区")
    return normalized


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
        raise _error(409, code, message)
    return result


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
) -> PortalOperation:
    operation = PortalOperation(
        operation_type=operation_type,
        target_type=target_type,
        target_id=target_id,
        requested_by=context.user.id,
        owner_managed_user_id=owner_id,
        approved_by=context.user.id,
        request_summary=summary,
        validated_payload=payload,
        idempotency_key=idempotency_key,
        risk_level=risk_level,
        status=OperationStatus.RUNNING,
        approved_at=utcnow(),
        started_at=utcnow(),
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
        "gpu": "NONE",
        "cpus": container.safe_spec.get("cpus", 8),
        "memory_gb": container.safe_spec.get("memory_gb", 32),
        "pids_limit": container.safe_spec.get("pids_limit", 4096),
        "privileged": False,
        "docker_socket": False,
        "munge": False,
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
    )
    _audit(
        db,
        request,
        context,
        event_type="LEASE_RENEWAL_REQUESTED",
        object_type="lease_renewal_request",
        object_id=str(renewal.id),
        metadata={"duration_seconds": renewal.requested_duration_seconds},
    )
    db.commit()
    return {"status": "REQUESTED", "renewal_request_id": str(renewal.id)}


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
    return {
        "status": "OK",
        "connection": {
            "available": available,
            "host": APPROVED_HOST,
            "port": container.ssh_port,
            "username": managed.unix_username,
            "authentication": "SSH_PUBLIC_KEY",
            "gpu": "NONE",
            "key_fingerprint": key.fingerprint_sha256 if key else None,
            "command": (
                f"ssh -i <你的私钥路径> -p {container.ssh_port} "
                f"{managed.unix_username}@{APPROVED_HOST}"
            )
            if available
            else None,
            "vscode": (
                f"Host h100-{managed.unix_username}-dev\n"
                f"    HostName {APPROVED_HOST}\n"
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
    lease_deadline: datetime | None = None
    if action in {"start", "restart"}:
        try:
            active, terminal = entitlement(db, managed.id, lock=True)
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
        lease_deadline = terminal.expires_at
        if managed.compute_environment_state != "ACTIVE":
            raise _error(409, "CONTAINER_OPERATION_DENIED_LEASE_INACTIVE", "计算环境不可用")
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
        "lease_id": str(lease_id) if lease_id else None,
        "lease_expires_at": ensure_utc(lease_deadline).isoformat() if lease_deadline else None,
        "expected_gpu": "NONE",
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
        operation.error_code = str(getattr(exc, "detail", {}).get("code", "WORKER_FAILED"))
        db.commit()
        raise
    expected_state = "STOPPED" if action == "stop" else "RUNNING"
    if result.get("container_state") != expected_state or result.get("container_gpu") != "NONE":
        operation.status = OperationStatus.FAILED
        operation.error_code = "CONTAINER_POSTCONDITION_FAILED"
        operation.finished_at = utcnow()
        db.commit()
        raise _error(409, "CONTAINER_POSTCONDITION_FAILED", "容器安全后置条件失败")
    container.observed_state = expected_state
    container.desired_state = expected_state
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
        metadata={"state": expected_state, "gpu": "NONE"},
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
            entitlement(db, managed.id)
        except HTTPException as exc:
            record.worker.request_close()
            raise _error(
                409, "TERMINAL_DENIED_LEASE_INACTIVE", "租约失效时不能使用网页终端"
            ) from exc
        container = _container_for_owner(db, managed.id)
        if (
            managed.compute_environment_state != "ACTIVE"
            or managed.host_access_state != "DISABLED_BY_PLATFORM_POLICY"
            or managed.shell != "/usr/sbin/nologin"
            or container.id != record.container_id
            or container.observed_state != "RUNNING"
        ):
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
    require_session_csrf(request, context)
    managed = managed_identity_for_user(db, context.user, lock=True)
    active_lease, terminal_lease = entitlement(db, managed.id, lock=True)
    container = _container_for_owner(db, managed.id, lock=True)
    if (
        managed.compute_environment_state != "ACTIVE"
        or managed.host_access_state != "DISABLED_BY_PLATFORM_POLICY"
        or managed.shell != "/usr/sbin/nologin"
        or container.observed_state != "RUNNING"
    ):
        raise _error(409, "TERMINAL_SECURITY_GATE_FAILED", "开发容器终端当前不可用")
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
        "lease_id": str(active_lease.id),
        "lease_expires_at": ensure_utc(terminal_lease.expires_at).isoformat(),
        "expected_gpu": "NONE",
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
            metadata={"code": exc.code, "gpu": "NONE", "host_access": "DISABLED"},
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
            "gpu": "NONE",
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
            "gpu": "NONE",
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


def _job_view(job: PortalJob) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "slurm_job_id": job.slurm_job_id,
        "name": job.name,
        "state": job.state,
        "cpus": job.requested_cpus,
        "memory_mb": job.memory_mb,
        "gpu_count": job.gpu_count,
        "time_limit_seconds": job.time_limit_seconds,
        "script_path": job.script_relative_path,
        "workdir": job.workdir_relative_path,
        "stdout_path": job.stdout_relative_path,
        "stderr_path": job.stderr_relative_path,
        "lease_deadline_at": ensure_utc(job.lease_deadline_at).isoformat(),
        "created_at": ensure_utc(job.created_at).isoformat(),
        "submitted_at": ensure_utc(job.submitted_at).isoformat() if job.submitted_at else None,
        "finished_at": ensure_utc(job.finished_at).isoformat() if job.finished_at else None,
        "exit_code": job.exit_code,
    }


def _refresh_job(context: AuthContext, managed: Any, job: PortalJob) -> None:
    if job.slurm_job_id is None:
        return
    try:
        result = call_worker(
            "self.job.status.read",
            payload={
                "portal_job_id": str(job.id),
                "managed_user_id": str(managed.id),
                "username": managed.unix_username,
                "uid": managed.uid,
                "gid": managed.gid,
                "slurm_job_id": job.slurm_job_id,
            },
            requested_by=context.user.normalized_login,
            idempotency_key=f"self-job-status:{job.id}",
            dry_run=False,
            timeout_seconds=25,
        )
    except WorkerClientError:
        return
    if result.get("status") != "OK" or result.get("slurm_user") != managed.unix_username:
        return
    job.state = str(result.get("job_state", job.state)).split("+", 1)[0]
    exit_code = result.get("exit_code")
    job.exit_code = str(exit_code)[:32] if exit_code else job.exit_code
    if job.state in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}:
        job.finished_at = job.finished_at or utcnow()


@router.get("/self/jobs")
def self_jobs(
    context: AuthContext = Depends(permission_dependency("self.jobs.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    managed = managed_identity_for_user(db, context.user)
    rows = db.scalars(
        select(PortalJob)
        .where(PortalJob.owner_managed_user_id == managed.id)
        .order_by(PortalJob.created_at.desc())
        .limit(200)
    ).all()
    for row in rows[:50]:
        _refresh_job(context, managed, row)
    db.commit()
    return {"status": "OK", "jobs": [_job_view(row) for row in rows], "count": len(rows)}


@router.post("/self/jobs")
def submit_self_job(
    body: SelfJobSubmitRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.jobs.submit")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    if body.image_ref is not None and body.image_ref not in APPROVED_IMAGE_REFS:
        raise _error(422, "IMAGE_NOT_APPROVED", "镜像不在管理员批准清单中")
    script_path = _relative_path(body.script_path, "Job Script")
    workdir = _relative_path(body.workdir, "Workdir")
    managed = managed_identity_for_user(db, context.user, lock=True)
    active, terminal = entitlement(db, managed.id, lock=True)
    now = utcnow()
    remaining = int((ensure_utc(terminal.expires_at) - now).total_seconds())
    if body.time_limit_seconds > remaining:
        raise _error(422, "JOB_EXCEEDS_LEASE", "作业时限不能超过租约剩余时间")
    if body.gpu_count > min(1, active.gpu_count):
        raise _error(422, "GPU_LIMIT_EXCEEDED", "当前用户最多申请1张GPU")
    operation_key = f"self-job-submit:{managed.id}:{body.idempotency_key}"
    existing = db.scalar(
        select(PortalOperation).where(
            PortalOperation.requested_by == context.user.id,
            PortalOperation.idempotency_key == operation_key,
        )
    )
    if existing is not None:
        job = db.scalar(select(PortalJob).where(PortalJob.operation_id == existing.id))
        if job is None:
            raise _error(409, "IDEMPOTENCY_CONFLICT", "幂等操作缺少作业记录")
        return {"status": existing.status, "job": _job_view(job)}
    job_id = uuid.uuid4()
    stdout = f"workspace/.portal/jobs/{job_id}.out"
    stderr = f"workspace/.portal/jobs/{job_id}.err"
    payload = {
        "portal_job_id": str(job_id),
        "managed_user_id": str(managed.id),
        "lease_id": str(active.id),
        "username": managed.unix_username,
        "uid": managed.uid,
        "gid": managed.gid,
        "name": body.name,
        "script_relative_path": script_path,
        "workdir_relative_path": workdir,
        "stdout_relative_path": stdout,
        "stderr_relative_path": stderr,
        "cpus": body.cpus,
        "memory_mb": body.memory_mb,
        "gpu_count": body.gpu_count,
        "time_limit_seconds": body.time_limit_seconds,
        "lease_deadline_at": ensure_utc(terminal.expires_at).isoformat(),
        "image_ref": body.image_ref,
    }
    operation = _new_operation(
        db,
        context,
        owner_id=managed.id,
        operation_type="self.job.submit",
        target_type="slurm_job",
        target_id=str(job_id),
        summary="用户通过Portal提交自己的Slurm作业",
        payload=payload,
        idempotency_key=operation_key,
    )
    job = PortalJob(
        id=job_id,
        owner_managed_user_id=managed.id,
        lease_id=active.id,
        operation_id=operation.id,
        name=body.name,
        state="SUBMITTING",
        script_relative_path=script_path,
        workdir_relative_path=workdir,
        stdout_relative_path=stdout,
        stderr_relative_path=stderr,
        requested_cpus=body.cpus,
        memory_mb=body.memory_mb,
        gpu_count=body.gpu_count,
        time_limit_seconds=body.time_limit_seconds,
        image_ref=body.image_ref,
        lease_deadline_at=terminal.expires_at,
    )
    db.add(job)
    db.flush()
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
        metadata={"slurm_job_id": slurm_job_id, "gpu_count": body.gpu_count},
    )
    db.commit()
    return {"status": "SUBMITTED", "job": _job_view(job)}


def _owned_job(
    db: Session, context: AuthContext, job_id: uuid.UUID, *, lock: bool = False
) -> tuple[Any, PortalJob]:
    managed = managed_identity_for_user(db, context.user)
    query = select(PortalJob).where(
        PortalJob.id == job_id,
        PortalJob.owner_managed_user_id == managed.id,
    )
    if lock:
        query = query.with_for_update()
    job = db.scalar(query)
    if job is None:
        raise _error(404, "JOB_NOT_FOUND", "作业不存在")
    return managed, job


@router.get("/self/jobs/{job_id}")
def self_job_detail(
    job_id: uuid.UUID,
    context: AuthContext = Depends(permission_dependency("self.jobs.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    managed, job = _owned_job(db, context, job_id)
    _refresh_job(context, managed, job)
    db.commit()
    return {"status": "OK", "job": _job_view(job)}


@router.get("/self/jobs/{job_id}/logs")
def self_job_logs(
    job_id: uuid.UUID,
    context: AuthContext = Depends(permission_dependency("self.jobs.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    managed, job = _owned_job(db, context, job_id)
    result = _worker(
        "self.job.logs.read",
        payload={
            "portal_job_id": str(job.id),
            "managed_user_id": str(managed.id),
            "username": managed.unix_username,
            "uid": managed.uid,
            "gid": managed.gid,
            "stdout_relative_path": job.stdout_relative_path,
            "stderr_relative_path": job.stderr_relative_path,
        },
        context=context,
        idempotency_key=f"self-job-logs:{job.id}",
    )
    return {"status": "OK", "stdout": result.get("stdout", ""), "stderr": result.get("stderr", "")}


@router.post("/self/jobs/{job_id}/cancel")
def cancel_self_job(
    job_id: uuid.UUID,
    body: SelfContainerActionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("self.jobs.cancel")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    managed, job = _owned_job(db, context, job_id, lock=True)
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
            "slurm_job_id": job.slurm_job_id,
        },
        context=context,
        idempotency_key=key,
    )
    job.state = str(result.get("job_state", "CANCELLED"))
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
    return {"status": "CANCELLED", "job": _job_view(job)}


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
            "root": f"/srv/gpu-platform/users/{managed.unix_username}",
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
        .where(PortalResourceRecycleItem.owner_managed_user_id == managed.id)
        .order_by(PortalResourceRecycleItem.recycled_at.desc())
    ).all()
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
            }
            for row in rows
        ],
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
        return {"status": existing.state, "restore_request_id": str(existing.id)}
    item = db.scalar(
        select(PortalResourceRecycleItem)
        .where(
            PortalResourceRecycleItem.id == item_id,
            PortalResourceRecycleItem.owner_managed_user_id == managed.id,
            PortalResourceRecycleItem.state == "RECYCLE_BIN",
        )
        .with_for_update()
    )
    if item is None:
        raise _error(404, "RECYCLE_ITEM_NOT_FOUND", "回收资源不存在")
    restore = PortalResourceRestoreRequest(
        owner_managed_user_id=managed.id,
        recycle_item_id=item.id,
        state="REQUESTED",
        requested_duration_seconds=body.duration_seconds,
        idempotency_key=str(body.idempotency_key),
    )
    db.add(restore)
    item.state = "RESTORE_PENDING"
    managed.compute_environment_state = "RESTORE_PENDING"
    _audit(
        db,
        request,
        context,
        event_type="RESOURCE_RESTORE_REQUESTED",
        object_type="resource_restore_request",
        object_id=str(restore.id),
        metadata={"duration_seconds": body.duration_seconds},
    )
    db.commit()
    return {"status": "REQUESTED", "restore_request_id": str(restore.id)}


@router.get("/admin/lease-renewals")
def admin_renewals(
    context: AuthContext = Depends(permission_dependency("users.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = db.scalars(
        select(PortalLeaseRenewalRequest).order_by(PortalLeaseRenewalRequest.requested_at.desc())
    ).all()
    return {
        "status": "OK",
        "requests": [
            {
                "id": str(row.id),
                "owner_managed_user_id": str(row.owner_managed_user_id),
                "lease_id": str(row.lease_id),
                "state": row.state,
                "duration_seconds": row.requested_duration_seconds,
                "requested_at": ensure_utc(row.requested_at).isoformat(),
            }
            for row in rows
        ],
    }


@router.post("/admin/lease-renewals/{request_id}/decision")
def admin_decide_renewal(
    request_id: uuid.UUID,
    body: LeaseDecisionRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("users.write")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
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
    context: AuthContext = Depends(permission_dependency("users.read")),
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
    context: AuthContext = Depends(permission_dependency("users.write")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
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
    payload = {
        "restore_request_id": str(restore.id),
        "managed_user_id": str(managed.id),
        "username": managed.unix_username,
        "uid": managed.uid,
        "gid": managed.gid,
        "container_name": container.name,
        "expected_gpu": "NONE",
        "host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_key_fingerprints": expected_key_fingerprints,
    }
    operation = _new_operation(
        db,
        context,
        owner_id=managed.id,
        operation_type="resource.restore",
        target_type="resource_recycle_item",
        target_id=str(item.id),
        summary="管理员批准恢复用户的可回收计算环境",
        payload=payload,
        idempotency_key=f"resource-restore:{restore.id}",
        risk_level=RiskLevel.HIGH,
    )
    restore.state = "RESTORING"
    item.state = "RESTORING"
    try:
        result = _worker(
            "resource.restore",
            payload=payload,
            context=context,
            idempotency_key=f"resource-restore:{restore.id}",
            timeout_seconds=180,
        )
    except HTTPException as exc:
        operation.status = OperationStatus.FAILED
        operation.finished_at = utcnow()
        detail = getattr(exc, "detail", {})
        operation.error_code = str(
            detail.get("code", "RESTORE_WORKER_FAILED")
            if isinstance(detail, dict)
            else "RESTORE_WORKER_FAILED"
        )[:64]
        restore.state = "FAILED"
        item.state = "FAILED"
        managed.compute_environment_state = "FAILED"
        db.commit()
        raise
    observed_fingerprints = result.get("container_key_fingerprints")
    if (
        result.get("container_state") != "RUNNING"
        or result.get("container_gpu") != "NONE"
        or result.get("container_key_state") != "INSTALLED"
        or not isinstance(observed_fingerprints, list)
        or sorted(str(item) for item in observed_fingerprints) != expected_key_fingerprints
    ):
        operation.status = OperationStatus.FAILED
        operation.error_code = "RESTORE_POSTCONDITION_FAILED"
        operation.finished_at = utcnow()
        restore.state = "FAILED"
        item.state = "FAILED"
        managed.compute_environment_state = "FAILED"
        db.commit()
        raise _error(409, "RESTORE_POSTCONDITION_FAILED", "恢复安全后置条件失败")
    lease = create_lease(
        managed_user_id=managed.id,
        starts_at=utcnow(),
        duration_seconds=restore.requested_duration_seconds,
        gpu_count=1,
        approved_by=context.user.id,
        restored=True,
    )
    db.add(lease)
    db.flush()
    restore.state = "RESTORED"
    restore.restored_lease_id = lease.id
    item.state = "RESTORED"
    item.restored_at = utcnow()
    managed.compute_environment_state = "ACTIVE"
    container.observed_state = "RUNNING"
    container.desired_state = "RUNNING"
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
    return {"status": "RESTORED", "lease_id": str(lease.id)}
