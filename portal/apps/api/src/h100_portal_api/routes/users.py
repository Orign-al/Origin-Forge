import pwd
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from h100_portal_api.audit import record_audit
from h100_portal_api.auth import (
    AuthContext,
    client_ip,
    require_session_csrf,
    serialize_user,
    user_agent,
)
from h100_portal_api.config import get_settings
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import auth_context, permission_dependency
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordActionPurpose,
    PasswordActionTokenState,
    PasswordState,
    RiskLevel,
)
from h100_portal_api.models import (
    PortalComputeLease,
    PortalComputeResourceRequest,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalOperationEvent,
    PortalPasswordCredential,
    PortalPasswordSetupToken,
    PortalRole,
    PortalUser,
    ensure_utc,
    utcnow,
)
from h100_portal_api.rbac import assignable_roles, has_permission, highest_role
from h100_portal_api.schemas import PortalUserCreateRequest
from h100_portal_api.security import digest_secret, normalize_login, random_token

router = APIRouter(prefix="/users", tags=["users"])


def _user_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"}
        ) from exc


def _password_action_state(token: PortalPasswordSetupToken) -> str:
    state = token.state.value if hasattr(token.state, "value") else str(token.state)
    if state == PasswordActionTokenState.ACTIVE and ensure_utc(token.expires_at) <= utcnow():
        return PasswordActionTokenState.EXPIRED
    return state


def _trusted_request_origin(request: Request) -> str:
    origin = request.headers.get("origin", "").rstrip("/")
    allowed_origins = {item.rstrip("/") for item in get_settings().allowed_origins}
    if origin in allowed_origins:
        return origin
    referer = request.headers.get("referer", "")
    for allowed in allowed_origins:
        if referer == allowed or referer.startswith(f"{allowed}/"):
            return allowed
    raise HTTPException(
        status_code=400,
        detail={"code": "PORTAL_ORIGIN_UNAVAILABLE", "message": "无法生成可信 Portal 链接"},
    )


def password_action_view(token: PortalPasswordSetupToken) -> dict[str, Any]:
    return {
        "id": str(token.id),
        "purpose": token.purpose.value if hasattr(token.purpose, "value") else str(token.purpose),
        "state": _password_action_state(token),
        "created_at": token.created_at,
        "expires_at": token.expires_at,
        "used_at": token.used_at,
        "revoked_at": token.revoked_at,
        "created_by": str(token.created_by) if token.created_by else None,
    }


def password_action_views(db: Session, user_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(PortalPasswordSetupToken)
        .where(PortalPasswordSetupToken.user_id == user_id)
        .order_by(PortalPasswordSetupToken.created_at.desc())
        .limit(20)
    ).all()
    return [password_action_view(row) for row in rows]


def _identity_operation(
    db: Session,
    *,
    actor: PortalUser,
    operation_type: str,
    target: PortalUser,
    summary: str,
    payload: dict[str, Any],
) -> PortalOperation:
    now = utcnow()
    operation = PortalOperation(
        operation_type=operation_type,
        target_type="portal_user",
        target_id=str(target.id),
        requested_by=actor.id,
        approved_by=actor.id,
        request_summary=summary,
        validated_payload=payload,
        idempotency_key=f"{operation_type}:{uuid.uuid4()}",
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.SUCCEEDED,
        created_at=now,
        approved_at=now,
        started_at=now,
        finished_at=now,
        result_summary="Portal identity action completed; compute resources unchanged",
    )
    db.add(operation)
    db.flush()
    db.add(
        PortalOperationEvent(
            operation_id=operation.id,
            from_status=None,
            to_status=OperationStatus.SUCCEEDED,
            safe_message="Fixed Portal identity operation completed",
            created_at=now,
        )
    )
    return operation


