from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Never

LOCAL_IMAGE_VALIDATOR_VERSION = "compute-provision-stage-local-image-v2"
LOCAL_IMAGE_SOURCE_TYPE = "LOCAL_OCI_LAYOUT"
LOCAL_IMAGE_ARTIFACT_BASE = Path("/srv/gpu-platform/artifacts/oci/standard-dev-base")
LOCAL_IMAGE_MANIFEST_PATH = Path("/etc/h100-portal/local-image.json")
DEPLOYMENT_VERSION_PATH = Path("/opt/h100-portal/DEPLOYMENT_VERSION")
APPROVED_SOURCE_CONTAINER = "gpu-dev-origin-pilot"
APPROVED_SOURCE_REFERENCE = "h100-local/dev-container:ubuntu24.04-origin-pilot-20260804"
APPROVED_SOURCE_BUILD_VERSION = "ubuntu24.04-origin-pilot-20260804"
APPROVED_PLATFORM = "linux/amd64"
APPROVED_EFFECTIVE_USER = "root"
APPROVED_LABELS = {
    "h100.dev.user": "origin-pilot",
    "h100.dev.uid": "20001",
    "h100.dev.gid": "20001",
}
DOCKER = "/usr/bin/docker"
BUILDKIT_RESOLUTION_TIMEOUT_SECONDS = 300
MAX_JSON_BYTES = 4 * 1024 * 1024
DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})")
DEPLOYMENT_RE = re.compile(r"[0-9a-f]{40}")
MANIFEST_FIELDS = {
    "artifact_format",
    "artifact_local_path",
    "manifest_digest",
    "platform",
    "effective_user",
    "source_reference",
    "source_build_version",
    "approved_deployment_version",
    "canonical_local_image_identity",
}
OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_CONFIG_MEDIA_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}
OCI_LAYER_MEDIA_TYPE_PREFIXES = (
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.docker.image.rootfs.diff.tar",
)


class LocalImageValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _raise(code: str) -> Never:
    raise LocalImageValidationError(code)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        _raise("LOCAL_BASE_IMAGE_UNAVAILABLE")
    return f"sha256:{digest.hexdigest()}"


def _secure_file(path: Path, *, owner_uid: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        _raise("LOCAL_BASE_IMAGE_UNAVAILABLE")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_mode & 0o022
        or (metadata.st_size > MAX_JSON_BYTES and path.suffix == ".json")
    ):
        _raise("LOCAL_BASE_IMAGE_PERMISSIONS_INVALID")
    return metadata


def _secure_directory(path: Path, *, owner_uid: int) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        _raise("LOCAL_BASE_IMAGE_UNAVAILABLE")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_mode & 0o022
    ):
        _raise("LOCAL_BASE_IMAGE_PERMISSIONS_INVALID")


def _read_json(path: Path, *, owner_uid: int) -> dict[str, Any]:
    metadata = _secure_file(path, owner_uid=owner_uid)
    if metadata.st_size > MAX_JSON_BYTES:
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    if not isinstance(value, dict):
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    return value


def _digest_hex(value: Any) -> str:
    if not isinstance(value, str):
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    match = DIGEST_RE.fullmatch(value)
    if match is None:
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    return match.group(1)


