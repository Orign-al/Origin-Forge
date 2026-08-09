import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KNOWN_READS = {
    "platform.health.read",
    "gpu.list",
    "gpu.health.read",
    "slurm.node.read",
    "slurm.jobs.read",
    "slurm.accounts.read",
    "slurm.history.read",
    "containers.list",
    "containers.inspect",
    "storage.summary.read",
    "quotas.list",
    "systemd.failed.read",
    "monitoring.alerts.read",
    "monitoring.summary.read",
    "registry.status.read",
    "images.list",
    "gpu_isolation.status.read",
    "ssh.policy.read",
}
KNOWN_WRITES = {
    "user.plan",
    "user.stage",
    "user.activate",
    "user.activate.rollback",
    "user.suspend",
    "container.start",
    "container.stop",
    "container.restart",
    "container.rebuild",
    "slurm.drain",
    "slurm.resume",
    "job.cancel",
    "quota.update",
    "ssh_key.add",
    "ssh_key.revoke",
    "ssh_key.prepare",
    "ssh_key.discard",
}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,127}$")
SAFE_USERNAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SAFE_APPROVAL_REFERENCE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
STAGE_PUBLIC_KEY_FIELDS = {
    "public_key_file",
    "public_key_path",
    "public_key",
    "raw_public_key",
    "approved_ssh_key_record_ids",
}
FORBIDDEN_SECRET_OR_COMMAND_FIELDS = {
    "raw_private_key",
    "private_key",
    "private_key_password",
    "private_key_path",
    "password",
    "command",
    "argv",
    "path",
}
APPROVED_SSH_KEY_TYPES = {
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "sk-ssh-ed25519@openssh.com",
}
PRIVATE_KEY_MARKERS = ("PRIVATE KEY",)
APPROVED_STAGE_PAYLOAD: dict[str, Any] = {
    "username": "origin-pilot",
    "uid": 20001,
    "gid": 20001,
    "project_id": 30001,
    "ssh_port": 22023,
    "quota_gb": 300,
    "slurm_account": "company",
    "slurm_qos": "general",
    "max_gpus": 1,
    "container_name": "gpu-dev-origin-pilot",
    "cpus": 8,
    "memory_gb": 32,
    "pids_limit": 4096,
    "gpu": "none",
    "expected_state": "DRAFT",
}


class PayloadValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1]
    request_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    operation_type: str = Field(min_length=3, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    approved_by: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,128}$")
    dry_run: bool = False

    @field_validator("operation_type")
    @classmethod
    def known_operation(cls, value: str) -> str:
        if value not in KNOWN_READS | KNOWN_WRITES:
            raise ValueError("unknown operation type")
        return value


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise PayloadValidationError("PAYLOAD_REJECTED", f"invalid {field}")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise PayloadValidationError("PAYLOAD_REJECTED", f"invalid {field}") from exc


def _validate_user_stage(payload: dict[str, Any], allow_legacy_stage: bool) -> dict[str, Any]:
    if set(payload) & STAGE_PUBLIC_KEY_FIELDS:
        raise PayloadValidationError(
            "PUBLIC_KEY_NOT_ALLOWED_DURING_STAGE",
            "SSH public keys are accepted only by user.activate",
        )
    if set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError("PAYLOAD_REJECTED", "secret or command fields are forbidden")
    requested_username = payload.get("username")
    if isinstance(requested_username, str) and requested_username.casefold() in {
        "root",
        "origin-al",
        "codexops",
    }:
        raise PayloadValidationError("PAYLOAD_REJECTED", "protected username")
    if allow_legacy_stage and set(payload) == {"username"}:
        username = payload.get("username")
        if not isinstance(username, str) or not SAFE_USERNAME.fullmatch(username):
            raise PayloadValidationError("PAYLOAD_REJECTED", "invalid username")
        if username in {"root", "origin-al", "codexops"}:
            raise PayloadValidationError("PAYLOAD_REJECTED", "protected username")
        return {"username": username}
    expected_fields = set(APPROVED_STAGE_PAYLOAD)
    payload_fields = set(payload)
    if payload_fields != expected_fields and payload_fields != expected_fields | {
        "approval_reference"
    }:
        raise PayloadValidationError("STAGE_PAYLOAD_REJECTED", "user.stage fields are incomplete")
    approval_reference = payload.get("approval_reference")
    if approval_reference is not None and (
        not isinstance(approval_reference, str)
        or not SAFE_APPROVAL_REFERENCE.fullmatch(approval_reference)
    ):
        raise PayloadValidationError("STAGE_PAYLOAD_REJECTED", "invalid approval reference")
    for field, expected in APPROVED_STAGE_PAYLOAD.items():
        if payload.get(field) != expected:
            raise PayloadValidationError(
                "STAGE_PLAN_MISMATCH", "user.stage payload differs from the validated plan"
            )
    result = dict(APPROVED_STAGE_PAYLOAD)
    if approval_reference is not None:
        result["approval_reference"] = approval_reference
    return result