def _admin_user_payload(user: PortalUser, db: Session) -> dict[str, Any]:
    item = serialize_user(user).model_dump(mode="json")
    item["note"] = user.note
    item["password_actions"] = password_action_views(db, user.id)
    compute_request = db.scalar(
        select(PortalComputeResourceRequest)
        .where(PortalComputeResourceRequest.portal_account_id == user.id)
        .order_by(PortalComputeResourceRequest.created_at.desc())
    )
    item["compute_request"] = (
        {
            "id": str(compute_request.id),
            "status": compute_request.status,
            "gpu_max": compute_request.requested_gpu_max,
            "submitted_at": compute_request.submitted_at,
        }
        if compute_request is not None
        else None
    )
    managed = db.scalar(
        select(PortalManagedUser).where(PortalManagedUser.portal_user_id == user.id)
    )
    lease = (
        db.scalar(
            select(PortalComputeLease)
            .where(PortalComputeLease.owner_managed_user_id == managed.id)
            .order_by(PortalComputeLease.expires_at.desc())
        )
        if managed is not None
        else None
    )
    now = utcnow()
    item["compute_lifecycle"] = (
        {
            "has_lease": True,
            "lease_id": str(lease.id),
            "owner": managed.unix_username,
            "starts_at": ensure_utc(lease.starts_at).isoformat(),
            "expires_at": ensure_utc(lease.expires_at).isoformat(),
            "time_expired": ensure_utc(lease.expires_at) <= now,
            "lease_state": lease.state,
        }
        if managed is not None and lease is not None
        else {"has_lease": False}
    )
    return item


def resource_view(
    user: PortalUser,
    resource: PortalManagedUser | None,
    container: PortalContainer | None = None,
) -> dict[str, Any]:
    if resource is None:
        return {
            "unix_username": user.unix_username,
            "onboarding_state": user.resource_onboarding_state,
            "gpu_isolation_state": "NOT_APPLIED",
            "ssh_key_state": "NOT_REQUIRED_FOR_STAGE",
            "ssh_key_count": 0,
            "host_access_state": "NOT_ACTIVATED",
        }
    safe_spec = container.safe_spec if container is not None else {}
    return {
        "managed_user_id": str(resource.id),
        "unix_username": resource.unix_username,
        "uid": resource.uid,
        "gid": resource.gid,
        "shell": resource.shell,
        "host_access_state": resource.host_access_state,
        "compute_environment_state": resource.compute_environment_state,
        "gpu_isolation_state": resource.gpu_isolation_state,
        "onboarding_state": resource.onboarding_state,
        "ssh_key_state": resource.ssh_key_state,
        "ssh_key_count": resource.ssh_key_count,
        "slurm_account": resource.slurm_account,
        "slurm_qos": resource.slurm_qos,
        "project_id": resource.project_id,
        "quota_bytes": resource.quota_bytes,
        "container_name": resource.container_name,
        "container_port": resource.container_port,
        "password_state": "LOCKED"
        if resource.onboarding_state in {OnboardingState.STAGED, OnboardingState.ACTIVE}
        else "UNKNOWN",
        "authorized_keys_state": safe_spec.get("authorized_keys", "UNKNOWN"),
        "max_gpus": safe_spec.get("max_gpus"),
        "gpu_open_state": safe_spec.get("gpu_open_probe"),
        "cuda_context_state": safe_spec.get("cuda_context_probe"),
        "guard_state": safe_spec.get("guard"),
        "container_state": container.observed_state if container is not None else None,
        "container_gpu": safe_spec.get("gpu"),
        "container_cpus": safe_spec.get("cpus"),
        "container_memory_gb": safe_spec.get("memory_gb"),
        "container_pids_limit": safe_spec.get("pids_limit"),
        "container_image_digest": container.image_digest if container is not None else None,
        "approved_host": safe_spec.get("approved_host"),
        "host_ssh_port": safe_spec.get("host_ssh_port"),
        "host_server_fingerprint": safe_spec.get("host_server_fingerprint"),
        "container_server_fingerprint": safe_spec.get("container_server_fingerprint"),
        "host_ssh_server": safe_spec.get("host_ssh_server"),
        "container_ssh_server": safe_spec.get("container_ssh_server"),
        "host_ssh_client_validation": safe_spec.get("host_ssh_client_validation"),
        "container_ssh_client_validation": safe_spec.get("container_ssh_client_validation"),
        "host_ssh_policy": safe_spec.get("host_ssh_policy"),
        "container_ssh_policy": safe_spec.get("container_ssh_policy"),
        "slurm_node_state": safe_spec.get("slurm_node_state"),
        "slurm_queue": safe_spec.get("slurm_queue"),
        "gpu_scheduling_available": safe_spec.get("gpu_scheduling_available"),
        "pilot_acceptance_status": safe_spec.get("pilot_acceptance_status"),
        "pilot_acceptance_operation_id": safe_spec.get("pilot_acceptance_operation_id"),
        "pilot_acceptance_accepted_at": safe_spec.get("pilot_acceptance_accepted_at"),
        "pilot_cpu_job_id": safe_spec.get("pilot_cpu_job_id"),
        "pilot_gpu_job_id": safe_spec.get("pilot_gpu_job_id"),
        "pilot_allocated_gpu_uuid": safe_spec.get("pilot_allocated_gpu_uuid"),
        "pilot_final_node_state": safe_spec.get("pilot_final_node_state"),
    }


