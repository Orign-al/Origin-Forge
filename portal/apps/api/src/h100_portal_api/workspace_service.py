from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from h100_portal_contracts.workspace import (
    WORKSPACE_MOUNT_CONTRACT_VERSION,
    WORKSPACE_QUOTA_BYTES,
    WorkspaceBinding,
    workspace_binding,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from h100_portal_api.lease_service import managed_identity_for_user
from h100_portal_api.models import PortalManagedUser, PortalStorageResource, PortalUser


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


@dataclass(frozen=True, slots=True)
class WorkspaceResolution:
    """Database and identity coordinates for one owner-bound workspace."""

    workspace_id: uuid.UUID
    managed: PortalManagedUser
    storage: PortalStorageResource
    binding: WorkspaceBinding

    @property
    def quota_reference(self) -> str:
        return f"xfs-project:{self.managed.project_id}"

    def worker_payload(self) -> dict[str, str | int]:
        project_id = self.managed.project_id
        if project_id is None:
            raise _error(
                409,
                "WORKSPACE_METADATA_REJECTED",
                "Workspace quota project metadata is unavailable.",
            )
        return {
            "managed_user_id": str(self.managed.id),
            "username": self.managed.unix_username,
            "uid": self.managed.uid,
            "gid": self.managed.gid,
            "workspace_path": str(self.binding.canonical_workspace),
            "quota_root": str(self.binding.quota_root),
            "project_id": project_id,
            "quota_bytes": self.storage.quota_bytes,
        }

    def public_contract(self, evidence: dict[str, Any]) -> dict[str, Any]:
        expected = {
            "username": self.managed.unix_username,
            "uid": self.managed.uid,
            "gid": self.managed.gid,
        }
        if any(evidence.get(key) != value for key, value in expected.items()):
            raise _error(
                409,
                "WORKSPACE_POSTCONDITION_FAILED",
                "Workspace verification did not match the authenticated owner.",
            )
        required_true = (
            "ownership_verified",
            "writable",
            "same_inode",
            "quota_mapping_valid",
            "quota_enforced",
            "required_directories_ready",
        )
        if any(evidence.get(key) is not True for key in required_true):
            raise _error(
                409,
                "WORKSPACE_NOT_READY",
                "The owner workspace did not pass all readiness checks.",
            )
        required_pass = ("mount_status", "permission_status", "storage_status")
        if any(evidence.get(key) != "PASS" for key in required_pass):
            raise _error(
                409,
                "WORKSPACE_NOT_READY",
                "The workspace mount or storage status is not ready.",
            )
        return {
            "workspace_id": str(self.workspace_id),
            "owner_id": str(self.managed.id),
            "username": self.managed.unix_username,
            "uid": self.managed.uid,
            "gid": self.managed.gid,
            "canonical_path": str(self.binding.canonical_workspace),
            "container_path": str(self.binding.container_workspace),
            "compute_runtime_path": str(self.binding.compute_workspace),
            "container_home_path": str(self.binding.compute_home),
            "compute_home_path": str(self.binding.compute_home),
            "persistent_paths": [
                str(self.binding.container_workspace),
                str(self.binding.compute_home),
            ],
            "backing_layout": self.binding.layout.value,
            "mount_contract_version": WORKSPACE_MOUNT_CONTRACT_VERSION,
            "quota": {
                "bytes": self.storage.quota_bytes,
                "gib": self.storage.quota_bytes // 1024**3,
                "reference": self.quota_reference,
                "mapping_valid": True,
                "enforced": True,
            },
            "ownership_verified": True,
            "writable": True,
            "same_inode": True,
            "required_directories_ready": True,
            "lifecycle_state": self.storage.state,
            "readiness": "READY",
        }


def resolve_workspace(db: Session, user: PortalUser) -> WorkspaceResolution:
    """Resolve a workspace only from the authenticated managed identity."""

    managed = managed_identity_for_user(db, user)
    storage = db.scalar(
        select(PortalStorageResource).where(
            PortalStorageResource.owner_managed_user_id == managed.id
        )
    )
    if storage is None:
        raise _error(409, "WORKSPACE_METADATA_MISSING", "Workspace metadata is unavailable.")
    try:
        binding = workspace_binding(managed.unix_username, managed.uid, managed.gid)
    except ValueError as exc:
        raise _error(
            409,
            "WORKSPACE_IDENTITY_REJECTED",
            "Managed workspace identity is invalid.",
        ) from exc
    if (
        managed.portal_user_id != user.id
        or storage.owner_managed_user_id != managed.id
        or storage.root_path != str(binding.canonical_workspace)
        or managed.project_id is None
        or managed.quota_bytes != WORKSPACE_QUOTA_BYTES
        or storage.quota_bytes != WORKSPACE_QUOTA_BYTES
        or storage.quota_bytes != managed.quota_bytes
        or storage.state not in {"STAGED", "ACTIVE", "PRESERVED", "RESTORING"}
    ):
        raise _error(
            409,
            "WORKSPACE_METADATA_REJECTED",
            "Workspace ownership, quota, or lifecycle metadata is inconsistent.",
        )
    return WorkspaceResolution(
        workspace_id=storage.id,
        managed=managed,
        storage=storage,
        binding=binding,
    )
