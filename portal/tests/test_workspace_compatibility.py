import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest
from h100_portal_contracts.workspace import (
    WORKSPACE_QUOTA_BYTES,
    WORKSPACE_REQUIRED_DIRECTORIES,
    WorkspaceBinding,
    WorkspaceLayout,
)
from h100_portal_worker import handlers
from h100_portal_worker.schemas import PayloadValidationError, validate_payload

PORTAL_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_ROOT = PORTAL_ROOT.parent


def test_workspace_check_schema_derives_paths_and_rejects_spoofing() -> None:
    payload = {
        "managed_user_id": "11111111-2222-4333-8444-555555555555",
        "username": "origin-pilot2",
        "uid": 20002,
        "gid": 20002,
        "workspace_path": "/storage/users/20002",
        "quota_root": "/srv/gpu-platform/users/origin-pilot2",
        "project_id": 30002,
        "quota_bytes": WORKSPACE_QUOTA_BYTES,
    }

    assert validate_payload("self.workspace.check", payload) == payload
    with pytest.raises(PayloadValidationError, match="managed owner"):
        validate_payload(
            "self.workspace.check",
            {**payload, "workspace_path": "/storage/users/20001"},
        )
    with pytest.raises(PayloadValidationError, match="quota"):
        validate_payload(
            "self.workspace.check",
            {**payload, "quota_bytes": WORKSPACE_QUOTA_BYTES + 1},
        )