def compute_plan_view(user: PortalUser, db: Session) -> dict[str, Any]:
    """Expose the latest origin-pilot plan without treating it as a resource."""
    managed = db.scalar(
        select(PortalManagedUser).where(PortalManagedUser.portal_user_id == user.id)
    )
    if user.normalized_login != "origin-al" and managed is None:
        return {
            "status": "NOT_APPLICABLE",
            "compute_username": None,
            "draft_state": "NOT_APPLICABLE",
            "ssh_key_status": "NOT_APPLICABLE",
            "plan": None,
        }
    compute_username = managed.unix_username if managed is not None else "origin-pilot"
    operation_query = (
        select(PortalOperation)
        .where(
            PortalOperation.operation_type.in_({"user.plan", "user.stage", "user.activate"}),
            PortalOperation.target_id == compute_username,
        )
        .order_by(PortalOperation.created_at.desc())
    )
    if managed is not None and managed.onboarding_state == OnboardingState.STAGED:
        operation_query = (
            select(PortalOperation)
            .where(
                PortalOperation.operation_type == "user.stage",
                PortalOperation.target_id == compute_username,
                PortalOperation.status == OperationStatus.SUCCEEDED,
            )
            .order_by(PortalOperation.finished_at.desc())
        )
    operation = db.scalar(operation_query)
    activate_operation = (
        db.scalar(
            select(PortalOperation)
            .where(
                PortalOperation.operation_type == "user.activate",
                PortalOperation.target_id == compute_username,
            )
            .order_by(PortalOperation.created_at.desc())
        )
        if managed is not None
        and managed.onboarding_state in {OnboardingState.STAGED, OnboardingState.ACTIVE}
        else None
    )
    if operation is None:
        if managed is not None:
            state = (
                managed.onboarding_state.value
                if hasattr(managed.onboarding_state, "value")
                else str(managed.onboarding_state)
            )
            return {
                "status": state,
                "compute_username": compute_username,
                "draft_state": state,
                "ssh_key_status": managed.ssh_key_state,
                "plan": None,
                "activate_dry_run": None,
            }
        return {
            "status": "NOT_CREATED",
            "compute_username": compute_username,
            "draft_state": "DRAFT NOT CREATED",
            "ssh_key_status": "NOT_REQUIRED_FOR_STAGE",
            "plan": None,
        }
    managed_state = (
        managed.onboarding_state.value
        if managed is not None and hasattr(managed.onboarding_state, "value")
        else (str(managed.onboarding_state) if managed is not None else None)
    )
    return {
        "status": managed_state
        if managed_state in {"STAGED", "ACTIVE"}
        else ("DRAFT" if operation.operation_type != "user.activate" else "ACTIVATE_DRAFT"),
        "compute_username": compute_username,
        "draft_state": managed_state if managed_state in {"STAGED", "ACTIVE"} else "DRAFT",
        "operation_id": str(operation.id),
        "operation_status": operation.status.value
        if hasattr(operation.status, "value")
        else str(operation.status),
        "plan": operation.dry_run_result,
        "operation_type": operation.operation_type,
        "ssh_key_status": managed.ssh_key_state
        if managed_state in {"STAGED", "ACTIVE"} and managed is not None
        else (
            "NOT_REQUIRED_FOR_STAGE"
            if operation.operation_type in {"user.plan", "user.stage"}
            else "REQUIRED_FOR_ACTIVATION"
        ),
        "result_summary": operation.result_summary,
        "error_code": operation.error_code,
        "activate_dry_run": (
            {
                "operation_id": str(activate_operation.id),
                "status": activate_operation.status.value
                if hasattr(activate_operation.status, "value")
                else str(activate_operation.status),
                "plan": activate_operation.dry_run_result,
                "result_summary": activate_operation.result_summary,
                "error_code": activate_operation.error_code,
            }
            if activate_operation is not None
            else None
        ),
    }


