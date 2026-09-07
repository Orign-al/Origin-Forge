from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

PORTAL_ROOT = Path(__file__).resolve().parents[1]
AUDITOR = PORTAL_ROOT / "deploy/scripts/web_artifact.py"
RUNTIME_INSTALLER = PORTAL_ROOT / "deploy/scripts/install-runtime.sh"
WEB_INSTALLER = PORTAL_ROOT / "deploy/scripts/install-web-artifact.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(AUDITOR), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _minimal_next(root: Path) -> Path:
    source = root / ".next"
    dependencies = source / "standalone/node_modules/package"
    web = source / "standalone/apps/web"
    static = source / "static/chunks"
    dependencies.mkdir(parents=True)
    web.mkdir(parents=True)
    static.mkdir(parents=True)
    (dependencies / "index.js").write_text("module.exports = true;\n")
    (web / "server.js").write_text("require('package');\n")
    (static / "app.js").write_text("console.log('asset');\n")
    (source / "BUILD_ID").write_text("test-build-id\n")
    return source


def _archive_with_member(archive: Path, member: tarfile.TarInfo) -> None:
    with tarfile.open(archive, "w:gz") as handle:
        root = tarfile.TarInfo(".next")
        root.type = tarfile.DIRTYPE
        handle.addfile(root)
        handle.addfile(member)


def _build_artifact(
    source: Path, archive: Path, manifest: Path
) -> subprocess.CompletedProcess[str]:
    return _run(
        "build",
        "--source",
        str(source),
        "--output",
        str(archive),
        "--manifest",
        str(manifest),
        "--git-commit",
        "1" * 40,
        "--git-tree",
        "2" * 40,
        "--production-parent",
        "3" * 40,
        "--cli-version",
        "h100 1.0.0",
        "--migration-head",
        "c1d2e3f4a5b6",
        "--quiet",
    )


def test_tree_audit_accepts_relative_internal_symlink(tmp_path: Path) -> None:
    source = _minimal_next(tmp_path)
    os.symlink("package", source / "standalone/node_modules/package-alias")

    result = _run("audit-tree", str(source))

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["absolute_symlinks"] == 0
    assert payload["escaping_symlinks"] == 0
    assert payload["links"] == [
        {
            "path": "standalone/node_modules/package-alias",
            "raw_target": "package",
            "resolved_target": "standalone/node_modules/package",
        }
    ]


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("/src/portal/node_modules", "absolute symlink"),
        ("../../../../outside", "escaping symlink"),
        ("missing", "missing symlink target"),
    ],
)
def test_tree_audit_rejects_unsafe_symlinks(tmp_path: Path, target: str, message: str) -> None:
    source = _minimal_next(tmp_path)
    os.symlink(target, source / "standalone/node_modules/unsafe")

    result = _run("audit-tree", str(source))

    assert result.returncode == 1
    assert message in result.stderr


def test_tree_audit_rejects_hardlink_outside_artifact(tmp_path: Path) -> None:
    source = _minimal_next(tmp_path)
    external = tmp_path / "external"
    external.write_text("external\n")
    os.link(external, source / "standalone/node_modules/external-hardlink")

    result = _run("audit-tree", str(source))

    assert result.returncode == 1
    assert "hardlink escapes artifact tree" in result.stderr


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("/absolute", "absolute archive member"),
        (".next/../outside", "archive member contains traversal"),
        ("outside", "archive member outside .next"),
    ],
)
def test_archive_audit_rejects_unsafe_member_paths(tmp_path: Path, name: str, message: str) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    _archive_with_member(archive, tarfile.TarInfo(name))

    result = _run("audit-archive", str(archive))

    assert result.returncode == 1
    assert message in result.stderr


@pytest.mark.parametrize(
    ("entry_type", "target", "message"),
    [
        (tarfile.SYMTYPE, "/src/portal/node_modules", "absolute symlink"),
        (tarfile.SYMTYPE, "../../../outside", "escaping symlink"),
        (tarfile.SYMTYPE, "missing", "missing symlink target"),
        (tarfile.LNKTYPE, "/src/portal/node_modules", "absolute hardlink"),
        (tarfile.LNKTYPE, "../outside", "escaping hardlink"),
        (tarfile.LNKTYPE, ".next/missing", "missing hardlink target"),
    ],
)
def test_archive_audit_rejects_unsafe_links(
    tmp_path: Path, entry_type: bytes, target: str, message: str
) -> None:
    archive = tmp_path / "unsafe-link.tar.gz"
    member = tarfile.TarInfo(".next/unsafe")
    member.type = entry_type
    member.linkname = target
    _archive_with_member(archive, member)

    result = _run("audit-archive", str(archive))

    assert result.returncode == 1
    assert message in result.stderr