def test_worker_workspace_readiness_proves_inode_mount_owner_directories_and_quota(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    quota_root = tmp_path / "origin-pilot2"
    workspace = quota_root / "workspace"
    workspace.mkdir(parents=True, mode=0o700)
    quota_root.chmod(0o700)
    workspace.chmod(0o700)
    uid = os.getuid()
    gid = os.getgid()
    for relative in WORKSPACE_REQUIRED_DIRECTORIES:
        target = workspace / relative
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.chmod(0o700)
    binding = WorkspaceBinding(
        username="origin-pilot2",
        uid=uid,
        gid=gid,
        layout=WorkspaceLayout.LEGACY_BIND_ALIAS,
        quota_root=PurePosixPath(quota_root),
        backing_workspace=PurePosixPath(workspace),
        canonical_workspace=PurePosixPath(workspace),
    )
    payload = {
        "username": binding.username,
        "uid": uid,
        "gid": gid,
        "workspace_path": str(workspace),
        "quota_root": str(quota_root),
        "project_id": 30002,
        "quota_bytes": WORKSPACE_QUOTA_BYTES,
    }
    monkeypatch.setattr(handlers, "workspace_binding", lambda *_args: binding)
    monkeypatch.setattr(handlers, "_managed_account", lambda _payload: object())
    monkeypatch.setattr(
        handlers,
        "_safe_file_lines",
        lambda path: (
            [f"30002:{quota_root}"]
            if path == handlers.PROJECTS_FILE
            else ["h100_origin-pilot2:30002"]
        ),
    )
    monkeypatch.setattr(
        handlers,
        "_verified_project_quota",
        lambda project_id, quota_gb: {
            "project_id": project_id,
            "hard_limit_gb": quota_gb,
            "enforcement": "ON",
        },
    )

    def fixed(name: str, args: list[str], timeout: float) -> dict[str, object]:
        assert name == "findmnt"
        assert timeout == 10
        return {
            "ok": True,
            "stdout": f"{workspace}\n" if "TARGET" in args else "rw,nosuid,nodev\n",
        }

    monkeypatch.setattr(handlers, "run_fixed", fixed)

    result = handlers._self_workspace_check(payload)

    assert result["status"] == "OK"
    assert result["ownership_verified"] is True
    assert result["same_inode"] is True
    assert result["quota_enforced"] is True
    assert result["required_directories_ready"] is True

    (workspace / ".portal/runtime").chmod(0o755)
    rejected = handlers._self_workspace_check(payload)
    assert rejected["status"] == "ERROR"
    assert rejected["error"]["code"] == "WORKSPACE_DIRECTORY_REJECTED"


def test_workspace_runtime_is_zero_copy_profile_aware_and_alias_aware() -> None:
    fstab = (PLATFORM_ROOT / "config/fstab").read_text()
    alias = (PLATFORM_ROOT / "scripts/h100-workspace-alias").read_text()
    common = (PLATFORM_ROOT / "scripts/h100-platform-common.sh").read_text()
    stage = (PLATFORM_ROOT / "scripts/h100-provision-stage").read_text()
    create = (PLATFORM_ROOT / "scripts/h100-container-create").read_text()
    gpu_runtime = (PLATFORM_ROOT / "scripts/h100-container-gpu-runtime").read_text()
    start = (PLATFORM_ROOT / "scripts/h100-container-start").read_text()
    delete = (PLATFORM_ROOT / "scripts/h100-container-delete").read_text()

    assert 'backing_workspace="${backing_root}/workspace"' in alias
    assert 'canonical_workspace="${H100_WORKSPACE_ROOT}/${user_uid}"' in alias
    assert "Options=bind,rw,nosuid,nodev" in alias
    assert "stat -c '%d:%i'" in alias
    assert "workspace identity differs from NSS" in alias
    assert "storage namespace root metadata is invalid" in alias
    assert "== 0:0:711" in alias
    assert "== 0:0:644" in alias
    assert "== root:root:711" not in alias
    assert "== root:root:644" not in alias
    assert "== 0:0:700" in alias
    assert '"${user_uid}:${user_gid}"' in alias
    assert "created_workspace_directories" in alias
    assert "wait_for_mount_contract" in alias
    assert "canonical workspace mount contract did not converge" in alias
    assert "h100-portal-worker\\.service" in alias
    assert "workspace_alias_host_namespace_pid=1" in alias
    assert 'nsenter --target "${workspace_alias_host_namespace_pid}" --mount' in alias
    assert '--root="/proc/${workspace_alias_host_namespace_pid}/root"' in alias
    assert "WORKSPACE ALIAS FAILURE CODE" in alias
    assert "TARGET,MAJ:MIN,FSROOT" in alias
    assert "worker_covered_alias_placeholder_ready" in alias
    assert "defer_worker_covered_alias_placeholder" in alias
    assert "finalize_worker_covered_alias_placeholder" in alias
    assert "WORKER_REMOVE_COLLISION" in alias
    assert alias.index('systemctl disable --now "${unit_name}"') < alias.index(
        "finalize_worker_covered_alias_placeholder"
    )
    assert "H100_WORKSPACE_ALIAS_NAMESPACE_REHEARSAL" in alias
    assert "workspace_mode_is_private()" not in alias
    assert alias.count("h100_workspace_mode_is_private") == 4
    assert "h100_workspace_mode_is_private()" in common
    assert "((8#${workspace_mode} & 0007) == 0)" in common
    assert '"${managed_uid}:${managed_gid}:700"' not in common
    assert "local action=$1" not in common
    assert "local h100_audit_action_value=$1" in common
    assert "local h100_audit_target_value=$2" in common
    assert "canonical workspace has dependent submounts" in alias
    assert "rsync" not in alias
    assert "/srv/gpu-platform/workspaces" not in fstab
    assert "/storage/users none bind" not in fstab
    assert "GPU_1_8CPU_32GB" in stage
    assert "GPU development profile requires max GPU one" in stage
    assert "target: /shared" in stage
    assert "target: /shared" in create
    assert "cuInit(0)" in gpu_runtime
    assert "count.value != 1" in gpu_runtime
    assert "cuCtxCreate_v2" in gpu_runtime
    assert "runtime: nvidia" not in stage
    assert "driver: nvidia" not in stage
    assert "DeviceRequests" in stage
    assert "length == 0" in stage
    assert "VERSION=4" in stage
    assert "WORKSPACE_LAYOUT=LEGACY_BIND_ALIAS" in stage
    assert "h100_require_workspace_alias" in start
    assert '"${H100_WORKSPACE_ALIAS_TOOL}" verify' in common
    assert 'isolation_marker_two="${backing_workspace}' in stage
    assert "stat -c '%d:%i' \"${workspace_root}\"" not in stage
    assert delete.index("workspace_alias_tool") < delete.index("rm -rf --one-file-system")


def test_gpu_policy_mutations_share_the_guard_lock() -> None:
    isolation = (PLATFORM_ROOT / "scripts/h100-user-gpu-isolation").read_text()
    guard = (PLATFORM_ROOT / "scripts/h100-gpu-bypass-guard").read_text()

    assert "GUARD_LOCK_FILE=/run/lock/h100-gpu-bypass-guard.lock" in isolation
    assert "LOCK_FILE=/run/lock/h100-gpu-bypass-guard.lock" in guard
    assert 'exec 8>"${GUARD_LOCK_FILE}"' in isolation
    assert "flock -x 8" in isolation
    assert isolation.index("flock -x 8") < isolation.index('case "${ACTION}" in')


def test_common_workspace_privacy_accepts_private_legacy_group_mode(tmp_path: Path) -> None:
    common = PLATFORM_ROOT / "scripts/h100-platform-common.sh"
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)

    def accepted(mode: int) -> bool:
        workspace.chmod(mode)
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'source "$1"; h100_workspace_mode_is_private "$2"',
                "workspace-mode-test",
                str(common),
                str(workspace),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    assert accepted(0o700)
    assert accepted(0o750)
    assert not accepted(0o755)
    assert not accepted(0o701)


def test_runtime_manifest_pins_workspace_execution_artifacts() -> None:
    manifest_path = PORTAL_ROOT / "deploy/worker-scripts.json"
    manifest = json.loads(manifest_path.read_text())
    expected = {
        "h100-workspace-alias": PLATFORM_ROOT / "scripts/h100-workspace-alias",
        "h100-platform-common": PLATFORM_ROOT / "scripts/h100-platform-common.sh",
        "h100-provision-stage": PLATFORM_ROOT / "scripts/h100-provision-stage",
        "h100-container-start": PLATFORM_ROOT / "scripts/h100-container-start",
        "h100-container-delete": PLATFORM_ROOT / "scripts/h100-container-delete",
    }

    assert set(expected) <= handlers.COMPUTE_STAGE_REQUIRED_SCRIPTS | {
        "h100-container-start",
        "h100-container-delete",
    }
    assert handlers.SCRIPT_ALLOWLIST["h100-workspace-alias"] == (
        "/usr/local/sbin/h100-workspace-alias"
    )
    for name, source in expected.items():
        assert manifest[name] == hashlib.sha256(source.read_bytes()).hexdigest()