@router.get("")
def users(
    context: AuthContext = Depends(permission_dependency("users.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    portal_users = db.scalars(select(PortalUser).order_by(PortalUser.created_at)).all()
    managed = {item.portal_user_id: item for item in db.scalars(select(PortalManagedUser)).all()}
    containers = {
        item.managed_user_id: item
        for item in db.scalars(select(PortalContainer)).all()
        if item.managed_user_id is not None
    }
    result = []
    for user in portal_users:
        item = _admin_user_payload(user, db)
        resource = managed.get(user.id)
        item["linux_identity"] = resource_view(
            user, resource, containers.get(resource.id) if resource is not None else None
        )
        item["compute_onboarding"] = compute_plan_view(user, db)
        result.append(item)
    return {"status": "OK", "users": result, "count": len(result)}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(
    body: PortalUserCreateRequest,
    request: Request,
    context: AuthContext = Depends(permission_dependency("users.write")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_session_csrf(request, context)
    try:
        normalized = normalize_login(body.login_name)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "LOGIN_NAME_INVALID",
                "message": "登录名必须以小写字母开头，仅包含小写字母、数字或连字符",
            },
        ) from exc
    if body.role not in set(assignable_roles(context.user)):
        raise HTTPException(
            status_code=403,
            detail={"code": "ROLE_NOT_ASSIGNABLE", "message": "当前账号无权分配该角色"},
        )
    if db.scalar(select(PortalUser.id).where(PortalUser.normalized_login == normalized)):
        raise HTTPException(
            status_code=409,
            detail={"code": "PORTAL_LOGIN_CONFLICT", "message": "该 Portal 登录名已存在"},
        )
    if db.scalar(select(PortalManagedUser.id).where(PortalManagedUser.unix_username == normalized)):
        raise HTTPException(
            status_code=409,
            detail={"code": "COMPUTE_IDENTITY_CONFLICT", "message": "该名称已被计算身份使用"},
        )
    try:
        pwd.getpwnam(normalized)
    except KeyError:
        pass
    else:
        raise HTTPException(
            status_code=409,
            detail={"code": "LINUX_USERNAME_CONFLICT", "message": "该名称已被 Linux 账号使用"},
        )
    role = db.scalar(select(PortalRole).where(PortalRole.name == body.role))
    if role is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "ROLE_INVALID", "message": "角色不存在"},
        )
    user = PortalUser(
        login_name=normalized,
        normalized_login=normalized,
        display_name=body.display_name,
        note=body.note,
        unix_username=None,
        account_state=AccountState.INVITED,
        password_state=PasswordState.SETUP_REQUIRED,
        resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        roles=[role],
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "PORTAL_LOGIN_CONFLICT", "message": "该 Portal 登录名已存在"},
        ) from exc
    operation = _identity_operation(
        db,
        actor=context.user,
        operation_type="portal_user.create",
        target=user,
        summary=f"Create invited Portal account {normalized}",
        payload={
            "account_id": str(user.id),
            "normalized_login": normalized,
            "role": body.role,
            "account_state": AccountState.INVITED,
            "compute_identity": "NOT_PROVISIONED",
        },
    )
    record_audit(
        db,
        event_type="PORTAL_USER_CREATED",
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="portal_user",
        object_id=str(user.id),
        operation_id=operation.id,
        metadata={
            "normalized_login": normalized,
            "role": body.role,
            "account_state": AccountState.INVITED,
            "compute_identity": "NOT_PROVISIONED",
        },
    )
    db.commit()
    return {
        "status": "CREATED",
        "user": _admin_user_payload(user, db),
        "operation_id": str(operation.id),
        "compute_resources_created": False,
    }