def _validate_user_activate(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) & (STAGE_PUBLIC_KEY_FIELDS - {"approved_ssh_key_record_ids"}):
        raise PayloadValidationError(
            "ARBITRARY_PATH_REJECTED", "Activate accepts approved key record IDs, not paths"
        )
    if set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError("PAYLOAD_REJECTED", "secret or command fields are forbidden")
    key_ids = payload.get("approved_ssh_key_record_ids")
    if not isinstance(key_ids, list) or not key_ids:
        raise PayloadValidationError(
            "PUBLIC_KEY_REQUIRED_FOR_ACTIVATION",
            "at least one approved SSH key record is required",
        )
    if len(key_ids) > 5:
        raise PayloadValidationError("PAYLOAD_REJECTED", "too many SSH key records")
    expected_fields = {
        "managed_user_id",
        "approved_ssh_key_record_ids",
        "expected_state",
        "approval_reference",
    }
    optional_fields = {"dry_run_operation_id"}
    if frozenset(payload) not in {
        frozenset(expected_fields),
        frozenset(expected_fields | optional_fields),
    }:
        raise PayloadValidationError("ACTIVATE_PAYLOAD_REJECTED", "invalid user.activate fields")
    canonical_ids = [_canonical_uuid(item, "SSH key record ID") for item in key_ids]
    if len(set(canonical_ids)) != len(canonical_ids):
        raise PayloadValidationError("PUBLIC_KEY_DUPLICATE", "duplicate SSH key record ID")
    if payload.get("expected_state") != "STAGED":
        raise PayloadValidationError("USER_NOT_IN_STAGED_STATE", "expected state must be STAGED")
    approval_reference = payload.get("approval_reference")
    if not isinstance(approval_reference, str) or not SAFE_APPROVAL_REFERENCE.fullmatch(
        approval_reference
    ):
        raise PayloadValidationError("ACTIVATE_PAYLOAD_REJECTED", "invalid approval reference")
    result = {
        "managed_user_id": _canonical_uuid(payload.get("managed_user_id"), "managed user ID"),
        "approved_ssh_key_record_ids": canonical_ids,
        "expected_state": "STAGED",
        "approval_reference": approval_reference,
    }
    if "dry_run_operation_id" in payload:
        result["dry_run_operation_id"] = _canonical_uuid(
            payload.get("dry_run_operation_id"), "dry-run operation ID"
        )
    return result


