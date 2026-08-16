import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from h100_portal_worker import local_image

DEPLOYMENT_VERSION = "a" * 40


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o444)


def _local_oci_fixture(
    root: Path,
    *,
    configured_user: str | None = None,
    architecture: str = "amd64",
    layer_urls: list[str] | None = None,
) -> dict[str, Any]:
    artifact_base = root / "controlled" / "artifacts" / "oci" / "standard-dev-base"
    config = {
        "architecture": architecture,
        "os": "linux",
        "config": {
            "User": configured_user,
            "Labels": dict(local_image.APPROVED_LABELS),
        },
    }
    config_bytes = _json_bytes(config)
    config_digest = _digest(config_bytes)
    layer_bytes = b"fixture-layer"
    layer_digest = _digest(layer_bytes)
    layer_descriptor: dict[str, Any] = {
        "mediaType": "application/vnd.oci.image.layer.v1.tar",
        "digest": layer_digest,
        "size": len(layer_bytes),
    }
    if layer_urls is not None:
        layer_descriptor["urls"] = layer_urls
    image_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [layer_descriptor],
    }
    manifest_bytes = _json_bytes(image_manifest)
    manifest_digest = _digest(manifest_bytes)
    digest_hex = manifest_digest.removeprefix("sha256:")
    layout = artifact_base / digest_hex / "layout"
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest_digest,
                "size": len(manifest_bytes),
                "platform": {"os": "linux", "architecture": "amd64"},
            }
        ],
    }
    _write(layout / "oci-layout", _json_bytes({"imageLayoutVersion": "1.0.0"}))
    _write(layout / "index.json", _json_bytes(index))
    _write(layout / "blobs" / "sha256" / digest_hex, manifest_bytes)
    _write(
        layout / "blobs" / "sha256" / config_digest.removeprefix("sha256:"),
        config_bytes,
    )
    layer_path = layout / "blobs" / "sha256" / layer_digest.removeprefix("sha256:")
    _write(layer_path, layer_bytes)
    for directory in sorted(
        (path for path in (root / "controlled").rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.chmod(0o755)

    deployment_path = root / "DEPLOYMENT_VERSION"
    _write(deployment_path, f"{DEPLOYMENT_VERSION}\n".encode())
    manifest_path = root / "local-image.json"
    canonical_manifest: dict[str, Any] = {
        "artifact_format": "OCI_LAYOUT",
        "artifact_local_path": str(layout),
        "manifest_digest": manifest_digest,
        "platform": "linux/amd64",
        "effective_user": "root",
        "source_reference": local_image.APPROVED_SOURCE_REFERENCE,
        "source_build_version": local_image.APPROVED_SOURCE_BUILD_VERSION,
        "approved_deployment_version": DEPLOYMENT_VERSION,
    }
    canonical_manifest["canonical_local_image_identity"] = local_image._canonical_identity(
        canonical_manifest
    )
    _write(manifest_path, _json_bytes(canonical_manifest))
    return {
        "artifact_base": artifact_base,
        "layout": layout,
        "manifest_path": manifest_path,
        "deployment_path": deployment_path,
        "canonical_manifest": canonical_manifest,
        "layer_path": layer_path,
    }


def _verify(fixture: dict[str, Any]) -> dict[str, Any]:
    return local_image.verify_local_image(
        fixture["manifest_path"],
        fixture["deployment_path"],
        owner_uid=os.getuid(),
        artifact_base=fixture["artifact_base"],
    )


def test_local_oci_accepts_null_config_user_as_effective_root(tmp_path: Path) -> None:
    fixture = _local_oci_fixture(tmp_path, configured_user=None)
    observed = _verify(fixture)
    assert observed["effective_user"] == "root"
    assert observed["manifest_digest"] == fixture["canonical_manifest"]["manifest_digest"]


def test_local_oci_rejects_missing_or_corrupt_blob(tmp_path: Path) -> None:
    missing = _local_oci_fixture(tmp_path / "missing")
    missing["layer_path"].unlink()
    with pytest.raises(local_image.LocalImageValidationError, match="LOCAL_BASE_IMAGE_UNAVAILABLE"):
        _verify(missing)

    corrupt = _local_oci_fixture(tmp_path / "corrupt")
    corrupt["layer_path"].chmod(0o644)
    corrupt["layer_path"].write_bytes(b"fixture-layez")
    corrupt["layer_path"].chmod(0o444)
    with pytest.raises(
        local_image.LocalImageValidationError, match="LOCAL_BASE_IMAGE_DIGEST_MISMATCH"
    ):
        _verify(corrupt)


def test_local_oci_rejects_remote_descriptor_and_platform_mismatch(tmp_path: Path) -> None:
    remote = _local_oci_fixture(
        tmp_path / "remote",
        layer_urls=["https://registry-1.docker.io/v2/forbidden"],
    )
    with pytest.raises(
        local_image.LocalImageValidationError,
        match="LOCAL_BASE_IMAGE_REMOTE_DESCRIPTOR_FORBIDDEN",
    ):
        _verify(remote)

    wrong_platform = _local_oci_fixture(tmp_path / "platform", architecture="arm64")
    with pytest.raises(
        local_image.LocalImageValidationError, match="LOCAL_BASE_IMAGE_PLATFORM_MISMATCH"
    ):
        _verify(wrong_platform)


def test_local_oci_rejects_non_root_effective_user(tmp_path: Path) -> None:
    fixture = _local_oci_fixture(tmp_path, configured_user="origin-pilot")
    with pytest.raises(
        local_image.LocalImageValidationError,
        match="LOCAL_BASE_IMAGE_EFFECTIVE_USER_INVALID",
    ):
        _verify(fixture)


def test_buildkit_resolution_uses_only_named_local_oci_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="#1 [internal] OCI load from client",
            stderr="",
        )

    monkeypatch.setattr(local_image.subprocess, "run", run)
    manifest = {
        "artifact_local_path": "/srv/gpu-platform/artifacts/oci/standard-dev-base/"
        + "d" * 64
        + "/layout",
        "manifest_digest": "sha256:" + "d" * 64,
    }
    local_image._verify_buildkit_local_source(manifest)
    context_index = observed.index("--build-context") + 1
    assert observed[context_index] == (
        "h100_base=oci-layout:///srv/gpu-platform/artifacts/oci/standard-dev-base/"
        + "d" * 64
        + "/layout@sha256:"
        + "d" * 64
    )
    assert "--pull=false" in observed
    assert "--network=none" in observed
    assert not any("docker.io" in argument for argument in observed)


def test_buildkit_resolution_rejects_non_local_source_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="#1 load metadata for docker.io/library/h100_base",
            stderr="",
        )

    monkeypatch.setattr(local_image.subprocess, "run", run)
    with pytest.raises(
        local_image.LocalImageValidationError,
        match="LOCAL_BASE_IMAGE_BUILDKIT_RESOLUTION_FAILED",
    ):
        local_image._verify_buildkit_local_source(
            {
                "artifact_local_path": "/srv/gpu-platform/artifacts/oci/standard-dev-base/"
                + "d" * 64
                + "/layout",
                "manifest_digest": "sha256:" + "d" * 64,
            }
        )
