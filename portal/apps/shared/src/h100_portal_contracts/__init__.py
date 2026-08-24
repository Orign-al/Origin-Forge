"""Cross-process contracts shared by the Portal API and Root Worker."""

from h100_portal_contracts.workspace import (
    LEGACY_STORAGE_ROOT,
    WORKSPACE_COMPUTE_PATH,
    WORKSPACE_CONTAINER_PATH,
    WORKSPACE_DEFAULT_WORKDIR,
    WORKSPACE_LOGICAL_ROOT,
    WORKSPACE_MOUNT_CONTRACT_VERSION,
    WORKSPACE_QUOTA_BYTES,
    WORKSPACE_REQUIRED_DIRECTORIES,
    WORKSPACE_ROOT,
    WorkspaceBinding,
    WorkspaceLayout,
    legacy_storage_path,
    logical_workspace_path,
    workspace_binding,
    workspace_path,
    workspace_relative_parts,
)

__all__ = [
    "LEGACY_STORAGE_ROOT",
    "WORKSPACE_COMPUTE_PATH",
    "WORKSPACE_CONTAINER_PATH",
    "WORKSPACE_DEFAULT_WORKDIR",
    "WORKSPACE_LOGICAL_ROOT",
    "WORKSPACE_MOUNT_CONTRACT_VERSION",
    "WORKSPACE_QUOTA_BYTES",
    "WORKSPACE_REQUIRED_DIRECTORIES",
    "WORKSPACE_ROOT",
    "WorkspaceBinding",
    "WorkspaceLayout",
    "legacy_storage_path",
    "logical_workspace_path",
    "workspace_binding",
    "workspace_path",
    "workspace_relative_parts",
]
