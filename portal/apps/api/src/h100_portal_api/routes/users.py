from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from h100_portal_api.auth import AuthContext, serialize_user
from h100_portal_api.database import get_db
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.models import PortalManagedUser, PortalUser

router = APIRouter(prefix="/users", tags=["users"])


def resource_view(user: PortalUser, resource: PortalManagedUser | None) -> dict[str, Any]:
    if resource is None:
        return {
            "unix_username": user.unix_username,
            "onboarding_state": user.resource_onboarding_state,
            "gpu_isolation_state": "NOT_APPLIED",
        }
    return {
        "unix_username": resource.unix_username,
        "uid": resource.uid,
        "gid": resource.gid,
        "shell": resource.shell,
        "host_access_state": resource.host_access_state,
        "gpu_isolation_state": resource.gpu_isolation_state,
        "onboarding_state": resource.onboarding_state,
        "slurm_account": resource.slurm_account,
        "slurm_qos": resource.slurm_qos,
        "project_id": resource.project_id,
        "quota_bytes": resource.quota_bytes,
        "container_name": resource.container_name,
        "container_port": resource.container_port,
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
    return {"status": "OK", "user": item}
