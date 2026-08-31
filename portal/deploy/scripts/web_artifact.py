#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

ARTIFACT_ROOT = ".next"
WEB_ENTRYPOINT = ".next/standalone/apps/web/server.js"


class ArtifactAuditError(RuntimeError):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _tree_entries(root: Path) -> list[Path]:
    entries: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        entries.extend(current_path / name for name in directories)
        entries.extend(current_path / name for name in files)
    return entries


def audit_tree(root: Path) -> dict[str, Any]:
    if not root.is_dir() or root.is_symlink():
        raise ArtifactAuditError(f"artifact tree root must be a real directory: {root}")

    root_real = root.resolve(strict=True)
    links: list[dict[str, str]] = []
    errors: list[str] = []
    inode_paths: dict[tuple[int, int], list[str]] = defaultdict(list)
    inode_links: dict[tuple[int, int], int] = {}
    file_count = 0

    entries = _tree_entries(root)
    for path in entries:
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            resolved_text = "UNRESOLVED"
            if os.path.isabs(target):
                errors.append(f"absolute symlink: {relative} -> {target}")
            else:
                try:
                    resolved = (path.parent / target).resolve(strict=False)
                    if not _inside(resolved, root_real):
                        resolved_text = "OUTSIDE_ARTIFACT"
                        errors.append(f"escaping symlink: {relative} -> {target}")
                    elif not path.exists():
                        resolved_text = resolved.relative_to(root_real).as_posix()
                        errors.append(f"missing symlink target: {relative} -> {target}")
                    else:
                        resolved_text = resolved.relative_to(root_real).as_posix()
                except RuntimeError:
                    errors.append(f"symlink resolution loop: {relative} -> {target}")
            links.append({"path": relative, "raw_target": target, "resolved_target": resolved_text})
            continue
        if stat.S_ISREG(metadata.st_mode):
            file_count += 1
            inode = (metadata.st_dev, metadata.st_ino)
            inode_paths[inode].append(relative)
            inode_links[inode] = metadata.st_nlink
        elif not stat.S_ISDIR(metadata.st_mode):
            errors.append(f"unsupported filesystem entry: {relative}")

    external_hardlinks = 0
    for inode, paths in inode_paths.items():
        if inode_links[inode] > len(paths):
            external_hardlinks += 1
            errors.append(f"hardlink escapes artifact tree: {paths[0]}")

    result: dict[str, Any] = {
        "kind": "tree",
        "root": ".",
        "member_count": len(entries) + 1,
        "file_count": file_count,
        "symlink_count": len(links),
        "absolute_symlinks": sum(os.path.isabs(link["raw_target"]) for link in links),
        "escaping_symlinks": sum("escaping symlink:" in error for error in errors),
        "missing_symlink_targets": sum("missing symlink target:" in error for error in errors),
        "absolute_hardlinks": 0,
        "escaping_hardlinks": external_hardlinks,
        "external_runtime_dependencies": len(errors),
        "links": links,
        "errors": errors,
        "status": "PASS" if not errors else "FAIL",
    }
    if errors:
        raise ArtifactAuditError("; ".join(errors))
    return result


def _member_path(name: str) -> tuple[str, bool, bool]:
    path = PurePosixPath(name)
    absolute = path.is_absolute() or name.startswith("/")
    traversal = any(part == ".." for part in path.parts)
    normalized = PurePosixPath(*(part for part in path.parts if part not in {"", "."})).as_posix()
    return normalized, absolute, traversal