@router.get("/{user_id}")
def user_detail(
    user_id: str,
    context: AuthContext = Depends(auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    target = _user_id(user_id)
    user = db.get(PortalUser, target)
    if user is None:
        raise HTTPException(
            status_code=404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"}
        )
    if user.id != context.user.id and not has_permission(context.user, "users.read"):
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "当前账号无权查看该用户"},
        )
    if user.id == context.user.id and highest_role(context.user) == "user":
        return {"status": "OK", "user": serialize_user(user).model_dump(mode="json")}
    resource = db.scalar(
        select(PortalManagedUser).where(PortalManagedUser.portal_user_id == user.id)
    )
    container = (
        db.scalar(select(PortalContainer).where(PortalContainer.managed_user_id == resource.id))
        if resource is not None
        else None
    )
    item = _admin_user_payload(user, db)
    item["linux_identity"] = resource_view(user, resource, container)
    item["compute_onboarding"] = compute_plan_view(user, db)
    return {"status": "OK", "user": item}


@router.get("/{user_id}/password-action-tokens")
def password_action_tokens(
    user_id: str,
    context: AuthContext = Depends(permission_dependency("users.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    target = db.get(PortalUser, _user_id(user_id))
    if target is None:
        raise HTTPException(
            status_code=404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"}
        )
    rows = password_action_views(db, target.id)
    return {"status": "OK", "tokens": rows, "count": len(rows)}


def _create_password_action_link(
    *,
    purpose: PasswordActionPurpose,
    user_id: str,
    request: Request,
    context: AuthContext,
    db: Session,
) -> dict[str, Any]:
    require_session_csrf(request, context)
    origin = _trusted_request_origin(request)
    target = db.scalar(
        select(PortalUser).where(PortalUser.id == _user_id(user_id)).with_for_update()
    )
    if target is None:
        raise HTTPException(
            status_code=404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"}
        )
    credential = db.scalar(
        select(PortalPasswordCredential).where(PortalPasswordCredential.user_id == target.id)
    )
    if purpose == PasswordActionPurpose.INITIAL_PASSWORD_SETUP:
        if (
            target.account_state != AccountState.INVITED
            or target.password_state != PasswordState.SETUP_REQUIRED
            or credential is not None
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "INITIAL_SETUP_NOT_ALLOWED",
                    "message": "只有尚未设置密码的邀请账号可以生成设置链接",
                },
            )
        lifetime = timedelta(hours=get_settings().initial_password_setup_token_hours)
        operation_type = "portal_user.password_setup_link.create"
        created_event = "PASSWORD_SETUP_LINK_CREATED"
        revoked_event = "PASSWORD_SETUP_LINK_REVOKED"
    else:
        if (
            target.account_state != AccountState.ACTIVE
            or target.password_state not in {PasswordState.SET, PasswordState.RESET_REQUIRED}
            or credential is None
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PASSWORD_RESET_NOT_ALLOWED",
                    "message": "当前账号状态不能生成密码重置链接",
                },
            )
        lifetime = timedelta(minutes=get_settings().password_reset_token_minutes)
        operation_type = "portal_user.password_reset_link.create"
        created_event = "PASSWORD_RESET_LINK_CREATED"
        revoked_event = "PASSWORD_RESET_LINK_REVOKED"
    now = utcnow()
    active_rows = db.scalars(
        select(PortalPasswordSetupToken)
        .where(
            PortalPasswordSetupToken.user_id == target.id,
            PortalPasswordSetupToken.purpose == purpose,
            PortalPasswordSetupToken.state == PasswordActionTokenState.ACTIVE,
            PortalPasswordSetupToken.used_at.is_(None),
        )
        .with_for_update()
    ).all()
    if active_rows:
        active_ids = [row.id for row in active_rows]
        db.execute(
            update(PortalPasswordSetupToken)
            .where(PortalPasswordSetupToken.id.in_(active_ids))
            .values(
                state=PasswordActionTokenState.REVOKED,
                revoked_at=now,
                challenge_hash=None,
                challenge_expires_at=None,
            )
        )
        record_audit(
            db,
            event_type=revoked_event,
            actor=context.user.normalized_login,
            actor_role=highest_role(context.user),
            source_ip=client_ip(request),
            user_agent=user_agent(request),
            object_type="portal_user",
            object_id=str(target.id),
            metadata={"purpose": purpose, "revoked_count": len(active_rows)},
        )
    raw_token = random_token(48)
    expires_at = now + lifetime
    token = PortalPasswordSetupToken(
        user_id=target.id,
        token_hash=digest_secret(raw_token),
        purpose=purpose,
        state=PasswordActionTokenState.ACTIVE,
        created_at=now,
        expires_at=expires_at,
        created_by=context.user.id,
        request_ip_digest=digest_secret(f"{get_settings().secret_key}:{client_ip(request)}"),
    )
    db.add(token)
    db.flush()
    operation = _identity_operation(
        db,
        actor=context.user,
        operation_type=operation_type,
        target=target,
        summary=f"Create one-time {purpose.value} link for {target.normalized_login}",
        payload={
            "account_id": str(target.id),
            "purpose": purpose,
            "expires_at": expires_at.isoformat(),
            "one_time": True,
            "revoked_previous_count": len(active_rows),
        },
    )
    record_audit(
        db,
        event_type=created_event,
        actor=context.user.normalized_login,
        actor_role=highest_role(context.user),
        source_ip=client_ip(request),
        user_agent=user_agent(request),
        object_type="password_action_token",
        object_id=str(token.id),
        operation_id=operation.id,
        metadata={
            "account_id": str(target.id),
            "purpose": purpose,
            "expires_at": expires_at.isoformat(),
            "one_time": True,
        },
    )
    db.commit()
    action_url = f"{origin}/setup-password#token={raw_token}"
    return {
        "status": "GENERATED",
        "purpose": purpose,
        "setup_url": action_url,
        "expires_at": expires_at,
        "token": password_action_view(token),
        "operation_id": str(operation.id),
    }


@router.post("/{user_id}/password-setup-links", status_code=status.HTTP_201_CREATED)
def create_password_setup_link(
    user_id: str,
    request: Request,
    context: AuthContext = Depends(permission_dependency("users.write")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _create_password_action_link(
        purpose=PasswordActionPurpose.INITIAL_PASSWORD_SETUP,
        user_id=user_id,
        request=request,
        context=context,
        db=db,
    )


@router.post("/{user_id}/password-reset-links", status_code=status.HTTP_201_CREATED)
def create_password_reset_link(
    user_id: str,
    request: Request,
    context: AuthContext = Depends(permission_dependency("users.write")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _create_password_action_link(
        purpose=PasswordActionPurpose.PASSWORD_RESET,
        user_id=user_id,
        request=request,
        context=context,
        db=db,
    )
