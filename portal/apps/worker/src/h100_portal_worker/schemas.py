import re
import uuid
from datetime import UTC, datetime
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
    "self.job.logs.read",
    "self.job.status.read",
    "self.storage.read",
    "compute.provision.retry_verify",
}
KNOWN_WRITES = {
    "compute.activate.self",
    "compute.activate.self.rollback",
    "compute.provision.plan",
    "compute.provision.dry_run",
    "compute.provision.stage",
    "user.plan",
    "user.stage",
    "user.activate",
    "user.activate.rollback",
    "user.ssh_client_validation.record",
    "user.pilot.acceptance",
    "user.suspend",
    "container.start",
    "container.stop",
    "container.restart",
    "container.rebuild",
    "slurm.drain",
    "slurm.resume",
    "slurm.production_pilot.start",
    "job.cancel",
    "quota.update",
    "ssh_key.add",
    "ssh_key.revoke",
    "ssh_key.prepare",
    "ssh_key.discard",
    "self.job.submit",
    "self.job.cancel",
    "lease.expire",
    "resource.recycle",
    "resource.restore",
    "host_access.revoke_managed_user",
}
KNOWN_STREAMS = {"self.container.terminal"}
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
COMPUTE_STAGE_HANDLER_IDENTITY = "h100-provision-stage"
COMPUTE_STAGE_HANDLER_PATH = "/usr/local/sbin/h100-provision-stage"
COMPUTE_STAGE_ARGV_CONTRACT_VERSION = "compute-provision-stage-argv-v1"
COMPUTE_STAGE_CONFIRMATION_VALIDATOR_VERSION = "compute-provision-stage-confirmation-validator-v1"
COMPUTE_STAGE_IMAGE_VALIDATOR_VERSION = "compute-provision-stage-local-image-v2"
COMPUTE_STAGE_CONTRACT_HASH = re.compile(r"^[0-9a-f]{64}$")
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
PORTAL3F_APPROVAL_REFERENCE = "portal3f-origin-pilot-first-acceptance-v1"
PORTAL3F_MANAGED_USER_ID = "3b95b4f0-95d9-444a-8f0b-46288195a807"
PORTAL3F_KEY_RECORD_ID = "7427da72-37b9-4ac2-8ada-2f0c83b7718e"
PORTAL3F_KEY_FINGERPRINT = "SHA256:nek6vyEb3GT+UJAcY5y/8PgY4achF2ouNy+8C2JqUVc"
PORTAL3F_IMAGE_REF = (
    "nvcr.io#nvidia/cuda:13.2.0-base-ubuntu24.04@"
    "sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a"
)
PORTAL3G_APPROVAL_REFERENCE = "portal3g-single-user-production-pilot-v1"
PORTAL3G_CLIENT_VALIDATION_OPERATION_ID = "a168cf96-c54d-4e68-b040-5516cae66518"
PORTAL3G_PILOT_ACCEPTANCE_OPERATION_ID = "94399c95-1a17-47a1-bd41-43a0c242ccc1"
APPROVED_CLIENT_VALIDATION_PAYLOAD: dict[str, Any] = {
    "managed_user_id": PORTAL3F_MANAGED_USER_ID,
    "username": "origin-pilot",
    "expected_compute_state": "ACTIVE",
    "expected_ssh_key_state": "INSTALLED",
    "key_record_id": PORTAL3F_KEY_RECORD_ID,
    "key_fingerprint": PORTAL3F_KEY_FINGERPRINT,
    "host_client_validation": "PASS",
    "container_client_validation": "PASS",
    "confirmation_source": "USER_CONFIRMED_REAL_CLIENT_CONNECTIONS",
    "approval_reference": PORTAL3F_APPROVAL_REFERENCE,
}
APPROVED_PILOT_ACCEPTANCE_PAYLOAD: dict[str, Any] = {
    "managed_user_id": PORTAL3F_MANAGED_USER_ID,
    "username": "origin-pilot",
    "node_name": "sagsh100server",
    "partition": "notebook",
    "account": "company",
    "qos": "general",
    "max_gpus": 1,
    "container_name": "gpu-dev-origin-pilot",
    "container_gpu": "NONE",
    "image_ref": PORTAL3F_IMAGE_REF,
    "expected_host_client_validation": "PASS",
    "expected_container_client_validation": "PASS",
    "final_node_state": "DRAIN",
    "approval_reference": PORTAL3F_APPROVAL_REFERENCE,
}
APPROVED_PRODUCTION_PILOT_PAYLOAD: dict[str, Any] = {
    "managed_user_id": PORTAL3F_MANAGED_USER_ID,
    "username": "origin-pilot",
    "node_name": "sagsh100server",
    "mode": "single-node",
    "managed_users": ["origin-pilot"],
    "max_gpus": 1,
    "client_validation_operation_id": PORTAL3G_CLIENT_VALIDATION_OPERATION_ID,
    "pilot_acceptance_operation_id": PORTAL3G_PILOT_ACCEPTANCE_OPERATION_ID,
    "expected_node_state": "DRAIN",
    "target_node_state": "IDLE",
    "approval_reference": PORTAL3G_APPROVAL_REFERENCE,
}
APPROVED_JOB_IMAGE = PORTAL3F_IMAGE_REF
STANDARD_COMPUTE_STORAGE_BYTES = 300 * 1024**3
STANDARD_COMPUTE_PROFILE = "STANDARD_8CPU_32GB"
STANDARD_COMPUTE_LEASE_SECONDS = 96 * 60 * 60
PILOT_UID_MIN = 20_000
PILOT_UID_MAX = 60_000
PROJECT_ID_MIN = 30_000
PROJECT_ID_MAX = 39_999
PILOT_SSH_PORT_MIN = 22_023
PILOT_SSH_PORT_MAX = 22_999


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
        if value not in KNOWN_READS | KNOWN_WRITES | KNOWN_STREAMS:
            raise ValueError("unknown operation type")
        return value


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise PayloadValidationError("PAYLOAD_REJECTED", f"invalid {field}")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise PayloadValidationError("PAYLOAD_REJECTED", f"invalid {field}") from exc


