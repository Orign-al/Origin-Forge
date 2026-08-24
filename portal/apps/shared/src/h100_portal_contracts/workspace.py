"""Portal-5A owner-bound workspace contract.

Existing quota roots remain username-keyed. New containers and Slurm jobs use
the UID-keyed canonical workspace, which is a bind alias of the existing
workspace directory. Both coordinates are derived from the same managed owner.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

MANAGED_UID_MIN = 20_000
MANAGED_UID_MAX = 60_000

WORKSPACE_ROOT = PurePosixPath("/storage/users")
LEGACY_STORAGE_ROOT = PurePosixPath("/srv/gpu-platform/users")
WORKSPACE_CONTAINER_PATH = PurePosixPath("/workspace")
WORKSPACE_COMPUTE_PATH = PurePosixPath("/workspace")
WORKSPACE_DEFAULT_WORKDIR = PurePosixPath("projects")
WORKSPACE_LOGICAL_ROOT = PurePosixPath("workspace")
WORKSPACE_MOUNT_CONTRACT_VERSION = 1
WORKSPACE_QUOTA_BYTES = 300 * 1024**3
WORKSPACE_REQUIRED_DIRECTORIES = (
    PurePosixPath("projects"),
    PurePosixPath("datasets"),
    PurePosixPath("outputs"),
    PurePosixPath(".portal"),
    PurePosixPath(".portal/job-scripts"),
    PurePosixPath(".portal/jobs"),
    PurePosixPath(".portal/templates"),
    PurePosixPath(".portal/logs"),
    PurePosixPath(".portal/runtime"),
)

MANAGED_USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class WorkspaceLayout(StrEnum):
    """Physical backing layout selected by server-owned lifecycle state."""

    LEGACY_BIND_ALIAS = "LEGACY_BIND_ALIAS"
    CANONICAL_NATIVE = "CANONICAL_NATIVE"


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    """All filesystem coordinates for one validated managed Linux identity."""

    username: str
    uid: int
    gid: int
    layout: WorkspaceLayout
    quota_root: PurePosixPath
    backing_workspace: PurePosixPath
    canonical_workspace: PurePosixPath
    container_workspace: PurePosixPath = WORKSPACE_CONTAINER_PATH
    compute_workspace: PurePosixPath = WORKSPACE_COMPUTE_PATH

    @property
    def default_job_workdir(self) -> PurePosixPath:
        return self.canonical_workspace / WORKSPACE_DEFAULT_WORKDIR


def _managed_id(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"managed {label} must be an integer")
    if not MANAGED_UID_MIN <= value <= MANAGED_UID_MAX:
        raise ValueError(f"managed {label} is outside the platform range")
    return value


def legacy_storage_path(username: str) -> PurePosixPath:
    """Return the authoritative legacy quota root for a managed username."""

    if not isinstance(username, str) or MANAGED_USERNAME_PATTERN.fullmatch(username) is None:
        raise ValueError("managed username is invalid")
    return LEGACY_STORAGE_ROOT / username


def workspace_path(uid: int) -> PurePosixPath:
    """Return the canonical workspace for a validated managed Linux UID."""

    return WORKSPACE_ROOT / str(_managed_id(uid, "UID"))


def workspace_binding(
    username: str,
    uid: int,
    gid: int,
    *,
    layout: WorkspaceLayout = WorkspaceLayout.LEGACY_BIND_ALIAS,
) -> WorkspaceBinding:
    """Derive every workspace coordinate from one managed owner binding."""

    validated_uid = _managed_id(uid, "UID")
    validated_gid = _managed_id(gid, "GID")
    canonical = workspace_path(validated_uid)
    if not isinstance(layout, WorkspaceLayout):
        raise ValueError("workspace layout is invalid")
    if layout is WorkspaceLayout.LEGACY_BIND_ALIAS:
        quota_root = legacy_storage_path(username)
        backing = quota_root / "workspace"
    else:
        quota_root = canonical
        backing = canonical
    return WorkspaceBinding(
        username=username,
        uid=validated_uid,
        gid=validated_gid,
        layout=layout,
        quota_root=quota_root,
        backing_workspace=backing,
        canonical_workspace=canonical,
    )


def logical_workspace_path(*relative_parts: str) -> PurePosixPath:
    """Build the rollback-compatible logical path stored in PortalJob rows."""

    path = WORKSPACE_LOGICAL_ROOT.joinpath(*relative_parts)
    workspace_relative_parts(path)
    return path


def workspace_relative_parts(value: str | PurePosixPath) -> tuple[str, ...]:
    """Strip one fixed ``workspace`` prefix from a safe logical job path."""

    raw = str(value)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or raw != path.as_posix()
        or path.parts[0] != str(WORKSPACE_LOGICAL_ROOT)
        or any(part in {"", ".", ".."} for part in path.parts)
        or str(WORKSPACE_LOGICAL_ROOT) in path.parts[1:]
    ):
        raise ValueError("logical workspace path is invalid")
    return path.parts[1:]
