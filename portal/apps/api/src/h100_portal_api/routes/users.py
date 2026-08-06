from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from h100_portal_api.auth import AuthContext, serialize_user
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.models import PortalManagedUser, PortalOperation, PortalUser

router = APIRouter(prefix="/users", tags=["users"])


def resource_view(user: PortalUser, resource: PortalManagedUser | None) -> dict[str, Any]:
    if resource is None:
        return {
            "unix_username": user.unix_username,
            "onboarding_state": user.resource_onboarding_state,
            "gpu_isolation_state": "NOT_APPLIED",
            "ssh_key_state": "NOT_REQUIRED_FOR_STAGE",
            "ssh_key_count": 0,
            "host_access_state": "NOT_ACTIVATED",
        }
    return {
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
    }


def compute_plan_view(user: PortalUser, db: Session) -> dict[str, Any]:
    """Expose the latest origin-pilot plan without treating it as a resource."""
    if user.normalized_login != "origin-al":
        return {
            "status": "NOT_APPLICABLE",
            "compute_username": None,
            "draft_state": "NOT_APPLICABLE",
            "ssh_key_status": "NOT_APPLICABLE",
            "plan": None,
        }
    operation = db.scalar(
        select(PortalOperation)
        .where(
            PortalOperation.requested_by == user.id,
            PortalOperation.operation_type.in_({"user.plan", "user.stage", "user.activate"}),
            PortalOperation.target_id == "origin-pilot",
        )
        .order_by(PortalOperation.created_at.desc())
    )
    if operation is None:
        return {
            "status": "NOT_CREATED",
            "compute_username": "origin-pilot",
            "draft_state": "DRAFT NOT CREATED",
            "ssh_key_status": "NOT_REQUIRED_FOR_STAGE",
            "plan": None,
        }
    return {
        "status": "DRAFT" if operation.operation_type != "user.activate" else "ACTIVATE_DRAFT",
        "compute_username": "origin-pilot",
        "draft_state": "DRAFT",
        "operation_id": str(operation.id),
        "operation_status": operation.status.value
        if hasattr(operation.status, "value")
        else str(operation.status),
        "plan": operation.dry_run_result,
        "operation_type": operation.operation_type,
        "ssh_key_status": (
            "NOT_REQUIRED_FOR_STAGE"
            if operation.operation_type in {"user.plan", "user.stage"}
            else "REQUIRED_FOR_ACTIVATION"
        ),
        "result_summary": operation.result_summary,
        "error_code": operation.error_code,
    }


@router.get("")
def users(
    context: AuthContext = Depends(permission_dependency("users.read")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    portal_users = db.scalars(select(PortalUser).order_by(PortalUser.created_at)).all()
    managed = {item.portal_user_id: item for item in db.scalars(select(PortalManagedUser)).all()}
    result = []
    for user in portal_users:
        item = serialize_user(user).model_dump(mode="json")
        item["linux_identity"] = resource_view(user, managed.get(user.id))
        item["compute_onboarding"] = compute_plan_view(user, db)
        result.append(item)
    return {"status": "OK", "users": result, "count": len(result)}


@router.get("/{user_id}")
def user_detail(
    user_id: str,
    context: AuthContext = Depends(permission_dependency("users.read")),
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
    resource = db.scalar(
        select(PortalManagedUser).where(PortalManagedUser.portal_user_id == user.id)
    )
    item = serialize_user(user).model_dump(mode="json")
    item["linux_identity"] = resource_view(user, resource)
    item["compute_onboarding"] = compute_plan_view(user, db)
    return {"status": "OK", "user": item}