def _resolve_archive_link(member_name: str, target: str, *, hardlink: bool) -> tuple[str, bool]:
    if target.startswith("/") or PurePosixPath(target).is_absolute():
        return target, True
    base = [] if hardlink else list(PurePosixPath(member_name).parent.parts)
    escaped = False
    for part in PurePosixPath(target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not base:
                escaped = True
                continue
            base.pop()
        else:
            base.append(part)
    return PurePosixPath(*base).as_posix(), escaped


def audit_archive(archive: Path) -> dict[str, Any]:
    if not archive.is_file() or archive.is_symlink():
        raise ArtifactAuditError(f"archive must be a real file: {archive}")

    errors: list[str] = []
    links: list[dict[str, str]] = []
    absolute_symlinks = 0
    escaping_symlinks = 0
    absolute_hardlinks = 0
    escaping_hardlinks = 0
    unsupported_members = 0

    try:
        with tarfile.open(archive, mode="r:gz") as handle:
            members = handle.getmembers()
    except (tarfile.TarError, OSError) as exc:
        raise ArtifactAuditError(f"invalid Web artifact archive: {exc}") from exc

    normalized_names: list[str] = []
    for member in members:
        normalized, absolute, traversal = _member_path(member.name)
        normalized_names.append(normalized)
        if absolute:
            errors.append(f"absolute archive member: {member.name}")
        if traversal:
            errors.append(f"archive member contains traversal: {member.name}")
        if normalized != ARTIFACT_ROOT and not normalized.startswith(f"{ARTIFACT_ROOT}/"):
            errors.append(f"archive member outside {ARTIFACT_ROOT}: {member.name}")
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            unsupported_members += 1
            errors.append(f"unsupported archive member type: {member.name}")

    duplicates = [name for name, count in Counter(normalized_names).items() if count > 1]
    errors.extend(f"duplicate archive member: {name}" for name in duplicates)
    member_names = set(normalized_names)

    for member, normalized in zip(members, normalized_names, strict=True):
        if not (member.issym() or member.islnk()):
            continue
        target = member.linkname
        absolute = target.startswith("/") or PurePosixPath(target).is_absolute()
        resolved, escaped = _resolve_archive_link(normalized, target, hardlink=member.islnk())
        link_kind = "hardlink" if member.islnk() else "symlink"
        if member.issym():
            absolute_symlinks += int(absolute)
            escaping_symlinks += int(escaped)
        else:
            absolute_hardlinks += int(absolute)
            escaping_hardlinks += int(escaped)
        if absolute:
            errors.append(f"absolute {link_kind}: {normalized} -> {target}")
        elif escaped:
            errors.append(f"escaping {link_kind}: {normalized} -> {target}")
        elif resolved not in member_names:
            errors.append(f"missing {link_kind} target: {normalized} -> {target}")
        links.append(
            {
                "kind": link_kind,
                "path": normalized,
                "raw_target": target,
                "resolved_target": resolved,
            }
        )

    result: dict[str, Any] = {
        "kind": "archive",
        "archive": archive.name,
        "member_count": len(members),
        "file_count": sum(member.isfile() for member in members),
        "symlink_count": sum(member.issym() for member in members),
        "hardlink_count": sum(member.islnk() for member in members),
        "absolute_symlinks": absolute_symlinks,
        "escaping_symlinks": escaping_symlinks,
        "absolute_hardlinks": absolute_hardlinks,
        "escaping_hardlinks": escaping_hardlinks,
        "unsupported_members": unsupported_members,
        "external_runtime_dependencies": len(errors),
        "links": links,
        "errors": errors,
        "status": "PASS" if not errors else "FAIL",
    }
    if errors:
        raise ArtifactAuditError("; ".join(errors))
    return result


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.pax_headers = {}
    return info


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_artifact(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    manifest_path = args.manifest.resolve()
    if output == manifest_path:
        raise ArtifactAuditError("output archive and manifest paths must be distinct")
    if output.exists() or manifest_path.exists():
        raise ArtifactAuditError("output archive and manifest paths must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    source_dependencies = source / "standalone/node_modules"
    source_entrypoint = source / "standalone/apps/web/server.js"
    source_static = source / "static"
    source_build_id = source / "BUILD_ID"
    if not source_dependencies.is_dir() or source_dependencies.is_symlink():
        raise ArtifactAuditError("standalone/node_modules must be a real self-contained directory")
    if not source_entrypoint.is_file() or source_entrypoint.is_symlink():
        raise ArtifactAuditError("standalone Web entrypoint is unavailable")
    if not source_static.is_dir() or source_static.is_symlink():
        raise ArtifactAuditError("Next static asset directory is unavailable")
    if not source_build_id.is_file() or source_build_id.is_symlink():
        raise ArtifactAuditError("Next BUILD_ID is unavailable")

    source_audit = audit_tree(source / "standalone")

    with tempfile.TemporaryDirectory(prefix=".h100-web-artifact-", dir=output.parent) as temp:
        stage = Path(temp) / ARTIFACT_ROOT
        stage.mkdir()
        shutil.copytree(source / "standalone", stage / "standalone", symlinks=True)
        shutil.copytree(source_static, stage / "static", symlinks=True)
        shutil.copy2(source_build_id, stage / "BUILD_ID", follow_symlinks=False)
        static_target = stage / "standalone/apps/web/.next/static"
        shutil.copytree(stage / "static", static_target, symlinks=True, dirs_exist_ok=True)
        stage_audit = audit_tree(stage)
        with (
            output.open("xb") as raw_output,
            gzip.GzipFile(fileobj=raw_output, mode="wb", mtime=0, filename="") as compressed,
            tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
        ):
            archive.dereference = False
            archive.add(stage, arcname=ARTIFACT_ROOT, recursive=True, filter=_normalized_tar_info)

    archive_audit = audit_archive(output)
    build_id = source_build_id.read_text(encoding="utf-8").strip()
    if not build_id:
        raise ArtifactAuditError("Next BUILD_ID is empty")
    manifest: dict[str, Any] = {
        "artifact_version": 1,
        "git_commit": args.git_commit,
        "git_tree": args.git_tree,
        "production_parent": args.production_parent,
        "build_timestamp": datetime.now(UTC).isoformat(),
        "cli_version": args.cli_version,
        "db_migration_head": args.migration_head,
        "web_build_id": build_id,
        "web_entrypoint": WEB_ENTRYPOINT,
        "file_count": archive_audit["file_count"],
        "member_count": archive_audit["member_count"],
        "archive_sha256": _sha256(output),
        "absolute_symlinks": archive_audit["absolute_symlinks"],
        "escaping_symlinks": archive_audit["escaping_symlinks"],
        "absolute_hardlinks": archive_audit["absolute_hardlinks"],
        "escaping_hardlinks": archive_audit["escaping_hardlinks"],
        "external_runtime_dependencies": archive_audit["external_runtime_dependencies"],
        "source_audit": source_audit,
        "staging_audit": stage_audit,
        "archive_audit": archive_audit,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _write_result(result: dict[str, Any], json_output: Path | None, quiet: bool) -> None:
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if json_output is not None:
        json_output.write_text(rendered, encoding="utf-8")
    if not quiet:
        sys.stdout.write(rendered)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Build and audit relocatable H100 Portal Web artifacts"
    )
    subcommands = root.add_subparsers(dest="command", required=True)

    tree = subcommands.add_parser("audit-tree")
    tree.add_argument("path", type=Path)
    tree.add_argument("--json-output", type=Path)
    tree.add_argument("--quiet", action="store_true")

    archive = subcommands.add_parser("audit-archive")
    archive.add_argument("path", type=Path)
    archive.add_argument("--json-output", type=Path)
    archive.add_argument("--quiet", action="store_true")

    build = subcommands.add_parser("build")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--git-commit", required=True)
    build.add_argument("--git-tree", required=True)
    build.add_argument("--production-parent", required=True)
    build.add_argument("--cli-version", required=True)
    build.add_argument("--migration-head", required=True)
    build.add_argument("--quiet", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "audit-tree":
            result = audit_tree(args.path)
            _write_result(result, args.json_output, args.quiet)
        elif args.command == "audit-archive":
            result = audit_archive(args.path)
            _write_result(result, args.json_output, args.quiet)
        else:
            result = build_artifact(args)
            if not args.quiet:
                sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    except ArtifactAuditError as exc:
        print(f"Web artifact rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