def _compute_request_identity(payload: dict[str, Any]) -> dict[str, str]:
    username = payload.get("username")
    if not isinstance(username, str) or not SAFE_USERNAME.fullmatch(username):
        raise PayloadValidationError("COMPUTE_PLAN_PAYLOAD_REJECTED", "invalid username")
    if username in {"root", "origin-al", "codexops"}:
        raise PayloadValidationError("COMPUTE_PLAN_PAYLOAD_REJECTED", "protected username")
    return {
        "request_id": _canonical_uuid(payload.get("request_id"), "compute request ID"),
        "portal_account_id": _canonical_uuid(payload.get("portal_account_id"), "Portal account ID"),
        "username": username,
    }


def _reserved_number_list(
    payload: dict[str, Any], field: str, *, minimum: int, maximum: int
) -> list[int]:
    raw = payload.get(field)
    if not isinstance(raw, list) or len(raw) > 4096:
        raise PayloadValidationError("COMPUTE_PLAN_PAYLOAD_REJECTED", f"invalid {field}")
    if any(not isinstance(item, int) or not minimum <= item <= maximum for item in raw):
        raise PayloadValidationError("COMPUTE_PLAN_PAYLOAD_REJECTED", f"invalid {field}")
    if len(raw) != len(set(raw)):
        raise PayloadValidationError("COMPUTE_PLAN_PAYLOAD_REJECTED", f"duplicate {field}")
    return sorted(raw)


def _validate_compute_provision_plan(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "request_id",
        "portal_account_id",
        "username",
        "requested_gpu_max",
        "requested_storage_bytes",
        "requested_container_profile",
        "requested_lease_seconds",
        "reserved_uids",
        "reserved_gids",
        "reserved_project_ids",
        "reserved_ssh_ports",
        "reserved_container_names",
    }
    if set(payload) != fields or set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError(
            "COMPUTE_PLAN_PAYLOAD_REJECTED", "compute provision plan fields are invalid"
        )
    result: dict[str, Any] = _compute_request_identity(payload)
    gpu_max = payload.get("requested_gpu_max")
    if gpu_max not in {0, 1} or isinstance(gpu_max, bool):
        raise PayloadValidationError("GPU_MAX_REJECTED", "GPU max must be zero or one")
    if (
        payload.get("requested_storage_bytes") != STANDARD_COMPUTE_STORAGE_BYTES
        or payload.get("requested_container_profile") != STANDARD_COMPUTE_PROFILE
        or payload.get("requested_lease_seconds") != STANDARD_COMPUTE_LEASE_SECONDS
    ):
        raise PayloadValidationError(
            "STANDARD_COMPUTE_PROFILE_REQUIRED", "compute request differs from standard profile"
        )
    result.update(
        {
            "requested_gpu_max": gpu_max,
            "requested_storage_bytes": STANDARD_COMPUTE_STORAGE_BYTES,
            "requested_container_profile": STANDARD_COMPUTE_PROFILE,
            "requested_lease_seconds": STANDARD_COMPUTE_LEASE_SECONDS,
            "reserved_uids": _reserved_number_list(
                payload, "reserved_uids", minimum=PILOT_UID_MIN, maximum=PILOT_UID_MAX
            ),
            "reserved_gids": _reserved_number_list(
                payload, "reserved_gids", minimum=PILOT_UID_MIN, maximum=PILOT_UID_MAX
            ),
            "reserved_project_ids": _reserved_number_list(
                payload,
                "reserved_project_ids",
                minimum=PROJECT_ID_MIN,
                maximum=PROJECT_ID_MAX,
            ),
            "reserved_ssh_ports": _reserved_number_list(
                payload,
                "reserved_ssh_ports",
                minimum=PILOT_SSH_PORT_MIN,
                maximum=PILOT_SSH_PORT_MAX,
            ),
        }
    )
    container_names = payload.get("reserved_container_names")
    if (
        not isinstance(container_names, list)
        or len(container_names) > 4096
        or any(
            not isinstance(item, str)
            or not SAFE_IDENTIFIER.fullmatch(item)
            or not item.startswith("gpu-dev-")
            for item in container_names
        )
        or len(container_names) != len(set(container_names))
    ):
        raise PayloadValidationError(
            "COMPUTE_PLAN_PAYLOAD_REJECTED", "invalid reserved container names"
        )
    result["reserved_container_names"] = sorted(container_names)
    return result


