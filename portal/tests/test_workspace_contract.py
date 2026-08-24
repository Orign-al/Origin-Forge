from pathlib import PurePosixPath

import pytest
from h100_portal_contracts.workspace import (
    WORKSPACE_COMPUTE_PATH,
    WORKSPACE_MOUNT_CONTRACT_VERSION,
    WORKSPACE_QUOTA_BYTES,
    WORKSPACE_REQUIRED_DIRECTORIES,
    WorkspaceLayout,
    logical_workspace_path,
    workspace_binding,
    workspace_relative_parts,
)


def test_legacy_workspace_binding_keeps_quota_root_and_aliases_only_workspace() -> None:
    binding = workspace_binding("origin-pilot2", 20002, 20002)

    assert binding.layout is WorkspaceLayout.LEGACY_BIND_ALIAS
    assert binding.quota_root == PurePosixPath("/srv/gpu-platform/users/origin-pilot2")
    assert binding.backing_workspace == binding.quota_root / "workspace"
    assert binding.canonical_workspace == PurePosixPath("/storage/users/20002")
    assert binding.container_workspace == PurePosixPath("/workspace")
    assert binding.compute_workspace == WORKSPACE_COMPUTE_PATH == PurePosixPath("/workspace")
    assert binding.default_job_workdir == PurePosixPath("/storage/users/20002/projects")
    assert WORKSPACE_MOUNT_CONTRACT_VERSION == 1
    assert WORKSPACE_QUOTA_BYTES == 300 * 1024**3
    assert (
        PurePosixPath("projects"),
        PurePosixPath("datasets"),
        PurePosixPath("outputs"),
        PurePosixPath(".portal"),
        PurePosixPath(".portal/job-scripts"),
        PurePosixPath(".portal/jobs"),
        PurePosixPath(".portal/templates"),
        PurePosixPath(".portal/logs"),
        PurePosixPath(".portal/runtime"),
    ) == WORKSPACE_REQUIRED_DIRECTORIES


def test_workspace_binding_rejects_untrusted_owner_coordinates() -> None:
    for username, uid, gid in (
        ("../origin-pilot2", 20002, 20002),
        ("origin-pilot2", True, 20002),
        ("origin-pilot2", 20002, 19999),
    ):
        with pytest.raises(ValueError):
            workspace_binding(username, uid, gid)


def test_logical_workspace_paths_are_rollback_compatible_and_owner_relative() -> None:
    logical = logical_workspace_path("outputs", "result.txt")

    assert logical == PurePosixPath("workspace/outputs/result.txt")
    assert workspace_relative_parts(logical) == ("outputs", "result.txt")
    assert workspace_relative_parts("workspace") == ()


@pytest.mark.parametrize(
    "path",
    (
        "/storage/users/20002/projects/train.py",
        "projects/train.py",
        "workspace/../20001/secret",
        "workspace/projects/workspace/secret",
        "workspace//projects",
        "workspace/./projects",
        "workspace/projects/",
    ),
)
def test_logical_workspace_path_rejects_absolute_escape_and_duplicate_prefix(path: str) -> None:
    with pytest.raises(ValueError, match="logical workspace path is invalid"):
        workspace_relative_parts(path)
