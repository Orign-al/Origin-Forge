from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from h100_portal_api.auth import AuthContext
from h100_portal_api.dependencies import permission_dependency
from h100_portal_api.routes.platform import adapter
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