def _validate_compute_provision_dry_run(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "request_id",
        "plan_id",
        "portal_account_id",
        "username",
        "uid",
        "gid",
        "project_id",
        "ssh_port",
        "container_name",
        "storage_bytes",
        "container_profile",
        "container_cpus",
        "container_memory_gb",
        "container_pids_limit",
        "container_gpu",
        "slurm_account",
        "slurm_qos",
        "gpu_max",
        "lease_seconds",
        "lease_state",
        "host_ssh",
        "shell",
        "password_state",
        "execution_enabled",
    }
    if set(payload) != fields or set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError(
            "COMPUTE_DRY_RUN_PAYLOAD_REJECTED", "compute dry-run fields are invalid"
        )
    result: dict[str, Any] = _compute_request_identity(payload)
    result["plan_id"] = _canonical_uuid(payload.get("plan_id"), "provision plan ID")
    username = result["username"]
    expected = {
        "storage_bytes": STANDARD_COMPUTE_STORAGE_BYTES,
        "container_profile": STANDARD_COMPUTE_PROFILE,
        "container_cpus": 8,
        "container_memory_gb": 32,
        "container_pids_limit": 4096,
        "container_gpu": 0,
        "slurm_account": "company",
        "slurm_qos": "general",
        "lease_seconds": STANDARD_COMPUTE_LEASE_SECONDS,
        "lease_state": "NOT_STARTED",
        "host_ssh": "DISABLED",
        "shell": "/usr/sbin/nologin",
        "password_state": "LOCKED",
        "execution_enabled": False,
        "container_name": f"gpu-dev-{username}",
    }
    if any(payload.get(field) != value for field, value in expected.items()):
        raise PayloadValidationError(
            "COMPUTE_DRY_RUN_PLAN_MISMATCH", "reserved plan differs from fixed policy"
        )
    gpu_max = payload.get("gpu_max")
    if gpu_max not in {0, 1} or isinstance(gpu_max, bool):
        raise PayloadValidationError("GPU_MAX_REJECTED", "GPU max must be zero or one")
    numbers = {
        "uid": (PILOT_UID_MIN, PILOT_UID_MAX),
        "gid": (PILOT_UID_MIN, PILOT_UID_MAX),
        "project_id": (PROJECT_ID_MIN, PROJECT_ID_MAX),
        "ssh_port": (PILOT_SSH_PORT_MIN, PILOT_SSH_PORT_MAX),
    }
    for field, (minimum, maximum) in numbers.items():
        value = payload.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise PayloadValidationError("COMPUTE_DRY_RUN_PAYLOAD_REJECTED", f"invalid {field}")
        result[field] = value
    result.update(expected)
    result["gpu_max"] = gpu_max
    return result


def _validate_compute_provision_retry_verify(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "request_id",
        "plan_id",
        "stage_operation_id",
        "portal_account_id",
        "username",
        "uid",
        "gid",
        "project_id",
        "ssh_port",
        "container_name",
    }
    if set(payload) != fields or set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError(
            "COMPUTE_RETRY_VERIFY_PAYLOAD_REJECTED",
            "compute retry verification fields are invalid",
        )
    result: dict[str, Any] = _compute_request_identity(payload)
    result["plan_id"] = _canonical_uuid(payload.get("plan_id"), "provision plan ID")
    result["stage_operation_id"] = _canonical_uuid(
        payload.get("stage_operation_id"), "failed Stage operation ID"
    )
    if payload.get("container_name") != f"gpu-dev-{result['username']}":
        raise PayloadValidationError(
            "COMPUTE_RETRY_VERIFY_PAYLOAD_REJECTED", "container name is not target-bound"
        )
    numbers = {
        "uid": (PILOT_UID_MIN, PILOT_UID_MAX),
        "gid": (PILOT_UID_MIN, PILOT_UID_MAX),
        "project_id": (PROJECT_ID_MIN, PROJECT_ID_MAX),
        "ssh_port": (PILOT_SSH_PORT_MIN, PILOT_SSH_PORT_MAX),
    }
    for field, (minimum, maximum) in numbers.items():
        value = payload.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise PayloadValidationError(
                "COMPUTE_RETRY_VERIFY_PAYLOAD_REJECTED", f"invalid {field}"
            )
        result[field] = value
    result["container_name"] = payload["container_name"]
    return result