def _descriptor(value: Any, *, allowed_media_types: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    digest = value.get("digest")
    _digest_hex(digest)
    size = value.get("size")
    media_type = value.get("mediaType")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    if not isinstance(media_type, str):
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    if allowed_media_types is not None and media_type not in allowed_media_types:
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    urls = value.get("urls")
    if urls is not None and urls != []:
        _raise("LOCAL_BASE_IMAGE_REMOTE_DESCRIPTOR_FORBIDDEN")
    return value


def _blob(
    layout: Path,
    descriptor: dict[str, Any],
    *,
    owner_uid: int,
) -> Path:
    digest = str(descriptor["digest"])
    path = layout / "blobs" / "sha256" / _digest_hex(digest)
    metadata = _secure_file(path, owner_uid=owner_uid)
    if metadata.st_size != descriptor["size"]:
        _raise("LOCAL_BASE_IMAGE_DIGEST_MISMATCH")
    if _sha256_file(path) != digest:
        _raise("LOCAL_BASE_IMAGE_DIGEST_MISMATCH")
    return path


def _resolve_image_manifest_descriptor(
    layout: Path,
    descriptor: dict[str, Any],
    *,
    owner_uid: int,
) -> dict[str, Any]:
    """Resolve one approved linux/amd64 image from a local OCI descriptor.

    Docker's containerd image store exports a top-level OCI index descriptor
    whose referenced index contains the platform image plus a provenance
    attestation.  The attestation is local metadata, not a second runnable
    platform.  Accept exactly that shape while rejecting ambiguous platform
    choices and all remote descriptors.
    """
    media_type = str(descriptor["mediaType"])
    if media_type in OCI_MANIFEST_MEDIA_TYPES:
        platform = descriptor.get("platform")
        if platform is not None and (
            not isinstance(platform, dict)
            or platform.get("os") != "linux"
            or platform.get("architecture") != "amd64"
        ):
            _raise("LOCAL_BASE_IMAGE_PLATFORM_MISMATCH")
        return descriptor

    index_path = _blob(layout, descriptor, owner_uid=owner_uid)
    nested_index = _read_json(index_path, owner_uid=owner_uid)
    nested_media_type = nested_index.get("mediaType")
    raw_manifests = nested_index.get("manifests")
    if (
        nested_index.get("schemaVersion") != 2
        or (nested_media_type is not None and nested_media_type not in OCI_INDEX_MEDIA_TYPES)
        or not isinstance(raw_manifests, list)
        or not raw_manifests
    ):
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")

    platform_images: list[dict[str, Any]] = []
    attestations: list[dict[str, Any]] = []
    for raw_descriptor in raw_manifests:
        nested_descriptor = _descriptor(
            raw_descriptor,
            allowed_media_types=OCI_MANIFEST_MEDIA_TYPES,
        )
        # Every descriptor must resolve to a complete local blob even when it
        # is non-runnable provenance metadata.
        _blob(layout, nested_descriptor, owner_uid=owner_uid)
        platform = nested_descriptor.get("platform")
        if (
            isinstance(platform, dict)
            and platform.get("os") == "linux"
            and platform.get("architecture") == "amd64"
        ):
            platform_images.append(nested_descriptor)
            continue
        annotations = nested_descriptor.get("annotations")
        if (
            isinstance(platform, dict)
            and platform.get("os") == "unknown"
            and platform.get("architecture") == "unknown"
            and isinstance(annotations, dict)
            and annotations.get("vnd.docker.reference.type") == "attestation-manifest"
        ):
            attestations.append(nested_descriptor)
            continue
        _raise("LOCAL_BASE_IMAGE_PLATFORM_MISMATCH")

    if len(platform_images) != 1:
        _raise("LOCAL_BASE_IMAGE_PLATFORM_MISMATCH")
    image_descriptor = platform_images[0]
    for attestation in attestations:
        annotations = attestation["annotations"]
        if annotations.get("vnd.docker.reference.digest") != image_descriptor["digest"]:
            _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    return image_descriptor


def _verify_layout(
    layout: Path,
    manifest_digest: str,
    *,
    owner_uid: int,
    artifact_base: Path,
) -> dict[str, Any]:
    digest_hex = _digest_hex(manifest_digest)
    expected_layout = artifact_base / digest_hex / "layout"
    try:
        resolved = layout.resolve(strict=True)
        resolved_expected = expected_layout.resolve(strict=True)
    except OSError:
        _raise("LOCAL_BASE_IMAGE_UNAVAILABLE")
    if resolved != resolved_expected:
        _raise("LOCAL_BASE_IMAGE_PATH_INVALID")
    for directory in (
        artifact_base.parent.parent,
        artifact_base.parent,
        artifact_base,
        expected_layout.parent,
        expected_layout,
        expected_layout / "blobs",
        expected_layout / "blobs" / "sha256",
    ):
        _secure_directory(directory, owner_uid=owner_uid)

    layout_header = _read_json(expected_layout / "oci-layout", owner_uid=owner_uid)
    if layout_header != {"imageLayoutVersion": "1.0.0"}:
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    index = _read_json(expected_layout / "index.json", owner_uid=owner_uid)
    manifests = index.get("manifests")
    if index.get("schemaVersion") != 2 or not isinstance(manifests, list):
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    matching = [
        _descriptor(
            value,
            allowed_media_types=OCI_MANIFEST_MEDIA_TYPES | OCI_INDEX_MEDIA_TYPES,
        )
        for value in manifests
        if isinstance(value, dict) and value.get("digest") == manifest_digest
    ]
    if len(manifests) != 1 or len(matching) != 1:
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    index_descriptor = matching[0]
    platform = index_descriptor.get("platform")
    if platform is not None and (
        not isinstance(platform, dict)
        or platform.get("os") != "linux"
        or platform.get("architecture") != "amd64"
    ):
        _raise("LOCAL_BASE_IMAGE_PLATFORM_MISMATCH")

    image_descriptor = _resolve_image_manifest_descriptor(
        expected_layout,
        index_descriptor,
        owner_uid=owner_uid,
    )
    image_manifest_path = _blob(
        expected_layout,
        image_descriptor,
        owner_uid=owner_uid,
    )
    image_manifest = _read_json(image_manifest_path, owner_uid=owner_uid)
    config_descriptor = _descriptor(
        image_manifest.get("config"), allowed_media_types=OCI_CONFIG_MEDIA_TYPES
    )
    layers = image_manifest.get("layers")
    if image_manifest.get("schemaVersion") != 2 or not isinstance(layers, list):
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    config_path = _blob(expected_layout, config_descriptor, owner_uid=owner_uid)
    for raw_layer in layers:
        layer = _descriptor(raw_layer)
        media_type = str(layer["mediaType"])
        if not media_type.startswith(OCI_LAYER_MEDIA_TYPE_PREFIXES):
            _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
        _blob(expected_layout, layer, owner_uid=owner_uid)

    config = _read_json(config_path, owner_uid=owner_uid)
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        _raise("LOCAL_BASE_IMAGE_PLATFORM_MISMATCH")
    runtime_config = config.get("config")
    if not isinstance(runtime_config, dict):
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    configured_user = runtime_config.get("User")
    if configured_user not in {None, "", "root"}:
        _raise("LOCAL_BASE_IMAGE_EFFECTIVE_USER_INVALID")
    labels = runtime_config.get("Labels")
    if not isinstance(labels, dict) or any(
        labels.get(key) != value for key, value in APPROVED_LABELS.items()
    ):
        _raise("LOCAL_BASE_IMAGE_IDENTITY_LABELS_INVALID")
    return {
        "manifest_digest": manifest_digest,
        "platform": APPROVED_PLATFORM,
        "effective_user": APPROVED_EFFECTIVE_USER,
        "config_digest": config_descriptor["digest"],
        "layer_count": len(layers),
    }


def _canonical_identity(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def verify_local_image(
    manifest_path: Path = LOCAL_IMAGE_MANIFEST_PATH,
    deployment_version_path: Path = DEPLOYMENT_VERSION_PATH,
    *,
    owner_uid: int = 0,
    artifact_base: Path = LOCAL_IMAGE_ARTIFACT_BASE,
) -> dict[str, Any]:
    manifest = _read_json(manifest_path, owner_uid=owner_uid)
    if set(manifest) != MANIFEST_FIELDS:
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    if (
        manifest.get("artifact_format") != "OCI_LAYOUT"
        or manifest.get("platform") != APPROVED_PLATFORM
        or manifest.get("effective_user") != APPROVED_EFFECTIVE_USER
        or manifest.get("source_reference") != APPROVED_SOURCE_REFERENCE
        or manifest.get("source_build_version") != APPROVED_SOURCE_BUILD_VERSION
        or not isinstance(manifest.get("approved_deployment_version"), str)
        or DEPLOYMENT_RE.fullmatch(str(manifest["approved_deployment_version"])) is None
    ):
        _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
    try:
        deployment_version = deployment_version_path.read_text(encoding="ascii").strip()
    except OSError, UnicodeDecodeError:
        _raise("LOCAL_BASE_IMAGE_DEPLOYMENT_MISMATCH")
    if deployment_version != manifest["approved_deployment_version"]:
        _raise("LOCAL_BASE_IMAGE_DEPLOYMENT_MISMATCH")
    manifest_digest = str(manifest.get("manifest_digest", ""))
    _digest_hex(manifest_digest)
    artifact_path = manifest.get("artifact_local_path")
    if not isinstance(artifact_path, str) or not artifact_path.startswith("/"):
        _raise("LOCAL_BASE_IMAGE_PATH_INVALID")
    layout = Path(artifact_path)
    _verify_layout(
        layout,
        manifest_digest,
        owner_uid=owner_uid,
        artifact_base=artifact_base,
    )
    identity_payload = {
        key: manifest[key] for key in sorted(MANIFEST_FIELDS - {"canonical_local_image_identity"})
    }
    expected_identity = _canonical_identity(identity_payload)
    if manifest.get("canonical_local_image_identity") != expected_identity:
        _raise("LOCAL_BASE_IMAGE_IDENTITY_MISMATCH")
    return dict(manifest)


def _buildkit_context_reference(manifest: Mapping[str, Any]) -> str:
    artifact_path = manifest.get("artifact_local_path")
    manifest_digest = manifest.get("manifest_digest")
    if not isinstance(artifact_path, str) or not artifact_path.startswith("/"):
        _raise("LOCAL_BASE_IMAGE_PATH_INVALID")
    _digest_hex(manifest_digest)
    return f"oci-layout://{artifact_path}@{manifest_digest}"


def _verify_buildkit_local_source(manifest: Mapping[str, Any]) -> None:
    """Require BuildKit to consume the approved base through its local OCI source."""
    context_reference = _buildkit_context_reference(manifest)
    temporary = Path(tempfile.mkdtemp(prefix="h100-local-image-resolve."))
    dockerfile = temporary / "Dockerfile"
    try:
        dockerfile.write_text("FROM h100_base\n", encoding="ascii")
        completed = subprocess.run(
            [
                DOCKER,
                "buildx",
                "build",
                "--pull=false",
                "--network=none",
                "--provenance=false",
                "--sbom=false",
                "--progress=plain",
                "--build-context",
                f"h100_base={context_reference}",
                "--output",
                "type=cacheonly",
                str(temporary),
            ],
            cwd="/",
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=BUILDKIT_RESOLUTION_TIMEOUT_SECONDS,
            check=False,
            text=True,
        )
    except OSError, subprocess.TimeoutExpired:
        _raise("LOCAL_BASE_IMAGE_BUILDKIT_RESOLUTION_FAILED")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0 or "OCI load from client" not in output:
        _raise("LOCAL_BASE_IMAGE_BUILDKIT_RESOLUTION_FAILED")


def local_image_contract() -> dict[str, Any]:
    empty: dict[str, Any] = {
        "status": "FAIL",
        "validator_version": LOCAL_IMAGE_VALIDATOR_VERSION,
        "source_type": LOCAL_IMAGE_SOURCE_TYPE,
        "canonical_local_image_identity": None,
        "artifact_path": None,
        "manifest_digest": None,
        "platform": None,
        "effective_user": "UNKNOWN",
        "source_reference": None,
        "source_build_version": None,
        "approved_deployment_version": None,
        "failure_code": "LOCAL_BASE_IMAGE_UNAVAILABLE",
    }
    try:
        manifest = verify_local_image()
        _verify_buildkit_local_source(manifest)
    except LocalImageValidationError as exc:
        return {**empty, "failure_code": exc.code}
    return {
        "status": "PASS",
        "validator_version": LOCAL_IMAGE_VALIDATOR_VERSION,
        "source_type": LOCAL_IMAGE_SOURCE_TYPE,
        "canonical_local_image_identity": manifest["canonical_local_image_identity"],
        "artifact_path": manifest["artifact_local_path"],
        "manifest_digest": manifest["manifest_digest"],
        "platform": manifest["platform"],
        "effective_user": manifest["effective_user"],
        "source_reference": manifest["source_reference"],
        "source_build_version": manifest["source_build_version"],
        "approved_deployment_version": manifest["approved_deployment_version"],
        "failure_code": None,
    }


def _docker_output(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            [DOCKER, *args],
            cwd="/",
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
            text=True,
        )
    except OSError, subprocess.TimeoutExpired:
        _raise("LOCAL_BASE_IMAGE_UNAVAILABLE")
    if completed.returncode != 0:
        _raise("LOCAL_BASE_IMAGE_UNAVAILABLE")
    return completed.stdout.strip()


def _validate_daemon_source() -> str:
    try:
        reference = json.loads(
            _docker_output(
                [
                    "container",
                    "inspect",
                    "--format",
                    "{{json .Config.Image}}",
                    APPROVED_SOURCE_CONTAINER,
                ]
            )
        )
        container_image_id = json.loads(
            _docker_output(
                [
                    "container",
                    "inspect",
                    "--format",
                    "{{json .Image}}",
                    APPROVED_SOURCE_CONTAINER,
                ]
            )
        )
        image_id = json.loads(
            _docker_output(
                ["image", "inspect", "--format", "{{json .Id}}", APPROVED_SOURCE_REFERENCE]
            )
        )
    except json.JSONDecodeError:
        _raise("LOCAL_BASE_IMAGE_SOURCE_IDENTITY_INVALID")
    if (
        reference != APPROVED_SOURCE_REFERENCE
        or not isinstance(image_id, str)
        or DIGEST_RE.fullmatch(image_id) is None
        or container_image_id != image_id
    ):
        _raise("LOCAL_BASE_IMAGE_SOURCE_IDENTITY_INVALID")
    return image_id


def _set_artifact_permissions(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if path.is_symlink():
            _raise("LOCAL_BASE_IMAGE_PERMISSIONS_INVALID")
        os.chown(path, 0, 0)
        path.chmod(0o555 if path.is_dir() else 0o444)
    os.chown(root, 0, 0)
    root.chmod(0o555)


def _atomic_manifest(manifest: dict[str, Any]) -> None:
    LOCAL_IMAGE_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _secure_directory(LOCAL_IMAGE_MANIFEST_PATH.parent, owner_uid=0)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".local-image.", dir=LOCAL_IMAGE_MANIFEST_PATH.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temp, 0, 0)
        temp.chmod(0o444)
        os.replace(temp, LOCAL_IMAGE_MANIFEST_PATH)
    finally:
        temp.unlink(missing_ok=True)


def publish_local_image(deployment_version: str) -> dict[str, Any]:
    if os.geteuid() != 0:
        _raise("LOCAL_BASE_IMAGE_PUBLISH_REQUIRES_ROOT")
    if DEPLOYMENT_RE.fullmatch(deployment_version) is None:
        _raise("LOCAL_BASE_IMAGE_DEPLOYMENT_MISMATCH")
    try:
        installed_version = DEPLOYMENT_VERSION_PATH.read_text(encoding="ascii").strip()
    except OSError, UnicodeDecodeError:
        _raise("LOCAL_BASE_IMAGE_DEPLOYMENT_MISMATCH")
    if installed_version != deployment_version:
        _raise("LOCAL_BASE_IMAGE_DEPLOYMENT_MISMATCH")
    source_image_id = _validate_daemon_source()

    LOCAL_IMAGE_ARTIFACT_BASE.mkdir(parents=True, exist_ok=True)
    for path in (
        LOCAL_IMAGE_ARTIFACT_BASE.parent.parent,
        LOCAL_IMAGE_ARTIFACT_BASE.parent,
        LOCAL_IMAGE_ARTIFACT_BASE,
    ):
        os.chown(path, 0, 0)
        path.chmod(0o755)
    staging = Path(tempfile.mkdtemp(prefix=".publish-", dir=LOCAL_IMAGE_ARTIFACT_BASE))
    cleanup_staging: Path | None = staging
    archive = staging / "source-image.tar"
    layout = staging / "layout"
    layout.mkdir(mode=0o700)
    try:
        try:
            completed = subprocess.run(
                [
                    DOCKER,
                    "image",
                    "save",
                    "--output",
                    str(archive),
                    source_image_id,
                ],
                cwd="/",
                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=900,
                check=False,
                text=True,
            )
        except OSError, subprocess.TimeoutExpired:
            _raise("LOCAL_BASE_IMAGE_EXPORT_FAILED")
        if completed.returncode != 0:
            _raise("LOCAL_BASE_IMAGE_EXPORT_FAILED")
        try:
            with tarfile.open(archive, mode="r:*") as bundle:
                bundle.extractall(layout, filter="data")
        except OSError, tarfile.TarError:
            _raise("LOCAL_BASE_IMAGE_EXPORT_FAILED")
        archive.unlink()
        for legacy in (layout / "manifest.json", layout / "repositories"):
            legacy.unlink(missing_ok=True)
        try:
            index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
        except OSError, UnicodeDecodeError, json.JSONDecodeError:
            _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
        manifests = index.get("manifests") if isinstance(index, dict) else None
        if not isinstance(manifests, list) or len(manifests) != 1:
            _raise("LOCAL_BASE_IMAGE_MANIFEST_INVALID")
        manifest_digest = str(manifests[0].get("digest", ""))
        digest_hex = _digest_hex(manifest_digest)
        target = LOCAL_IMAGE_ARTIFACT_BASE / digest_hex
        target_layout = target / "layout"

        if target.exists():
            _verify_layout(
                target_layout,
                manifest_digest,
                owner_uid=0,
                artifact_base=LOCAL_IMAGE_ARTIFACT_BASE,
            )
        else:
            _set_artifact_permissions(staging)
            os.replace(staging, target)
            cleanup_staging = None
            _verify_layout(
                target_layout,
                manifest_digest,
                owner_uid=0,
                artifact_base=LOCAL_IMAGE_ARTIFACT_BASE,
            )

        manifest: dict[str, Any] = {
            "artifact_format": "OCI_LAYOUT",
            "artifact_local_path": str(target_layout),
            "manifest_digest": manifest_digest,
            "platform": APPROVED_PLATFORM,
            "effective_user": APPROVED_EFFECTIVE_USER,
            "source_reference": APPROVED_SOURCE_REFERENCE,
            "source_build_version": APPROVED_SOURCE_BUILD_VERSION,
            "approved_deployment_version": deployment_version,
        }
        manifest["canonical_local_image_identity"] = _canonical_identity(manifest)
        _atomic_manifest(manifest)
        return verify_local_image()
    finally:
        if cleanup_staging is not None and cleanup_staging.exists():
            shutil.rmtree(cleanup_staging)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the approved local OCI base image")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify")
    publish = subparsers.add_parser("publish")
    publish.add_argument("--deployment-version", required=True)
    args = parser.parse_args()
    try:
        if args.command == "publish":
            manifest = publish_local_image(str(args.deployment_version))
            result = {
                "status": "PASS",
                "source_type": LOCAL_IMAGE_SOURCE_TYPE,
                "canonical_local_image_identity": manifest["canonical_local_image_identity"],
                "manifest_digest": manifest["manifest_digest"],
                "artifact_path": manifest["artifact_local_path"],
                "effective_user": manifest["effective_user"],
            }
        else:
            result = local_image_contract()
    except LocalImageValidationError as exc:
        print(json.dumps({"status": "FAIL", "failure_code": exc.code}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
