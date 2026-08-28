from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from h100_portal_contracts.workspace import (
    CPU_DEVELOPMENT_PROFILE,
    GPU_DEVELOPMENT_PROFILE,
    workspace_path,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from h100_portal_api.lease_service import entitlement, managed_identity_for_user
from h100_portal_api.models import (
    PortalComputeLease,
    PortalContainer,
    PortalManagedUser,
    PortalStorageResource,
    PortalUser,
)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


@dataclass(frozen=True)
class SelfResourceContext:
    """Server-derived coordinates for one ordinary user's active compute environment."""

    portal_user_id: uuid.UUID
    owner_login: str
    managed: PortalManagedUser
    container: PortalContainer
    active_lease: PortalComputeLease
    terminal_lease: PortalComputeLease
    storage: PortalStorageResource

    @property
    def username(self) -> str:
        return self.managed.unix_username

    @property
    def max_gpu(self) -> int:
        return min(1, self.active_lease.gpu_count)

    @property
    def workspace(self) -> str:
        return str(workspace_path(self.managed.uid))

    def worker_identity(self) -> dict[str, str | int]:
        return {
            "managed_user_id": str(self.managed.id),
            "username": self.managed.unix_username,
            "uid": self.managed.uid,
            "gid": self.managed.gid,
        }


def resolve_self_compute_context(
    db: Session,
    user: PortalUser,
    *,
    lock: bool = False,
    require_running_container: bool = False,
) -> SelfResourceContext:
    """Resolve and validate all self-service targets from the authenticated user.

    Browser-provided usernames, UIDs, container names, Slurm coordinates, and
    resource IDs never participate in this lookup.
    """

    managed = managed_identity_for_user(db, user, lock=lock)
    active_lease, terminal_lease = entitlement(db, managed.id, lock=lock)
    container_query = select(PortalContainer).where(
        PortalContainer.owner_managed_user_id == managed.id
    )
    storage_query = select(PortalStorageResource).where(
        PortalStorageResource.owner_managed_user_id == managed.id
    )
    if lock:
        container_query = container_query.with_for_update()
        storage_query = storage_query.with_for_update()
    container = db.scalar(container_query)
    storage = db.scalar(storage_query)
    if container is None:
        raise _error(404, "MANAGED_CONTAINER_NOT_FOUND", "开发容器不存在")
    if storage is None:
        raise _error(
            409,
            "SELF_COMPUTE_CONTEXT_INVALID",
            "无法确认当前计算环境，请联系管理员。",
        )

    expected_container = f"gpu-dev-{managed.unix_username}"
    binding_valid = (
        managed.portal_user_id == user.id
        and managed.compute_environment_state == "ACTIVE"
        and managed.host_access_state == "DISABLED_BY_PLATFORM_POLICY"
        and managed.shell == "/usr/sbin/nologin"
        and managed.slurm_account is not None
        and managed.slurm_qos is not None
        and container.managed_user_id == managed.id
        and container.owner_managed_user_id == managed.id
        and container.name == managed.container_name == expected_container
        and active_lease.managed_user_id == managed.id
        and active_lease.owner_managed_user_id == managed.id
        and terminal_lease.owner_managed_user_id == managed.id
        and storage.owner_managed_user_id == managed.id
        and storage.root_path == str(workspace_path(managed.uid))
        and container.development_profile in {CPU_DEVELOPMENT_PROFILE, GPU_DEVELOPMENT_PROFILE}
        and container.gpu_count
        == (1 if container.development_profile == GPU_DEVELOPMENT_PROFILE else 0)
        and ((container.gpu_allocation_job_id is None) == (container.gpu_allocation_uuid is None))
        and (
            container.development_profile == GPU_DEVELOPMENT_PROFILE
            or (container.gpu_allocation_job_id is None and container.gpu_allocation_uuid is None)
        )
    )
    if not binding_valid:
        raise _error(
            409,
            "SELF_COMPUTE_CONTEXT_INVALID",
            "无法确认当前计算环境，请联系管理员。",
        )
    if require_running_container and container.observed_state != "RUNNING":
        raise _error(409, "CONTAINER_STATE_REJECTED", "开发容器当前未运行")

    return SelfResourceContext(
        portal_user_id=user.id,
        owner_login=user.normalized_login,
        managed=managed,
        container=container,
        active_lease=active_lease,
        terminal_lease=terminal_lease,
        storage=storage,
    )