def _validate_ssh_key_prepare(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError(
            "SSH_PRIVATE_KEY_UPLOAD_REJECTED", "private-key fields are forbidden"
        )
    expected_fields = {
        "record_id",
        "operation_id",
        "managed_user_id",
        "username",
        "public_key",
        "key_type",
        "fingerprint_sha256",
        "content_sha256",
        "scope",
    }
    if set(payload) != expected_fields:
        raise PayloadValidationError(
            "SSH_KEY_PREPARE_REJECTED", "SSH key prepare fields are incomplete"
        )
    public_key = payload.get("public_key")
    if not isinstance(public_key, str) or not 32 <= len(public_key.encode("utf-8")) <= 16 * 1024:
        raise PayloadValidationError(
            "SSH_KEY_PREPARE_REJECTED", "SSH public key has an invalid size"
        )
    upper = public_key.upper()
    if any(marker in upper for marker in PRIVATE_KEY_MARKERS):
        raise PayloadValidationError(
            "SSH_PRIVATE_KEY_UPLOAD_REJECTED", "private-key material is forbidden"
        )
    key_type = payload.get("key_type")
    fingerprint = payload.get("fingerprint_sha256")
    content_sha256 = payload.get("content_sha256")
    scope = payload.get("scope")
    if key_type not in APPROVED_SSH_KEY_TYPES:
        raise PayloadValidationError(
            "SSH_PUBLIC_KEY_TYPE_REJECTED", "SSH public-key type is not approved"
        )
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"SHA256:[A-Za-z0-9+/]+", fingerprint) is None
    ):
        raise PayloadValidationError("SSH_KEY_PREPARE_REJECTED", "SSH key fingerprint is invalid")
    if not isinstance(content_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None:
        raise PayloadValidationError(
            "SSH_KEY_PREPARE_REJECTED", "SSH key content digest is invalid"
        )
    if scope not in {"HOST", "CONTAINER", "BOTH"}:
        raise PayloadValidationError("SSH_KEY_PREPARE_REJECTED", "SSH key scope is invalid")
    username = payload.get("username")
    if username != "origin-pilot":
        raise PayloadValidationError(
            "SSH_KEY_PREPARE_REJECTED", "current Pilot may prepare keys only for origin-pilot"
        )
    return {
        "record_id": _canonical_uuid(payload.get("record_id"), "SSH key record ID"),
        "operation_id": _canonical_uuid(payload.get("operation_id"), "operation ID"),
        "managed_user_id": _canonical_uuid(payload.get("managed_user_id"), "managed user ID"),
        "username": username,
        "public_key": public_key,
        "key_type": key_type,
        "fingerprint_sha256": fingerprint,
        "content_sha256": content_sha256,
        "scope": scope,
    }


def _validate_ssh_key_discard(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {"record_id", "operation_id", "content_sha256"}:
        raise PayloadValidationError(
            "SSH_KEY_DISCARD_REJECTED", "SSH key discard fields are incomplete"
        )
    content_sha256 = payload.get("content_sha256")
    if not isinstance(content_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None:
        raise PayloadValidationError(
            "SSH_KEY_DISCARD_REJECTED", "SSH key content digest is invalid"
        )
    return {
        "record_id": _canonical_uuid(payload.get("record_id"), "SSH key record ID"),
        "operation_id": _canonical_uuid(payload.get("operation_id"), "operation ID"),
        "content_sha256": content_sha256,
    }


def _validate_container_start(payload: dict[str, Any]) -> dict[str, Any]:
    legacy_fields = {"name"}
    managed_fields = {
        "name",
        "username",
        "managed_user_id",
        "expected_compute_state",
        "expected_container_state",
        "expected_ssh_key_state",
    }
    if set(payload) == legacy_fields:
        name = payload.get("name")
        if not isinstance(name, str) or not SAFE_IDENTIFIER.fullmatch(name):
            raise PayloadValidationError("PAYLOAD_REJECTED", "invalid container name")
        return {"name": name}
    if set(payload) != managed_fields:
        raise PayloadValidationError(
            "CONTAINER_START_PAYLOAD_REJECTED", "managed container start fields are incomplete"
        )
    username = payload.get("username")
    name = payload.get("name")
    if (
        not isinstance(username, str)
        or not SAFE_USERNAME.fullmatch(username)
        or username in {"root", "origin-al", "codexops"}
        or name != f"gpu-dev-{username}"
    ):
        raise PayloadValidationError(
            "CONTAINER_OWNERSHIP_REJECTED", "container name is not bound to the managed user"
        )
    if (
        payload.get("expected_compute_state") != "ACTIVE"
        or payload.get("expected_container_state") != "STOPPED"
        or payload.get("expected_ssh_key_state") != "INSTALLED"
    ):
        raise PayloadValidationError(
            "CONTAINER_START_STATE_REJECTED", "container start requires ACTIVE/STOPPED/INSTALLED"
        )
    return {
        "name": name,
        "username": username,
        "managed_user_id": _canonical_uuid(payload.get("managed_user_id"), "managed user ID"),
        "expected_compute_state": "ACTIVE",
        "expected_container_state": "STOPPED",
        "expected_ssh_key_state": "INSTALLED",
    }


def validate_payload(
    operation_type: str, payload: dict[str, Any], *, allow_legacy_stage: bool = False
) -> dict[str, Any]:
    if operation_type == "containers.inspect":
        name = payload.get("name")
        if not isinstance(name, str) or not SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError("invalid container name")
        return {"name": name}
    if operation_type in {"job.cancel"}:
        job_id = payload.get("job_id")
        if not isinstance(job_id, int) or not 0 < job_id < 2**63:
            raise ValueError("invalid job id")
        return {"job_id": job_id}
    if operation_type == "user.stage":
        return _validate_user_stage(payload, allow_legacy_stage)
    if operation_type in {"user.activate", "user.activate.rollback"}:
        return _validate_user_activate(payload)
    if operation_type == "ssh_key.prepare":
        return _validate_ssh_key_prepare(payload)
    if operation_type == "ssh_key.discard":
        return _validate_ssh_key_discard(payload)
    if operation_type == "container.start":
        return _validate_container_start(payload)
    if operation_type.startswith("user."):
        username = payload.get("username")
        if not isinstance(username, str) or not SAFE_USERNAME.fullmatch(username):
            raise PayloadValidationError("PAYLOAD_REJECTED", "invalid username")
        if operation_type == "user.plan" and username != "origin-pilot":
            raise PayloadValidationError(
                "PAYLOAD_REJECTED", "Portal lifecycle planning is limited to origin-pilot"
            )
        if username in {"root", "origin-al", "codexops"}:
            raise PayloadValidationError("PAYLOAD_REJECTED", "protected username")
        return {"username": username}
    if operation_type == "quota.update":
        username = payload.get("username")
        quota_bytes = payload.get("quota_bytes")
        if not isinstance(username, str) or not SAFE_USERNAME.fullmatch(username):
            raise ValueError("invalid username")
        if username in {"root", "origin-al", "codexops"}:
            raise ValueError("protected username")
        if not isinstance(quota_bytes, int) or not 0 < quota_bytes <= 10 * 1024**4:
            raise ValueError("invalid quota")
        return {"username": username, "quota_bytes": quota_bytes}
    if operation_type.startswith("slurm."):
        node_name = payload.get("node_name", "sagsh100server")
        if node_name != "sagsh100server":
            raise ValueError("invalid node name")
        return {"node_name": node_name}
    if operation_type.startswith("container."):
        name = payload.get("name")
        if not isinstance(name, str) or not SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError("invalid container name")
        return {"name": name}
    if operation_type.startswith("ssh_key."):
        username = payload.get("username")
        if not isinstance(username, str) or not SAFE_USERNAME.fullmatch(username):
            raise ValueError("invalid username")
        if username in {"root", "origin-al", "codexops"}:
            raise ValueError("protected username")
        return {"username": username}
    return {}
