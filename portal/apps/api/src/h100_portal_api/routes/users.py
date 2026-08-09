from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from h100_portal_api.auth import AuthContext, serialize_user
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import auth_context, permission_dependency
from h100_portal_api.enums import OnboardingState, OperationStatus
from h100_portal_api.models import PortalContainer, PortalManagedUser, PortalOperation, PortalUser
from h100_portal_api.rbac import has_permission

router = APIRouter(prefix="/users", tags=["users"])


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
        item = serialize_user(user).model_dump(mode="json")
        resource = managed.get(user.id)
        item["linux_identity"] = resource_view(
            user, resource, containers.get(resource.id) if resource is not None else None
        )
        item["compute_onboarding"] = compute_plan_view(user, db)
        result.append(item)
    return {"status": "OK", "users": result, "count": len(result)}


@router.get("/{user_id}")
def user_detail(
    user_id: str,
    context: AuthContext = Depends(auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    import uuid

    try:
        target = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"}
        ) from exc
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
    resource = db.scalar(
        select(PortalManagedUser).where(PortalManagedUser.portal_user_id == user.id)
    )
    container = (
        db.scalar(select(PortalContainer).where(PortalContainer.managed_user_id == resource.id))
        if resource is not None
        else None
    )
    item = serialize_user(user).model_dump(mode="json")
    item["linux_identity"] = resource_view(user, resource, container)
    item["compute_onboarding"] = compute_plan_view(user, db)
    return {"status": "OK", "user": item}