def _validate_compute_stage_contract(value: Any) -> dict[str, Any]:
    """Accept only the non-secret, successful contract evidence emitted by dry-run."""
    if not isinstance(value, dict) or set(value) != {
        "status",
        "contract_sha256",
        "handler",
        "argv_contract",
        "confirmation_gate",
        "image_contract",
    }:
        raise PayloadValidationError(
            "DRY_RUN_STAGE_CONTRACT_INCOMPLETE",
            "successful structured dry-run Stage contract evidence is required",
        )
    handler = value.get("handler")
    argv_contract = value.get("argv_contract")
    confirmation_gate = value.get("confirmation_gate")
    image_contract = value.get("image_contract")
    if (
        value.get("status") != "PASS"
        or not isinstance(value.get("contract_sha256"), str)
        or not COMPUTE_STAGE_CONTRACT_HASH.fullmatch(value["contract_sha256"])
        or not isinstance(handler, dict)
        or set(handler) != {"identity", "deployed_path", "sha256", "integrity_status"}
        or handler.get("identity") != COMPUTE_STAGE_HANDLER_IDENTITY
        or handler.get("deployed_path") != COMPUTE_STAGE_HANDLER_PATH
        or not isinstance(handler.get("sha256"), str)
        or not COMPUTE_STAGE_CONTRACT_HASH.fullmatch(handler["sha256"])
        or handler.get("integrity_status") != "PASS"
        or not isinstance(argv_contract, dict)
        or set(argv_contract)
        != {
            "version",
            "sha256",
            "shape_status",
            "shell_argument_count",
            "expected_shell_argument_count",
            "multi_digit_position_status",
            "argument_13",
            "argument_14",
        }
        or argv_contract.get("version") != COMPUTE_STAGE_ARGV_CONTRACT_VERSION
        or not isinstance(argv_contract.get("sha256"), str)
        or not COMPUTE_STAGE_CONTRACT_HASH.fullmatch(argv_contract["sha256"])
        or argv_contract.get("shape_status") != "PASS"
        or argv_contract.get("shell_argument_count") != 14
        or argv_contract.get("expected_shell_argument_count") != 14
        or argv_contract.get("multi_digit_position_status") != "PASS"
        or not isinstance(confirmation_gate, dict)
        or set(confirmation_gate) != {"identity", "validator_version", "validator_sha256", "status"}
        or confirmation_gate.get("identity") != "EXPLICIT_STAGE_CONFIRMATION_GATE"
        or confirmation_gate.get("validator_version")
        != COMPUTE_STAGE_CONFIRMATION_VALIDATOR_VERSION
        or not isinstance(confirmation_gate.get("validator_sha256"), str)
        or not COMPUTE_STAGE_CONTRACT_HASH.fullmatch(confirmation_gate["validator_sha256"])
        or confirmation_gate.get("status") != "PASS"
        or not isinstance(image_contract, dict)
        or set(image_contract)
        != {
            "status",
            "validator_version",
            "source_type",
            "canonical_local_image_identity",
            "artifact_path",
            "manifest_digest",
            "platform",
            "effective_user",
            "source_reference",
            "source_build_version",
            "approved_deployment_version",
            "failure_code",
        }
        or image_contract.get("status") != "PASS"
        or image_contract.get("validator_version") != COMPUTE_STAGE_IMAGE_VALIDATOR_VERSION
        or image_contract.get("source_type") != "LOCAL_OCI_LAYOUT"
        or not isinstance(image_contract.get("canonical_local_image_identity"), str)
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", image_contract["canonical_local_image_identity"]
        )
        or not isinstance(image_contract.get("artifact_path"), str)
        or not re.fullmatch(
            r"/srv/gpu-platform/artifacts/oci/standard-dev-base/[0-9a-f]{64}/layout",
            image_contract["artifact_path"],
        )
        or not isinstance(image_contract.get("manifest_digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_contract["manifest_digest"])
        or not image_contract["artifact_path"].endswith(
            f"/{image_contract['manifest_digest'].removeprefix('sha256:')}/layout"
        )
        or image_contract.get("platform") != "linux/amd64"
        or image_contract.get("effective_user") != "root"
        or image_contract.get("source_reference")
        != "h100-local/dev-container:ubuntu24.04-origin-pilot-20260804"
        or image_contract.get("source_build_version") != "ubuntu24.04-origin-pilot-20260804"
        or not isinstance(image_contract.get("approved_deployment_version"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", image_contract["approved_deployment_version"])
        or image_contract.get("failure_code") is not None
    ):
        raise PayloadValidationError(
            "DRY_RUN_STAGE_CONTRACT_INCOMPLETE",
            "dry-run Stage contract identity or validation evidence is incomplete",
        )
    arguments = (
        (
            argv_contract.get("argument_13"),
            13,
            "EXPLICIT_STAGE_CONFIRMATION_FLAG",
        ),
        (
            argv_contract.get("argument_14"),
            14,
            "CONFIRMED_TARGET_USERNAME",
        ),
    )
    for argument, index, role in arguments:
        if (
            not isinstance(argument, dict)
            or set(argument) != {"index", "semantic_role", "binding_status"}
            or argument.get("index") != index
            or argument.get("semantic_role") != role
            or argument.get("binding_status") != "VALID"
        ):
            raise PayloadValidationError(
                "DRY_RUN_STAGE_CONTRACT_INCOMPLETE",
                f"dry-run argument {index} binding evidence is incomplete",
            )
    return {
        "status": "PASS",
        "contract_sha256": value["contract_sha256"],
        "handler": dict(handler),
        "argv_contract": {
            **argv_contract,
            "argument_13": dict(argv_contract["argument_13"]),
            "argument_14": dict(argv_contract["argument_14"]),
        },
        "confirmation_gate": dict(confirmation_gate),
        "image_contract": dict(image_contract),
    }


def _validate_compute_provision_stage(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the closed Stage contract produced from a reserved plan.

    Reservation IDs are carried all the way to the Worker even though the
    host script only consumes the allocator values.  That prevents a future
    API caller from accidentally reducing Stage to an unbound user-create.
    """
    fields = {
        "request_id",
        "plan_id",
        "portal_account_id",
        "stage_operation_id",
        "dry_run_operation_id",
        "dry_run_stage_contract",
        "reservation_ids",
        "username",
        "uid",
        "gid",
        "project_id",
        "ssh_port",
        "container_name",
        "storage_bytes",
        "container_profile",
        "container_cpus",
        "container_memory_gb",
        "container_pids_limit",
        "container_gpu",
        "slurm_account",
        "slurm_qos",
        "gpu_max",
        "lease_seconds",
        "lease_state",
        "host_ssh",
        "shell",
        "password_state",
        "execution_enabled",
    }
    if "dry_run_stage_contract" not in payload:
        raise PayloadValidationError(
            "DRY_RUN_STAGE_CONTRACT_INCOMPLETE",
            "Stage requires structured contract evidence from its dry-run",
        )
    if set(payload) != fields or set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError(
            "COMPUTE_STAGE_PAYLOAD_REJECTED", "compute Stage fields are invalid"
        )
    dry_run_payload = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "stage_operation_id",
            "dry_run_operation_id",
            "dry_run_stage_contract",
            "reservation_ids",
        }
    }
    dry_run_payload["execution_enabled"] = False
    result = _validate_compute_provision_dry_run(dry_run_payload)
    if payload.get("execution_enabled") is not True:
        raise PayloadValidationError(
            "COMPUTE_STAGE_EXECUTION_GATE_REJECTED", "compute Stage execution is not enabled"
        )
    reservation_ids = payload.get("reservation_ids")
    if not isinstance(reservation_ids, dict) or set(reservation_ids) != {
        "UID",
        "GID",
        "PROJECT_ID",
        "SSH_PORT",
        "CONTAINER_NAME",
    }:
        raise PayloadValidationError(
            "COMPUTE_STAGE_RESERVATION_REJECTED", "Stage reservation IDs are incomplete"
        )
    result.update(
        {
            "stage_operation_id": _canonical_uuid(
                payload.get("stage_operation_id"), "Stage operation ID"
            ),
            "dry_run_operation_id": _canonical_uuid(
                payload.get("dry_run_operation_id"), "dry-run operation ID"
            ),
            "dry_run_stage_contract": _validate_compute_stage_contract(
                payload.get("dry_run_stage_contract")
            ),
            "reservation_ids": {
                resource_type: _canonical_uuid(value, f"{resource_type} reservation ID")
                for resource_type, value in sorted(reservation_ids.items())
            },
            "execution_enabled": True,
        }
    )
    return result


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


def _validate_compute_activate_self(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "activation_operation_id",
        "managed_user_id",
        "portal_account_id",
        "owner_login",
        "request_id",
        "plan_id",
        "stage_operation_id",
        "dry_run_operation_id",
        "username",
        "uid",
        "gid",
        "project_id",
        "ssh_port",
        "container_name",
        "slurm_account",
        "slurm_qos",
        "ssh_key_record_ids",
        "ssh_key_fingerprints",
        "gpu_max",
        "lease_seconds",
        "expected_compute_state",
        "expected_container_state",
        "expected_host_access",
        "expected_shell",
        "expected_password_state",
        "expected_gpu",
        "deployment_version",
    }
    if set(payload) != fields or set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError(
            "ACTIVATION_PAYLOAD_REJECTED",
            "owner-bound activation fields are incomplete",
        )
    username = payload.get("username")
    owner_login = payload.get("owner_login")
    if (
        not isinstance(username, str)
        or SAFE_USERNAME.fullmatch(username) is None
        or username in {"root", "origin-al", "codexops", "nobody"}
        or not isinstance(owner_login, str)
        or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", owner_login) is None
    ):
        raise PayloadValidationError(
            "ACTIVATION_OWNER_REJECTED",
            "activation owner identity is invalid",
        )
    uid = payload.get("uid")
    gid = payload.get("gid")
    project_id = payload.get("project_id")
    ssh_port = payload.get("ssh_port")
    if (
        not isinstance(uid, int)
        or isinstance(uid, bool)
        or not PILOT_UID_MIN <= uid <= PILOT_UID_MAX
        or not isinstance(gid, int)
        or isinstance(gid, bool)
        or not PILOT_UID_MIN <= gid <= PILOT_UID_MAX
        or not isinstance(project_id, int)
        or isinstance(project_id, bool)
        or not PROJECT_ID_MIN <= project_id <= PROJECT_ID_MAX
        or not isinstance(ssh_port, int)
        or isinstance(ssh_port, bool)
        or not PILOT_SSH_PORT_MIN <= ssh_port <= PILOT_SSH_PORT_MAX
    ):
        raise PayloadValidationError(
            "ACTIVATION_RESOURCE_BINDING_REJECTED",
            "activation resource coordinates are invalid",
        )
    if (
        payload.get("container_name") != f"gpu-dev-{username}"
        or payload.get("slurm_account") != "company"
        or payload.get("slurm_qos") != "general"
        or payload.get("gpu_max") not in {0, 1}
        or payload.get("lease_seconds") != STANDARD_COMPUTE_LEASE_SECONDS
        or payload.get("expected_compute_state") != "STAGED"
        or payload.get("expected_container_state") != "STOPPED"
        or payload.get("expected_host_access") != "DISABLED_BY_PLATFORM_POLICY"
        or payload.get("expected_shell") != "/usr/sbin/nologin"
        or payload.get("expected_password_state") != "LOCKED"
        or payload.get("expected_gpu") != "NONE"
    ):
        raise PayloadValidationError(
            "ACTIVATION_SECURITY_CONTRACT_REJECTED",
            "activation security contract differs from the fixed policy",
        )
    record_ids = payload.get("ssh_key_record_ids")
    fingerprints = payload.get("ssh_key_fingerprints")
    if (
        not isinstance(record_ids, list)
        or not 1 <= len(record_ids) <= 5
        or not isinstance(fingerprints, list)
        or len(fingerprints) != len(record_ids)
        or any(
            not isinstance(item, str) or re.fullmatch(r"SHA256:[A-Za-z0-9+/]+", item) is None
            for item in fingerprints
        )
    ):
        raise PayloadValidationError(
            "ACTIVATION_KEY_BINDING_REJECTED",
            "activation SSH key bindings are invalid",
        )
    canonical_record_ids = [_canonical_uuid(item, "SSH key record ID") for item in record_ids]
    if len(set(canonical_record_ids)) != len(canonical_record_ids) or len(set(fingerprints)) != len(
        fingerprints
    ):
        raise PayloadValidationError(
            "ACTIVATION_KEY_BINDING_REJECTED",
            "activation SSH key bindings contain duplicates",
        )
    version = payload.get("deployment_version")
    if not isinstance(version, str) or (
        version != "SOURCE_WORKTREE" and re.fullmatch(r"[0-9a-f]{40}", version) is None
    ):
        raise PayloadValidationError(
            "ACTIVATION_RUNTIME_BINDING_REJECTED",
            "activation deployment version is invalid",
        )
    return {
        "activation_operation_id": _canonical_uuid(
            payload.get("activation_operation_id"), "activation operation ID"
        ),
        "managed_user_id": _canonical_uuid(payload.get("managed_user_id"), "managed user ID"),
        "portal_account_id": _canonical_uuid(payload.get("portal_account_id"), "Portal account ID"),
        "owner_login": owner_login,
        "request_id": _canonical_uuid(payload.get("request_id"), "compute request ID"),
        "plan_id": _canonical_uuid(payload.get("plan_id"), "provision plan ID"),
        "stage_operation_id": _canonical_uuid(
            payload.get("stage_operation_id"), "Stage operation ID"
        ),
        "dry_run_operation_id": _canonical_uuid(
            payload.get("dry_run_operation_id"), "dry-run operation ID"
        ),
        "username": username,
        "uid": uid,
        "gid": gid,
        "project_id": project_id,
        "ssh_port": ssh_port,
        "container_name": f"gpu-dev-{username}",
        "slurm_account": "company",
        "slurm_qos": "general",
        "ssh_key_record_ids": canonical_record_ids,
        "ssh_key_fingerprints": list(fingerprints),
        "gpu_max": int(payload["gpu_max"]),
        "lease_seconds": STANDARD_COMPUTE_LEASE_SECONDS,
        "expected_compute_state": "STAGED",
        "expected_container_state": "STOPPED",
        "expected_host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_shell": "/usr/sbin/nologin",
        "expected_password_state": "LOCKED",
        "expected_gpu": "NONE",
        "deployment_version": version,
    }


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
    username = payload.get("username")
    if (
        not isinstance(username, str)
        or not SAFE_USERNAME.fullmatch(username)
        or username in {"root", "origin-al", "codexops", "nobody"}
    ):
        raise PayloadValidationError(
            "SSH_KEY_PREPARE_REJECTED", "SSH key target username is invalid or protected"
        )
    if scope not in {"HOST", "CONTAINER", "BOTH"} or (
        username != "origin-pilot" and scope != "CONTAINER"
    ):
        raise PayloadValidationError(
            "SSH_KEY_PREPARE_REJECTED", "managed ordinary users require CONTAINER-only keys"
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


def _validate_portal3f_payload(operation_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError(
            "PORTAL3F_PAYLOAD_REJECTED", "secret, command, argv, and path fields are forbidden"
        )
    expected = (
        APPROVED_CLIENT_VALIDATION_PAYLOAD
        if operation_type == "user.ssh_client_validation.record"
        else APPROVED_PILOT_ACCEPTANCE_PAYLOAD
    )
    if payload != expected:
        raise PayloadValidationError(
            "PORTAL3F_PLAN_MISMATCH", "Portal-3F payload differs from the fixed approved plan"
        )
    return dict(expected)


def _validate_portal3g_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError(
            "PORTAL3G_PAYLOAD_REJECTED", "secret, command, argv, and path fields are forbidden"
        )
    if payload != APPROVED_PRODUCTION_PILOT_PAYLOAD:
        raise PayloadValidationError(
            "PORTAL3G_PLAN_MISMATCH",
            "Production Pilot payload differs from the fixed approved plan",
        )
    return dict(APPROVED_PRODUCTION_PILOT_PAYLOAD)


def _portal4a_identity(payload: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "managed_user_id": PORTAL3F_MANAGED_USER_ID,
        "username": "origin-pilot",
        "uid": 20001,
        "gid": 20001,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise PayloadValidationError(
                "RESOURCE_OWNERSHIP_REJECTED", "Portal-4A-R identity binding is invalid"
            )
    return expected


def _relative_user_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or "\x00" in value:
        raise PayloadValidationError("ARBITRARY_PATH_REJECTED", f"invalid {field}")
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        raise PayloadValidationError("ARBITRARY_PATH_REJECTED", f"invalid {field}")
    return value


def _future_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise PayloadValidationError("PAYLOAD_REJECTED", f"invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadValidationError("PAYLOAD_REJECTED", f"invalid {field}") from exc
    if parsed.tzinfo is None or parsed.astimezone(UTC) <= datetime.now(UTC):
        raise PayloadValidationError("LEASE_INACTIVE", "lease deadline is not in the future")
    return parsed.astimezone(UTC).isoformat()


def _validate_self_job_submit(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "portal_job_id",
        "managed_user_id",
        "lease_id",
        "username",
        "uid",
        "gid",
        "name",
        "script_relative_path",
        "workdir_relative_path",
        "stdout_relative_path",
        "stderr_relative_path",
        "cpus",
        "memory_mb",
        "gpu_count",
        "time_limit_seconds",
        "lease_deadline_at",
        "image_ref",
    }
    if set(payload) != fields:
        raise PayloadValidationError("JOB_SPEC_REJECTED", "job specification fields are incomplete")
    result = _portal4a_identity(payload)
    portal_job_id = _canonical_uuid(payload.get("portal_job_id"), "Portal job ID")
    result.update(
        {
            "portal_job_id": portal_job_id,
            "lease_id": _canonical_uuid(payload.get("lease_id"), "lease ID"),
        }
    )
    name = payload.get("name")
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) is None:
        raise PayloadValidationError("JOB_SPEC_REJECTED", "job name is invalid")
    cpus = payload.get("cpus")
    memory_mb = payload.get("memory_mb")
    gpu_count = payload.get("gpu_count")
    time_limit = payload.get("time_limit_seconds")
    if not isinstance(cpus, int) or not 1 <= cpus <= 8:
        raise PayloadValidationError("JOB_SPEC_REJECTED", "CPU request is invalid")
    if not isinstance(memory_mb, int) or not 256 <= memory_mb <= 32768:
        raise PayloadValidationError("JOB_SPEC_REJECTED", "memory request is invalid")
    if gpu_count not in {0, 1}:
        raise PayloadValidationError("GPU_LIMIT_EXCEEDED", "GPU request must be 0 or 1")
    if not isinstance(time_limit, int) or not 60 <= time_limit <= 345600:
        raise PayloadValidationError("JOB_SPEC_REJECTED", "time limit is invalid")
    deadline = _future_timestamp(payload.get("lease_deadline_at"), "lease deadline")
    remaining = (
        datetime.fromisoformat(deadline).astimezone(UTC) - datetime.now(UTC)
    ).total_seconds()
    if time_limit > remaining:
        raise PayloadValidationError("JOB_EXCEEDS_LEASE", "job time limit exceeds lease deadline")
    image_ref = payload.get("image_ref")
    if image_ref not in {None, APPROVED_JOB_IMAGE}:
        raise PayloadValidationError("IMAGE_NOT_APPROVED", "container image is not approved")
    stdout_path = _relative_user_path(payload.get("stdout_relative_path"), "stdout path")
    stderr_path = _relative_user_path(payload.get("stderr_relative_path"), "stderr path")
    if (
        stdout_path != f"workspace/.portal/jobs/{portal_job_id}.out"
        or stderr_path != f"workspace/.portal/jobs/{portal_job_id}.err"
    ):
        raise PayloadValidationError("JOB_OUTPUT_REJECTED", "job output path is not fixed")
    result.update(
        {
            "name": name,
            "script_relative_path": _relative_user_path(
                payload.get("script_relative_path"), "script path"
            ),
            "workdir_relative_path": _relative_user_path(
                payload.get("workdir_relative_path"), "workdir"
            ),
            "stdout_relative_path": stdout_path,
            "stderr_relative_path": stderr_path,
            "cpus": cpus,
            "memory_mb": memory_mb,
            "gpu_count": gpu_count,
            "time_limit_seconds": time_limit,
            "lease_deadline_at": deadline,
            "image_ref": image_ref,
        }
    )
    return result


def _validate_self_job_target(payload: dict[str, Any], *, logs: bool = False) -> dict[str, Any]:
    expected = (
        {
            "portal_job_id",
            "managed_user_id",
            "username",
            "uid",
            "gid",
            "stdout_relative_path",
            "stderr_relative_path",
        }
        if logs
        else {
            "portal_job_id",
            "managed_user_id",
            "username",
            "uid",
            "gid",
            "slurm_job_id",
        }
    )
    if set(payload) != expected:
        raise PayloadValidationError("JOB_TARGET_REJECTED", "job target fields are invalid")
    result = _portal4a_identity(payload)
    portal_job_id = _canonical_uuid(payload.get("portal_job_id"), "Portal job ID")
    result["portal_job_id"] = portal_job_id
    if logs:
        stdout_path = _relative_user_path(payload.get("stdout_relative_path"), "stdout path")
        stderr_path = _relative_user_path(payload.get("stderr_relative_path"), "stderr path")
        if (
            stdout_path != f"workspace/.portal/jobs/{portal_job_id}.out"
            or stderr_path != f"workspace/.portal/jobs/{portal_job_id}.err"
        ):
            raise PayloadValidationError("JOB_OUTPUT_REJECTED", "job output path is not fixed")
        result["stdout_relative_path"] = stdout_path
        result["stderr_relative_path"] = stderr_path
    else:
        job_id = payload.get("slurm_job_id")
        if not isinstance(job_id, int) or not 0 < job_id < 2**63:
            raise PayloadValidationError("JOB_TARGET_REJECTED", "Slurm job ID is invalid")
        result["slurm_job_id"] = job_id
    return result


def _validate_container_lifecycle(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "managed_user_id",
        "username",
        "uid",
        "gid",
        "name",
        "lease_id",
        "lease_expires_at",
        "expected_gpu",
    }
    if set(payload) != fields:
        raise PayloadValidationError("CONTAINER_TARGET_REJECTED", "container fields are invalid")
    result = _portal4a_identity(payload)
    if payload.get("name") != "gpu-dev-origin-pilot" or payload.get("expected_gpu") != "NONE":
        raise PayloadValidationError("CONTAINER_TARGET_REJECTED", "container target is invalid")
    result["name"] = "gpu-dev-origin-pilot"
    result["expected_gpu"] = "NONE"
    if payload.get("lease_id") is None and payload.get("lease_expires_at") is None:
        result["lease_id"] = None
        result["lease_expires_at"] = None
    else:
        result["lease_id"] = _canonical_uuid(payload.get("lease_id"), "lease ID")
        result["lease_expires_at"] = _future_timestamp(
            payload.get("lease_expires_at"), "lease expiry"
        )
    return result


def _validate_container_terminal(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "managed_user_id",
        "username",
        "uid",
        "gid",
        "name",
        "lease_id",
        "lease_expires_at",
        "expected_gpu",
        "host_access",
        "expected_key_fingerprints",
        "cols",
        "rows",
    }
    if set(payload) != fields or set(payload) & FORBIDDEN_SECRET_OR_COMMAND_FIELDS:
        raise PayloadValidationError(
            "TERMINAL_TARGET_REJECTED", "terminal target fields are invalid"
        )
    result = _portal4a_identity(payload)
    if (
        payload.get("name") != "gpu-dev-origin-pilot"
        or payload.get("expected_gpu") != "NONE"
        or payload.get("host_access") != "DISABLED_BY_PLATFORM_POLICY"
    ):
        raise PayloadValidationError(
            "TERMINAL_TARGET_REJECTED", "terminal target is not the owned development container"
        )
    cols = payload.get("cols")
    rows = payload.get("rows")
    if not isinstance(cols, int) or not 20 <= cols <= 300:
        raise PayloadValidationError("TERMINAL_SIZE_REJECTED", "terminal columns are invalid")
    if not isinstance(rows, int) or not 5 <= rows <= 120:
        raise PayloadValidationError("TERMINAL_SIZE_REJECTED", "terminal rows are invalid")
    result.update(
        {
            "name": "gpu-dev-origin-pilot",
            "lease_id": _canonical_uuid(payload.get("lease_id"), "lease ID"),
            "lease_expires_at": _future_timestamp(payload.get("lease_expires_at"), "lease expiry"),
            "expected_gpu": "NONE",
            "host_access": "DISABLED_BY_PLATFORM_POLICY",
            "expected_key_fingerprints": _validated_key_fingerprints(
                payload.get("expected_key_fingerprints")
            ),
            "cols": cols,
            "rows": rows,
        }
    )
    return result


def _validated_key_fingerprints(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 5
        or any(
            not isinstance(item, str) or re.fullmatch(r"SHA256:[A-Za-z0-9+/]+", item) is None
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise PayloadValidationError(
            "CONTAINER_KEY_BINDING_REJECTED", "approved SSH key fingerprints are invalid"
        )
    return sorted(value)


def _validate_restore(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "restore_request_id",
        "managed_user_id",
        "username",
        "uid",
        "gid",
        "container_name",
        "expected_gpu",
        "host_access",
        "expected_key_fingerprints",
    }
    if set(payload) != fields:
        raise PayloadValidationError("RESTORE_PAYLOAD_REJECTED", "restore fields are invalid")
    result = _portal4a_identity(payload)
    if (
        payload.get("container_name") != "gpu-dev-origin-pilot"
        or payload.get("expected_gpu") != "NONE"
        or payload.get("host_access") != "DISABLED_BY_PLATFORM_POLICY"
    ):
        raise PayloadValidationError("RESTORE_PAYLOAD_REJECTED", "restore target is invalid")
    result.update(
        {
            "restore_request_id": _canonical_uuid(
                payload.get("restore_request_id"), "restore request ID"
            ),
            "container_name": "gpu-dev-origin-pilot",
            "expected_gpu": "NONE",
            "host_access": "DISABLED_BY_PLATFORM_POLICY",
            "expected_key_fingerprints": _validated_key_fingerprints(
                payload.get("expected_key_fingerprints")
            ),
        }
    )
    return result


def _validate_recycle(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "lease_id",
        "managed_user_id",
        "username",
        "uid",
        "gid",
        "container_name",
        "expires_at",
        "expected_gpu",
        "host_access",
        "expected_key_fingerprints",
    }
    if set(payload) != fields:
        raise PayloadValidationError("RECYCLE_PAYLOAD_REJECTED", "recycle fields are invalid")
    result = _portal4a_identity(payload)
    if (
        payload.get("container_name") != "gpu-dev-origin-pilot"
        or payload.get("expected_gpu") != "NONE"
        or payload.get("host_access") != "DISABLED_BY_PLATFORM_POLICY"
    ):
        raise PayloadValidationError("RECYCLE_PAYLOAD_REJECTED", "recycle target is invalid")
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, str) or len(expires_at) > 64:
        raise PayloadValidationError("RECYCLE_PAYLOAD_REJECTED", "lease expiry is invalid")
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadValidationError("RECYCLE_PAYLOAD_REJECTED", "lease expiry is invalid") from exc
    if parsed.tzinfo is None or parsed.astimezone(UTC) > datetime.now(UTC):
        raise PayloadValidationError("LEASE_NOT_EXPIRED", "lease has not expired")
    result.update(
        {
            "lease_id": _canonical_uuid(payload.get("lease_id"), "lease ID"),
            "container_name": "gpu-dev-origin-pilot",
            "expires_at": parsed.astimezone(UTC).isoformat(),
            "expected_gpu": "NONE",
            "host_access": "DISABLED_BY_PLATFORM_POLICY",
            "expected_key_fingerprints": _validated_key_fingerprints(
                payload.get("expected_key_fingerprints")
            ),
        }
    )
    return result


def _validate_host_revoke(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "managed_user_id",
        "username",
        "uid",
        "gid",
        "key_record_id",
        "key_fingerprint",
        "container_name",
        "expected_scope",
    }
    if set(payload) != fields:
        raise PayloadValidationError(
            "HOST_REVOKE_PAYLOAD_REJECTED", "host revoke fields are invalid"
        )
    result = _portal4a_identity(payload)
    fingerprint = payload.get("key_fingerprint")
    if (
        payload.get("container_name") != "gpu-dev-origin-pilot"
        or payload.get("expected_scope") != "BOTH"
        or fingerprint != PORTAL3F_KEY_FINGERPRINT
    ):
        raise PayloadValidationError(
            "HOST_REVOKE_PAYLOAD_REJECTED", "host revoke target is invalid"
        )
    result.update(
        {
            "key_record_id": _canonical_uuid(payload.get("key_record_id"), "SSH key record ID"),
            "key_fingerprint": fingerprint,
            "container_name": "gpu-dev-origin-pilot",
            "expected_scope": "BOTH",
        }
    )
    return result


def validate_payload(
    operation_type: str, payload: dict[str, Any], *, allow_legacy_stage: bool = False
) -> dict[str, Any]:
    if operation_type == "compute.provision.plan":
        return _validate_compute_provision_plan(payload)
    if operation_type == "compute.provision.dry_run":
        return _validate_compute_provision_dry_run(payload)
    if operation_type == "compute.provision.retry_verify":
        return _validate_compute_provision_retry_verify(payload)
    if operation_type == "compute.provision.stage":
        return _validate_compute_provision_stage(payload)
    if operation_type in {"compute.activate.self", "compute.activate.self.rollback"}:
        return _validate_compute_activate_self(payload)
    if operation_type == "containers.inspect":
        name = payload.get("name")
        if not isinstance(name, str) or not SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError("invalid container name")
        return {"name": name}
    if operation_type == "self.job.submit":
        return _validate_self_job_submit(payload)
    if operation_type == "self.job.cancel":
        return _validate_self_job_target(payload)
    if operation_type == "self.job.logs.read":
        return _validate_self_job_target(payload, logs=True)
    if operation_type == "self.job.status.read":
        return _validate_self_job_target(payload)
    if operation_type == "self.storage.read":
        if set(payload) != {"managed_user_id", "username", "uid", "gid"}:
            raise PayloadValidationError(
                "STORAGE_TARGET_REJECTED", "storage target fields are invalid"
            )
        return _portal4a_identity(payload)
    if operation_type == "self.container.terminal":
        return _validate_container_terminal(payload)
    if (
        operation_type in {"container.start", "container.stop", "container.restart"}
        and "uid" in payload
    ):
        return _validate_container_lifecycle(payload)
    if operation_type == "resource.restore":
        return _validate_restore(payload)
    if operation_type in {"lease.expire", "resource.recycle"}:
        return _validate_recycle(payload)
    if operation_type == "host_access.revoke_managed_user":
        return _validate_host_revoke(payload)
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
    if operation_type in {"user.ssh_client_validation.record", "user.pilot.acceptance"}:
        return _validate_portal3f_payload(operation_type, payload)
    if operation_type == "slurm.production_pilot.start":
        return _validate_portal3g_payload(payload)
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