def test_archive_audit_rejects_duplicate_and_unsupported_members(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.tar.gz"
    with tarfile.open(duplicate, "w:gz") as handle:
        handle.addfile(tarfile.TarInfo(".next"))
        handle.addfile(tarfile.TarInfo(".next"))
    duplicate_result = _run("audit-archive", str(duplicate))
    assert duplicate_result.returncode == 1
    assert "duplicate archive member" in duplicate_result.stderr

    device = tmp_path / "device.tar.gz"
    member = tarfile.TarInfo(".next/device")
    member.type = tarfile.CHRTYPE
    _archive_with_member(device, member)
    device_result = _run("audit-archive", str(device))
    assert device_result.returncode == 1
    assert "unsupported archive member type" in device_result.stderr


def test_build_is_minimal_self_contained_and_deterministic(tmp_path: Path) -> None:
    source = _minimal_next(tmp_path / "source")
    os.symlink("package", source / "standalone/node_modules/package-alias")
    (source / "cache").mkdir()
    (source / "cache/build-host-path.txt").write_text("/src/portal\n")

    archives = [tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"]
    manifests = [tmp_path / "first.json", tmp_path / "second.json"]
    for archive, manifest in zip(archives, manifests, strict=True):
        result = _build_artifact(source, archive, manifest)
        assert result.returncode == 0, result.stderr

    assert (
        hashlib.sha256(archives[0].read_bytes()).digest()
        == hashlib.sha256(archives[1].read_bytes()).digest()
    )
    with tarfile.open(archives[0], "r:gz") as handle:
        names = {member.name for member in handle.getmembers()}
    assert ".next/standalone/apps/web/server.js" in names
    assert ".next/standalone/apps/web/.next/static/chunks/app.js" in names
    assert ".next/cache/build-host-path.txt" not in names

    manifest = json.loads(manifests[0].read_text())
    assert manifest["archive_sha256"] == hashlib.sha256(archives[0].read_bytes()).hexdigest()
    assert manifest["absolute_symlinks"] == 0
    assert manifest["escaping_symlinks"] == 0
    assert manifest["absolute_hardlinks"] == 0
    assert manifest["escaping_hardlinks"] == 0
    assert manifest["external_runtime_dependencies"] == 0
    assert str(tmp_path) not in manifests[0].read_text()


def test_runtime_installer_audits_web_tree_before_first_rsync() -> None:
    installer = RUNTIME_INSTALLER.read_text()
    audit = 'audit-tree "$SOURCE_DIR/apps/web/.next" --quiet'
    first_rsync = "rsync -a --chown=root:root --chmod=Fgo-w,Dgo-w"
    web_sync = "rsync -a --checksum --delete --chown=root:root --chmod=Fgo-w,Dgo-w"
    runtime_audit = 'audit-tree "$RUNTIME_WEB_NEXT" --quiet'

    assert audit in installer
    assert installer.index(audit) < installer.index(first_rsync)
    assert '&& ! -L "$SOURCE_DIR/apps/web/.next/standalone/node_modules"' in installer
    assert "--exclude=/apps/web/.next/" in installer
    assert web_sync in installer
    assert runtime_audit in installer
    assert installer.index(web_sync) < installer.index(runtime_audit)
    assert '[[ -d "$RUNTIME_WEB_NEXT" && ! -L "$RUNTIME_WEB_NEXT" ]]' in installer


def test_web_installer_rejects_symlinked_target_tree(tmp_path: Path) -> None:
    source = _minimal_next(tmp_path / "source")
    archive = tmp_path / "artifact.tar.gz"
    manifest = tmp_path / "manifest.json"
    built = _build_artifact(source, archive, manifest)
    assert built.returncode == 0, built.stderr
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    os.symlink(outside, target / ".next")

    result = subprocess.run(
        [str(WEB_INSTALLER), str(archive), str(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "Web .next target must be a real directory" in result.stderr
    assert not list(outside.iterdir())


def test_web_installer_replaces_same_size_same_mtime_release_files(tmp_path: Path) -> None:
    old_source = _minimal_next(tmp_path / "old-source")
    new_source = _minimal_next(tmp_path / "new-source")
    (old_source / "BUILD_ID").write_text("old-build-id\n")
    (new_source / "BUILD_ID").write_text("new-build-id\n")
    old_server = old_source / "standalone/apps/web/server.js"
    new_server = new_source / "standalone/apps/web/server.js"
    old_server.write_text("module.exports='old';\n")
    new_server.write_text("module.exports='new';\n")
    assert old_server.stat().st_size == new_server.stat().st_size
    old_only = old_source / "standalone/apps/web/old-only"
    old_only.write_text("removed\n")

    old_archive = tmp_path / "old.tar.gz"
    new_archive = tmp_path / "new.tar.gz"
    assert _build_artifact(old_source, old_archive, tmp_path / "old.json").returncode == 0
    assert _build_artifact(new_source, new_archive, tmp_path / "new.json").returncode == 0
    target = tmp_path / "target"
    target.mkdir()

    old_install = subprocess.run(
        [str(WEB_INSTALLER), str(old_archive), str(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert old_install.returncode == 0, old_install.stderr
    installed_server = target / ".next/standalone/apps/web/server.js"
    old_metadata = installed_server.stat()

    new_install = subprocess.run(
        [str(WEB_INSTALLER), str(new_archive), str(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert new_install.returncode == 0, new_install.stderr
    assert (target / ".next/BUILD_ID").read_text() == "new-build-id\n"
    assert installed_server.read_text() == "module.exports='new';\n"
    assert installed_server.stat().st_size == old_metadata.st_size
    assert installed_server.stat().st_mtime_ns == old_metadata.st_mtime_ns
    assert not (target / ".next/standalone/apps/web/old-only").exists()
