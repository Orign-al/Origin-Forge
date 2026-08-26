import base64
import binascii
import csv
import errno
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import struct
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from h100_portal_contracts.workspace import (
    CPU_DEVELOPMENT_PROFILE,
    GPU_DEVELOPMENT_PROFILE,
    WORKSPACE_QUOTA_BYTES,
    WORKSPACE_REQUIRED_DIRECTORIES,
    WorkspaceBinding,
    workspace_binding,
    workspace_path,
)

from h100_portal_worker.local_image import DEPLOYMENT_VERSION_PATH, local_image_contract
from h100_portal_worker.schemas import (
    APPROVED_CLIENT_VALIDATION_PAYLOAD,
    APPROVED_PILOT_ACCEPTANCE_PAYLOAD,
    APPROVED_PRODUCTION_PILOT_PAYLOAD,
    APPROVED_STAGE_PAYLOAD,
    COMPUTE_STAGE_ARGV_CONTRACT_VERSION,
    COMPUTE_STAGE_CONFIRMATION_VALIDATOR_VERSION,
    COMPUTE_STAGE_HANDLER_IDENTITY,
    COMPUTE_STAGE_HANDLER_PATH,
    COMPUTE_STAGE_IMAGE_VALIDATOR_VERSION,
    KNOWN_WRITES,
    STANDARD_COMPUTE_LEASE_SECONDS,
    STANDARD_COMPUTE_PROFILE,
    STANDARD_COMPUTE_STORAGE_BYTES,
    WorkerRequest,
    validate_payload,
)

MAX_OUTPUT = 256 * 1024
FIXED_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "LANG": "C",
}
BINARIES = {
    "nvidia-smi": "/usr/bin/nvidia-smi",
    "dcgmi": "/usr/bin/dcgmi",
    "scontrol": "/usr/bin/scontrol",
    "sinfo": "/usr/bin/sinfo",
    "squeue": "/usr/bin/squeue",
    "sacct": "/usr/bin/sacct",
    "sacctmgr": "/usr/bin/sacctmgr",
    "docker": "/usr/bin/docker",
    "systemctl": "/usr/bin/systemctl",
    "journalctl": "/usr/bin/journalctl",
    "df": "/usr/bin/df",
    "vgs": "/usr/sbin/vgs",
    "xfs_quota": "/usr/sbin/xfs_quota",
    "du": "/usr/bin/du",
    "find": "/usr/bin/find",
    "findmnt": "/usr/bin/findmnt",
    "hostname": "/usr/bin/hostname",
    "ss": "/usr/bin/ss",
    "ssh-keygen": "/usr/bin/ssh-keygen",
    "passwd": "/usr/bin/passwd",
    "sshd": "/usr/sbin/sshd",
    "sbatch": "/usr/bin/sbatch",
    "scancel": "/usr/bin/scancel",
    "setpriv": "/usr/bin/setpriv",
    "usermod": "/usr/sbin/usermod",
    "squeue-fallback": "/usr/bin/squeue",
}
SCRIPT_ALLOWLIST = {
    "h100-provision-stage": "/usr/local/sbin/h100-provision-stage",
    "h100-workspace-alias": "/usr/local/sbin/h100-workspace-alias",
    "h100-user-create": "/usr/local/sbin/h100-user-create",
    "h100-user-gpu-isolation": "/usr/local/sbin/h100-user-gpu-isolation",
    "h100-container-create": "/usr/local/sbin/h100-container-create",
    "h100-container-start": "/usr/local/sbin/h100-container-start",
    "h100-container-gpu-runtime": "/usr/local/sbin/h100-container-gpu-runtime",
    "h100-gpu-development-epilog": "/usr/local/sbin/h100-gpu-development-epilog",
    "h100-container-stop": "/usr/local/sbin/h100-container-stop",
    "h100-container-rebuild": "/usr/local/sbin/h100-container-rebuild",
    "h100-container-delete": "/usr/local/sbin/h100-container-delete",
    "h100-container-status": "/usr/local/sbin/h100-container-status",
    "h100-quota-show": "/usr/local/sbin/h100-quota-show",
    "h100-gpu-bypass-guard": "/usr/local/sbin/h100-gpu-bypass-guard",
    "h100-origin-pilot-acceptance": ("/opt/h100-portal/scripts/h100-origin-pilot-acceptance"),
}
INTEGRITY_FILE_ALLOWLIST = {
    # h100-provision-stage sources this root-owned library before it performs
    # any validation or writes, so it is part of the execution trust boundary.
    "h100-platform-common": "/usr/local/lib/h100-platform/h100-platform-common.sh",
}
SCRIPT_HASH_CONFIG = Path("/etc/h100-portal/worker-scripts.json")
GPU_ISOLATED_USERS = Path("/etc/h100-platform/gpu-isolated-users")
GPU_INFO_ROOT = Path("/proc/driver/nvidia/gpus")
PROJECTS_FILE = Path("/etc/projects")
PROJID_FILE = Path("/etc/projid")
ENROOT_ROOT = Path("/srv/gpu-platform/enroot")
STORAGE_PATHS = {
    "docker": "/var/lib/docker",
    "enroot": "/srv/gpu-platform/enroot",
    "datasets": "/srv/gpu-platform/datasets",
    "models": "/srv/gpu-platform/models",
    "scratch": "/srv/gpu-platform/scratch",
}
REGISTRY_ENDPOINTS = {
    "NGC": "https://nvcr.io/v2/",
    "GHCR": "https://ghcr.io/v2/",
    "Quay": "https://quay.io/v2/",
}
PCI_BUS_ID = re.compile(
    r"^(?P<domain>[0-9A-Fa-f]{4,8}):(?P<bus>[0-9A-Fa-f]{2}):"
    r"(?P<device>[0-9A-Fa-f]{2})\.(?P<function>[0-7])$"
)

# These ranges are the ranges already enforced by the host lifecycle scripts.
# Portal-3A starts at the first value used by the checked-in Pilot inventory
# example, but never reserves a value while producing a plan.
PILOT_UID_MIN = 20000
PILOT_UID_MAX = 60000
PILOT_UID_FIRST = 20001
PROJECT_ID_MIN = 30000
PROJECT_ID_MAX = 39999
PROJECT_ID_FIRST = 30001
PILOT_SSH_PORT_MIN = 22023
PILOT_SSH_PORT_MAX = 22999
PILOT_SSH_PORT_FIRST = 22023
PILOT_USERNAME = "origin-pilot"
MANAGEMENT_USERNAME = "origin-al"
MANAGEMENT_IP = "10.82.36.1"
CONTAINER_PUBLISH_HOST = "0.0.0.0"  # noqa: S104 -- approved user-ingress publish target
PUBLIC_ACCESS_HOST = "20.10.10.3"
SSH_REPRESENTATIVE_CLIENT_IP = "10.20.18.10"
SSH_REPRESENTATIVE_HOST = "sagsh100server"
GPU_DROPIN_NAME = "50-h100-gpu-isolation.conf"
GPU_DROPIN_CONTENT = "[Slice]\nDevicePolicy=closed\n"
GPU_REGISTRY = Path("/etc/h100-platform/gpu-isolated-users")
GUARD_METRIC_FILE = Path("/var/lib/node_exporter/textfile_collector/h100_gpu_bypass_guard.prom")
GUARD_METRIC_OWNER_UID = 0
GUARD_METRIC_OWNER_GID = 0
GUARD_METRIC_MAX_AGE_SECONDS = 15 * 60
PILOT_STATE_ROOT = Path("/etc/h100-platform/users")
PILOT_DATA_ROOT = Path("/srv/gpu-platform/users")
MANAGED_HOME_ROOT = Path("/home")
PLATFORM_BACKUP_ROOT = Path("/srv/gpu-platform/platform/backups")
PILOT_COMPOSE_ROOT = Path("/srv/gpu-platform/platform/config/dev-containers")
PILOT_SCAN_ROOTS = (
    Path("/home"),
    Path("/srv/gpu-platform"),
    Path("/var/lib/slurm"),
    Path("/var/spool/slurmctld"),
    Path("/var/spool/slurmd"),
)
SSH_KEY_STAGING_ROOT = Path("/var/lib/h100-portal/ssh-key-staging")
SSH_KEY_STAGING_OWNER_UID = 0
SSH_KEY_STAGING_OWNER_GID = 0
MAX_SSH_KEY_FILE_BYTES = 16 * 1024
MAX_APPROVED_SSH_KEYS = 5
PORTAL3C_STAGE_IDEMPOTENCY_KEY = "portal3b-r-origin-pilot-stage-v1"
PORTAL3C_STAGE_APPROVAL_REFERENCE = "portal3b-r-lifecycle-revalidated"
STAGE_EXECUTION_TIMEOUT_SECONDS = 1800.0
ACTIVATE_EXECUTION_TIMEOUT_SECONDS = 300.0
PORTAL3E_FINAL_APPROVAL_REFERENCE = "portal3e-final-origin-pilot-v1"
PORTAL3E_FINAL_DRY_RUN_OPERATION_ID = "f677d34a-4ef2-45ec-a323-99e4af148c0e"
PORTAL3E_FINAL_MANAGED_USER_ID = "3b95b4f0-95d9-444a-8f0b-46288195a807"
PORTAL3E_FINAL_KEY_RECORD_ID = "7427da72-37b9-4ac2-8ada-2f0c83b7718e"
PORTAL3E_FINAL_KEY_FINGERPRINT = "SHA256:nek6vyEb3GT+UJAcY5y/8PgY4achF2ouNy+8C2JqUVc"
PORTAL3E_FINAL_IDEMPOTENCY_KEY = "portal3e-final-origin-pilot-activate-v1"
PORTAL3E_FINAL_ROLLBACK_IDEMPOTENCY_KEY = "portal3e-final-origin-pilot-rollback-v1"
PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY = "portal3f-origin-pilot-client-validation-v1"
# v1-v6 remain immutable ROLLED_BACK audit records. v7 verifies the GPU task's
# Slurm cgroup from the host because Pyxis intentionally uses a cgroup namespace.
PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY = "portal3f-origin-pilot-acceptance-v7"
PORTAL3G_PRODUCTION_PILOT_IDEMPOTENCY_KEY = "portal3g-origin-pilot-production-pilot-v1"
PORTAL3G_DRAIN_IDEMPOTENCY_KEY = "portal3g-origin-pilot-safety-drain-v1"
HOST_ED25519_PUBLIC_KEY = Path("/etc/ssh/ssh_host_ed25519_key.pub")
CONTAINER_ED25519_PUBLIC_KEY = Path(
    "/srv/gpu-platform/container-data/origin-pilot/ssh-host-keys/ssh_host_ed25519_key.pub"
)
STAGE_REQUIRED_SCRIPTS = frozenset(
    {
        "h100-user-create",
        "h100-user-gpu-isolation",
        "h100-container-create",
        "h100-container-stop",
        "h100-gpu-bypass-guard",
    }
)
COMPUTE_STAGE_REQUIRED_SCRIPTS = frozenset(
    {
        "h100-provision-stage",
        "h100-platform-common",
        "h100-workspace-alias",
        "h100-user-gpu-isolation",
        "h100-gpu-bypass-guard",
    }
)
COMPUTE_STAGE_ARGV_BINDINGS = (
    ("STAGE_HANDLER_EXECUTABLE", "literal", COMPUTE_STAGE_HANDLER_PATH),
    ("EXECUTION_MODE", "literal", "--execute"),
    ("TARGET_USERNAME", "payload", "username"),
    ("TARGET_UID", "payload", "uid"),
    ("TARGET_GID", "payload", "gid"),
    ("XFS_PROJECT_ID", "payload", "project_id"),
    ("CONTAINER_SSH_PORT", "payload", "ssh_port"),
    ("SLURM_ACCOUNT", "payload", "slurm_account"),
    ("SLURM_QOS", "payload", "slurm_qos"),
    ("MAX_GPU_LIMIT", "payload", "gpu_max"),
    ("COMPUTE_REQUEST_ID", "payload", "request_id"),
    ("PROVISION_PLAN_ID", "payload", "plan_id"),
    ("DRY_RUN_OPERATION_ID", "payload", "dry_run_operation_id"),
    ("DEVELOPMENT_PROFILE", "payload", "container_profile"),
    ("EXPLICIT_STAGE_CONFIRMATION_FLAG", "literal", "--confirm-stage"),
    ("CONFIRMED_TARGET_USERNAME", "payload", "username"),
)
ACTIVATE_REQUIRED_SCRIPTS = frozenset(
    {
        "h100-user-create",
        "h100-user-gpu-isolation",
        "h100-container-start",
        "h100-container-gpu-runtime",
        "h100-container-stop",
        "h100-gpu-bypass-guard",
    }
)
SELF_ACTIVATE_REQUIRED_SCRIPTS = frozenset(
    {
        "h100-platform-common",
        "h100-user-gpu-isolation",
        "h100-container-start",
        "h100-container-stop",
    }
)
PORTAL3F_REQUIRED_SCRIPTS = ACTIVATE_REQUIRED_SCRIPTS | frozenset({"h100-origin-pilot-acceptance"})
FORBIDDEN_PILOT_GROUPS = frozenset(
    {"sudo", "docker", "video", "render", "adm", "systemd-journal", "gpu-platform-admin"}
)
SSH_POLICY_FIELDS = (
    "pubkeyauthentication",
    "passwordauthentication",
    "kbdinteractiveauthentication",
    "authenticationmethods",
    "permitrootlogin",
    "authorizedkeysfile",
    "usepam",
    "allowtcpforwarding",
    "x11forwarding",
)
SSH_MANAGEMENT_NO_REGRESSION_FIELDS = (
    "pubkeyauthentication",
    "passwordauthentication",
    "kbdinteractiveauthentication",
    "authenticationmethods",
    "permitrootlogin",
    "authorizedkeysfile",
)


def _truncate(value: str) -> str:
    if len(value) <= MAX_OUTPUT:
        return value
    return value[:MAX_OUTPUT] + "\n[OUTPUT_TRUNCATED]"


def run_fixed(binary: str, args: list[str], timeout: float = 20.0) -> dict[str, Any]:
    executable = BINARIES.get(binary)
    if executable is None or not os.path.isabs(executable):
        return {"ok": False, "error_code": "BINARY_NOT_ALLOWLISTED", "stdout": "", "stderr": ""}
    if not os.path.exists(executable):
        return {"ok": False, "error_code": "BINARY_UNAVAILABLE", "stdout": "", "stderr": executable}
    if any(not isinstance(item, str) or "\x00" in item for item in args):
        return {"ok": False, "error_code": "ARGUMENT_INVALID", "stdout": "", "stderr": ""}
    try:
        completed = subprocess.run(
            [executable, *args],
            cwd="/",
            env=FIXED_ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error_code": "COMMAND_TIMEOUT",
            "stdout": _truncate(str(exc.stdout or "")),
            "stderr": _truncate(str(exc.stderr or "")),
        }
    except OSError as exc:
        return {
            "ok": False,
            "error_code": "COMMAND_EXECUTION_ERROR",
            "stdout": "",
            "stderr": str(exc)[:512],
        }
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": _truncate(completed.stdout),
        "stderr": _truncate(completed.stderr),
    }


def run_allowlisted_script(
    argv: list[str],
    timeout: float,
    *,
    expected_local_image_identity: str | None = None,
) -> dict[str, Any]:
    """Run one exact management-script argv without a shell.

    The caller must validate the operation payload and script hashes first.
    This second boundary prevents a future caller from passing an arbitrary
    executable, NUL-containing argument, working directory, environment, or
    stdin into the root Worker.
    """
    if not argv or argv[0] not in SCRIPT_ALLOWLIST.values():
        return {
            "ok": False,
            "error_code": "SCRIPT_NOT_ALLOWLISTED",
            "stdout": "",
            "stderr": "",
        }
    if any(not isinstance(item, str) or "\x00" in item for item in argv):
        return {
            "ok": False,
            "error_code": "ARGUMENT_INVALID",
            "stdout": "",
            "stderr": "",
        }
    environment = FIXED_ENV
    if expected_local_image_identity is not None:
        if (
            argv[0] != COMPUTE_STAGE_HANDLER_PATH
            or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_local_image_identity) is None
        ):
            return {
                "ok": False,
                "error_code": "LOCAL_IMAGE_IDENTITY_ARGUMENT_INVALID",
                "stdout": "",
                "stderr": "",
            }
        environment = {
            **FIXED_ENV,
            "H100_EXPECTED_LOCAL_IMAGE_IDENTITY": expected_local_image_identity,
        }
    try:
        completed = subprocess.run(
            argv,
            cwd="/",
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error_code": "STAGE_EXECUTION_TIMEOUT",
            "stdout": _truncate(str(exc.stdout or "")),
            "stderr": _truncate(str(exc.stderr or "")),
        }
    except OSError as exc:
        return {
            "ok": False,
            "error_code": "SCRIPT_EXECUTION_ERROR",
            "stdout": "",
            "stderr": str(exc)[:512],
        }
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": _truncate(completed.stdout),
        "stderr": _truncate(completed.stderr),
    }


def _parse_csv(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    reader = csv.DictReader(text.splitlines(), skipinitialspace=True)
    for row in reader:
        rows.append(
            {str(key).strip(): value.strip() for key, value in row.items() if key is not None}
        )
    return rows


def _normalize_pci_bus_id(value: str) -> str | None:
    match = PCI_BUS_ID.fullmatch(value.strip())
    if match is None:
        return None
    domain = int(match.group("domain"), 16)
    if domain > 0xFFFF:
        return None
    return (
        f"{domain:04x}:{match.group('bus').lower()}:"
        f"{match.group('device').lower()}.{match.group('function')}"
    )


def _driver_gpu_minor_map() -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for information in sorted(GPU_INFO_ROOT.glob("*/information")):
        bus_from_path = _normalize_pci_bus_id(information.parent.name)
        if bus_from_path is None:
            continue
        try:
            with information.open(encoding="utf-8", errors="replace") as source:
                content = source.read(16 * 1024 + 1)
        except OSError:
            continue
        if len(content) > 16 * 1024:
            continue
        values: dict[str, str] = {}
        for line in content.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key.strip()] = value.strip()
        bus = _normalize_pci_bus_id(values.get("Bus Location", ""))
        minor = values.get("Device Minor", "")
        uuid = values.get("GPU UUID", "")
        if (
            bus != bus_from_path
            or not minor.isdigit()
            or not 0 <= int(minor) <= 255
            or re.fullmatch(r"GPU-[A-Za-z0-9-]{8,80}", uuid) is None
        ):
            continue
        mapping[bus] = {"minor_number": minor, "uuid": uuid}
    return mapping


def _parse_dcgm_json(text: str) -> dict[int, str]:
    """Return DCGM diagnostic status keyed by DCGM/NVML entity id."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    statuses: dict[int, str] = {}
    categories = payload.get("DCGM Diagnostic", {}).get("test_categories", [])
    if not isinstance(categories, list):
        return statuses
    for category in categories:
        if not isinstance(category, dict):
            continue
        for test in category.get("tests", []):
            if not isinstance(test, dict):
                continue
            for item in test.get("results", []):
                if not isinstance(item, dict) or item.get("entity_group") != "GPU":
                    continue
                entity_id = item.get("entity_id")
                status_value = item.get("status")
                if isinstance(entity_id, int) and isinstance(status_value, str):
                    # Preserve a failure if any test reports one.
                    previous = statuses.get(entity_id)
                    statuses[entity_id] = (
                        status_value
                        if previous is None or previous.casefold() != "fail"
                        else previous
                    )
    return statuses


def _active_gpu_job_ids() -> dict[int, list[int]]:
    """Best-effort mapping from explicit Slurm GPU index details to job ids.

    If there are no active jobs the empty mapping is authoritative. When Slurm
    does not expose an explicit index, we return no guessed association.
    """
    parsed = _json_command("squeue", ["--json"], timeout=20)
    if parsed.get("status") != "OK":
        return {}
    jobs = parsed.get("data", {}).get("jobs", [])
    mapping: dict[int, list[int]] = {}
    if not isinstance(jobs, list):
        return mapping
    for job in jobs:
        if not isinstance(job, dict):
            continue
        raw_id = job.get("job_id") or job.get("id")
        if not isinstance(raw_id, int):
            continue
        # Slurm JSON schemas vary across minor releases; only consume fields
        # that explicitly contain IDX/NVIDIA index values.
        encoded = json.dumps(job, ensure_ascii=True)
        for match in re.finditer(r"(?:IDX|index)[=: -]*(\d+)", encoded, re.IGNORECASE):
            index = int(match.group(1))
            if 0 <= index < 32:
                mapping.setdefault(index, []).append(raw_id)
    return mapping


def gpu_list() -> dict[str, Any]:
    fields = [
        "index",
        "uuid",
        "pci.bus_id",
        "name",
        "memory.total",
        "memory.used",
        "utilization.gpu",
        "temperature.gpu",
        "power.draw",
        "ecc.errors.uncorrected.volatile.total",
        "mig.mode.current",
        "pcie.link.gen.current",
        "pcie.link.width.current",
    ]
    result = run_fixed(
        "nvidia-smi",
        [f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
        timeout=20,
    )
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    minor_map = _driver_gpu_minor_map()
    rows: list[dict[str, Any]] = []
    mapping_errors: list[dict[str, str]] = []
    for row in csv.reader(result["stdout"].splitlines(), skipinitialspace=True):
        if len(row) != len(fields):
            continue
        item: dict[str, Any] = {
            field: value.strip() for field, value in zip(fields, row, strict=True)
        }
        normalized_bus = _normalize_pci_bus_id(item["pci.bus_id"])
        driver_identity = minor_map.get(normalized_bus or "")
        if driver_identity is None or driver_identity["uuid"] != item["uuid"]:
            item["minor_number"] = None
            item["device_path"] = None
            item["minor_source"] = None
            mapping_errors.append({"uuid": item["uuid"], "pci_bus_id": item["pci.bus_id"]})
        else:
            item["minor_number"] = driver_identity["minor_number"]
            item["device_path"] = f"/dev/nvidia{driver_identity['minor_number']}"
            item["minor_source"] = "nvidia-driver-procfs"
        rows.append(item)
    status = "OK" if rows and not mapping_errors else "UNKNOWN"
    job_mapping = _active_gpu_job_ids() if rows else {}
    for item in rows:
        try:
            index = int(item["index"])
        except KeyError, TypeError, ValueError:
            continue
        # DCGM diagnostic is sampled by gpu.health.read (a slower independent
        # source) and merged by the API. Never infer a DCGM result from NVML.
        item["dcgm_status"] = "UNKNOWN"
        item["active_slurm_job_ids"] = sorted(set(job_mapping.get(index, [])))
        item["slurm_job_mapping"] = "EXPLICIT_INDEX" if index in job_mapping else "NO_ACTIVE_JOB"
    response: dict[str, Any] = {"status": status, "gpus": rows, "count": len(rows)}
    if mapping_errors:
        response["error"] = {
            "error_code": "GPU_MINOR_MAPPING_INCOMPLETE",
            "identities": mapping_errors,
        }
    return response


def _json_command(binary: str, args: list[str], timeout: float = 20.0) -> dict[str, Any]:
    result = run_fixed(binary, args, timeout)
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    try:
        value = json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {"status": "UNKNOWN", "error": {"error_code": "JSON_PARSE_ERROR"}}
    return {"status": "OK", "data": value}


def _slurm_text(value: object, separator: str = "+") -> str:
    if isinstance(value, list):
        return separator.join(str(item) for item in value if str(item) not in {"", "INVALID"})
    if value is None:
        return ""
    return str(value)


def slurm_node() -> dict[str, Any]:
    parsed = _json_command("scontrol", ["show", "node", "--json"], timeout=20)
    if parsed["status"] == "OK":
        payload = parsed.get("data")
        raw_nodes = payload.get("nodes") if isinstance(payload, dict) else None
        if isinstance(raw_nodes, list):
            nodes = []
            for raw in raw_nodes:
                if not isinstance(raw, dict):
                    continue
                nodes.append(
                    {
                        "name": raw.get("name") or raw.get("hostname"),
                        "state": _slurm_text(raw.get("state")),
                        "reason": raw.get("reason") or "",
                        "cpus": raw.get("cpus"),
                        "alloc_cpus": raw.get("alloc_cpus"),
                        "real_memory": raw.get("real_memory"),
                        "alloc_memory": raw.get("alloc_memory"),
                        "gres": raw.get("gres"),
                        "gres_used": raw.get("gres_used"),
                        "cfg_tres": raw.get("tres"),
                        "alloc_tres": raw.get("tres_used"),
                        "partitions": _slurm_text(raw.get("partitions"), separator=","),
                    }
                )
            if nodes:
                return {"status": "OK", "source": "slurm-json", "nodes": nodes}
    result = run_fixed("scontrol", ["show", "node"], timeout=20)
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    node: dict[str, str] = {}
    for token in result["stdout"].replace("\n", " ").split():
        if "=" in token:
            key, value = token.split("=", 1)
            node[key] = value
    if not node:
        return {"status": "UNKNOWN", "error": {"error_code": "SLURM_NODE_PARSE_ERROR"}}
    normalized = {
        "name": node.get("NodeName"),
        "state": node.get("State", ""),
        "reason": node.get("Reason", ""),
        "cpus": node.get("CPUTot"),
        "alloc_cpus": node.get("CPUAlloc"),
        "real_memory": node.get("RealMemory"),
        "alloc_memory": node.get("AllocMem"),
        "gres": node.get("Gres"),
        "gres_used": node.get("GresUsed"),
        "cfg_tres": node.get("CfgTRES"),
        "alloc_tres": node.get("AllocTRES"),
        "partitions": node.get("Partitions"),
    }
    return {"status": "OK", "source": "scontrol-text-fallback", "nodes": [normalized]}


def slurm_jobs() -> dict[str, Any]:
    parsed = _json_command("squeue", ["--json"], timeout=20)
    if parsed["status"] == "OK":
        payload = parsed.get("data")
        raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if isinstance(raw_jobs, list):
            jobs = []
            for raw in raw_jobs:
                if not isinstance(raw, dict):
                    continue
                jobs.append(
                    {
                        "job_id": raw.get("job_id") or raw.get("id"),
                        "user": raw.get("user_name") or raw.get("user"),
                        "state": _slurm_text(raw.get("job_state") or raw.get("state")),
                        "partition": raw.get("partition"),
                        "name": raw.get("name"),
                        "reason": _slurm_text(raw.get("state_reason") or raw.get("reason")),
                        "nodes": raw.get("nodes") or raw.get("node_count"),
                    }
                )
            return {"status": "OK", "source": "slurm-json", "jobs": jobs}
    result = run_fixed("squeue", ["-h", "-o", "%i|%P|%j|%u|%T|%M|%D|%R"], timeout=20)
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    jobs = []
    for line in result["stdout"].splitlines():
        fields = line.split("|", 7)
        if len(fields) == 8:
            jobs.append(
                dict(
                    zip(
                        ["job_id", "partition", "name", "user", "state", "time", "nodes", "reason"],
                        fields,
                        strict=True,
                    )
                )
            )
    return {"status": "OK", "jobs": jobs}


def slurm_accounts() -> dict[str, Any]:
    result = run_fixed(
        "sacctmgr", ["-n", "-P", "show", "account", "format=Account,Description"], timeout=20
    )
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    accounts = []
    for line in result["stdout"].splitlines():
        name, _, description = line.partition("|")
        if name:
            accounts.append({"account": name, "description": description})
    qos_result = run_fixed(
        "sacctmgr",
        [
            "-n",
            "-P",
            "show",
            "qos",
            "format=Name,Priority,MaxTRESPerUser,MaxJobsPU,MaxSubmitJobsPU",
        ],
        timeout=20,
    )
    qos = []
    if qos_result.get("ok"):
        for line in qos_result["stdout"].splitlines():
            fields = line.split("|")
            if fields and fields[0]:
                qos.append(
                    dict(
                        zip(
                            [
                                "name",
                                "priority",
                                "max_tres_per_user",
                                "max_jobs_per_user",
                                "max_submit_jobs_per_user",
                            ],
                            fields + [""] * 5,
                            strict=False,
                        )
                    )
                )
    assoc_result = run_fixed(
        "sacctmgr",
        ["-n", "-P", "show", "assoc", "format=Cluster,Account,User,Partition,QOS,DefaultQOS"],
        timeout=20,
    )
    associations = []
    if assoc_result.get("ok"):
        for line in assoc_result["stdout"].splitlines():
            fields = line.split("|")
            if fields and fields[0]:
                associations.append(
                    dict(
                        zip(
                            ["cluster", "account", "user", "partition", "qos", "default_qos"],
                            fields + [""] * 6,
                            strict=False,
                        )
                    )
                )
    status = "OK" if qos_result.get("ok") and assoc_result.get("ok") else "PARTIAL"
    return {"status": status, "accounts": accounts, "qos": qos, "associations": associations}


def slurm_history() -> dict[str, Any]:
    result = run_fixed(
        "sacct",
        [
            "-S",
            "now-7days",
            "-X",
            "-n",
            "-P",
            "-a",
            "-o",
            "JobIDRaw,JobName,User,Partition,State,Elapsed,AllocTRES,ExitCode",
        ],
        timeout=25,
    )
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    fields = ["job_id", "name", "user", "partition", "state", "elapsed", "alloc_tres", "exit_code"]
    jobs = []
    for line in result["stdout"].splitlines()[-200:]:
        values = line.split("|", len(fields) - 1)
        if len(values) == len(fields) and values[0]:
            jobs.append(dict(zip(fields, values, strict=True)))
    return {"status": "OK", "jobs": jobs, "count": len(jobs), "window": "7d"}


def containers_list() -> dict[str, Any]:
    result = run_fixed(
        "docker",
        ["ps", "--all", "--no-trunc", "--format", "{{json .}}"],
        timeout=20,
    )
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    items: list[dict[str, Any]] = []
    for line in result["stdout"].splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            labels = str(raw.get("Labels") or "")
            safe_labels: dict[str, str] = {}
            for pair in labels.split(","):
                key, separator, value = pair.partition("=")
                if separator and key in {
                    "h100.dev.user",
                    "h100.base.digest",
                    "org.opencontainers.image.version",
                }:
                    safe_labels[key] = value[:255]
            items.append(
                {
                    key: raw.get(key)
                    for key in (
                        "ID",
                        "Names",
                        "Image",
                        "ImageID",
                        "Command",
                        "CreatedAt",
                        "Status",
                        "Ports",
                    )
                }
            )
            items[-1]["owner"] = safe_labels.get("h100.dev.user")
            items[-1]["image_digest"] = raw.get("ImageID")
            items[-1]["safe_labels"] = safe_labels
    return {"status": "OK", "containers": items, "count": len(items)}


def containers_inspect(payload: dict[str, Any]) -> dict[str, Any]:
    name = payload["name"]
    if not (name.startswith("gpu-dev-") or name.startswith("h100-")):
        return {"status": "DENIED", "error": {"error_code": "CONTAINER_PREFIX_REQUIRED"}}
    result = run_fixed("docker", ["inspect", "--type", "container", name], timeout=20)
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    try:
        raw = json.loads(result["stdout"])
        item = raw[0] if isinstance(raw, list) and raw else {}
    except json.JSONDecodeError, IndexError:
        return {"status": "UNKNOWN", "error": {"error_code": "DOCKER_JSON_PARSE_ERROR"}}
    if not isinstance(item, dict):
        return {"status": "UNKNOWN", "error": {"error_code": "DOCKER_SCHEMA_ERROR"}}
    host_config = item.get("HostConfig") or {}
    config = item.get("Config") or {}
    network = item.get("NetworkSettings") or {}
    image_descriptor = item.get("ImageManifestDescriptor") or {}
    port_bindings = host_config.get("PortBindings") or {}
    ssh_ports = port_bindings.get("22/tcp") or []
    ssh_port = (
        ssh_ports[0].get("HostPort") if ssh_ports and isinstance(ssh_ports[0], dict) else None
    )
    ssh_host_ip = (
        ssh_ports[0].get("HostIp") if ssh_ports and isinstance(ssh_ports[0], dict) else None
    )
    labels_value = config.get("Labels")
    labels: dict[str, Any] = labels_value if isinstance(labels_value, dict) else {}
    safe_labels = {
        key: str(labels[key])[:255]
        for key in (
            "h100.dev.user",
            "h100.dev.uid",
            "h100.dev.gid",
            "h100.base.digest",
            "h100.dev.gpu-allocation-job",
            "h100.dev.gpu-uuid",
            "org.opencontainers.image.version",
        )
        if key in labels
    }
    nano_cpus = host_config.get("NanoCpus")
    cpu_limit = (
        (float(nano_cpus) / 1_000_000_000) if isinstance(nano_cpus, int) and nano_cpus else None
    )
    memory_limit = host_config.get("Memory")
    pids_limit = host_config.get("PidsLimit")
    mounts = [
        {key: mount.get(key) for key in ("Type", "Source", "Destination", "RW")}
        for mount in item.get("Mounts", [])
        if isinstance(mount, dict)
    ]
    docker_socket = any(
        str(mount.get("Destination", "")) in {"/var/run/docker.sock", "/run/docker.sock"}
        for mount in mounts
    )
    device_requests = host_config.get("DeviceRequests")
    devices = host_config.get("Devices")
    device_cgroup_rules = host_config.get("DeviceCgroupRules")
    raw_environment = config.get("Env")
    environment: list[Any] = raw_environment if isinstance(raw_environment, list) else []
    safe_environment = {
        key: value
        for item in environment
        if isinstance(item, str)
        for key, separator, value in (item.partition("="),)
        if separator and key in {"CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES"}
    }
    return {
        "status": "OK",
        "container": {
            "name": item.get("Name"),
            "id": item.get("Id"),
            "created": item.get("Created"),
            "state": item.get("State"),
            "image": config.get("Image"),
            "image_digest": image_descriptor.get("digest") or item.get("Image"),
            "image_id": item.get("Image"),
            "owner": safe_labels.get("h100.dev.user"),
            "safe_labels": safe_labels,
            "cpu_limit": cpu_limit,
            "nano_cpus": nano_cpus,
            "memory_limit_bytes": memory_limit,
            "pids_limit": pids_limit,
            "ssh_port": ssh_port,
            "ssh_host_ip": ssh_host_ip,
            "privileged": host_config.get("Privileged"),
            "network_mode": host_config.get("NetworkMode"),
            "pid_mode": host_config.get("PidMode"),
            "ipc_mode": host_config.get("IpcMode"),
            "mounts": mounts,
            "device_requests": device_requests,
            "devices": devices,
            "device_cgroup_rules": device_cgroup_rules,
            "cap_add": host_config.get("CapAdd"),
            "runtime": host_config.get("Runtime"),
            "runtime_user": config.get("User"),
            "safe_environment": safe_environment,
            "gpu": "NONE" if not device_requests else "REQUESTED",
            "docker_socket_mounted": docker_socket,
            "restart_policy": host_config.get("RestartPolicy"),
            "ports": network.get("Ports"),
        },
    }


def storage_summary() -> dict[str, Any]:
    df = run_fixed(
        "df",
        [
            "-B1",
            "--output=target,fstype,size,used,avail,pcent",
            "/",
            "/var/lib/docker",
            "/srv/gpu-platform",
        ],
        timeout=20,
    )
    vgs = _json_command(
        "vgs",
        ["--reportformat", "json", "--units", "b", "-o", "vg_name,vg_size,vg_free"],
        timeout=20,
    )
    mounts: list[dict[str, str]] = []
    if df.get("ok"):
        for line in df["stdout"].splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 6:
                mounts.append(
                    dict(
                        zip(
                            ["target", "fstype", "size", "used", "avail", "percent"],
                            fields[:6],
                            strict=True,
                        )
                    )
                )
    usage: dict[str, dict[str, Any]] = {}
    for label, path in STORAGE_PATHS.items():
        result = run_fixed("du", ["-s", "-B1", path], timeout=30)
        if result.get("ok"):
            first = result.get("stdout", "").splitlines()[0].split()
            usage[label] = {
                "path": path,
                "status": "OK",
                "bytes": int(first[0]) if first and first[0].isdigit() else None,
            }
        else:
            usage[label] = {"path": path, "status": "ABSENT", "bytes": None}
    docker_df = run_fixed("docker", ["system", "df", "--format", "{{json .}}"], timeout=25)
    docker_usage: list[dict[str, str]] = []
    if docker_df.get("ok"):
        for line in docker_df.get("stdout", "").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                docker_usage.append(
                    {
                        key.casefold(): str(item.get(key, ""))[:128]
                        for key in ("Type", "TotalCount", "Active", "Size", "Reclaimable")
                    }
                )
    return {
        "status": "OK" if mounts and vgs.get("status") == "OK" else "PARTIAL",
        "mounts": mounts,
        "volume_groups": vgs,
        "path_usage": usage,
        "docker_usage": docker_usage,
        "enroot_cache": usage.get("enroot", {"status": "ABSENT"}),
        "datasets": usage.get("datasets", {"status": "ABSENT"}),
        "models": usage.get("models", {"status": "ABSENT"}),
        "scratch": usage.get("scratch", {"status": "ABSENT"}),
    }


def quotas_list() -> dict[str, Any]:
    result = run_fixed("xfs_quota", ["-x", "-c", "report -p -b -n"], timeout=20)
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    projects: dict[str, str] = {}
    try:
        for line in PROJECTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            name, separator, project_path = line.partition(":")
            if separator and project_path:
                projects[project_path.strip()] = name.strip()
    except OSError:
        pass
    projids: dict[str, str] = {}
    try:
        for line in PROJID_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            name, separator, number = line.partition(":")
            if separator:
                projids[number.strip()] = name.strip()
    except OSError:
        pass
    rows: list[dict[str, Any]] = []
    for line in result["stdout"].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Project") or stripped.startswith("-"):
            continue
        fields = stripped.split()
        if len(fields) >= 4 and fields[0].startswith("#"):
            project_id = fields[0][1:]
            rows.append(
                {
                    "project_id": project_id,
                    "name": projids.get(project_id),
                    "used_blocks": fields[1],
                    "soft_blocks": fields[2],
                    "hard_blocks": fields[3],
                    "grace": fields[4] if len(fields) > 4 else "",
                }
            )
    return {
        "status": "OK",
        "report": result["stdout"][:MAX_OUTPUT],
        "projects": rows,
        "project_file_entries": len(projects),
    }


def systemd_failed() -> dict[str, Any]:
    result = run_fixed("systemctl", ["--failed", "--no-legend", "--plain"], timeout=20)
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    failed = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
    return {"status": "OK", "failed_units": failed, "count": len(failed)}


def monitoring_alerts() -> dict[str, Any]:
    result = _prometheus_json("/api/v1/alerts")
    if result.get("status") != "OK":
        return result
    data = result.get("data", {})
    alerts = data.get("alerts", []) if isinstance(data, dict) else []
    safe_alerts = []
    for alert in alerts[:200]:
        if isinstance(alert, dict):
            labels = alert.get("labels", {})
            annotations = alert.get("annotations", {})
            safe_alerts.append(
                {
                    "name": labels.get("alertname"),
                    "severity": labels.get("severity"),
                    "state": alert.get("state"),
                    "summary": str(annotations.get("summary", ""))[:255],
                    "active_at": alert.get("activeAt"),
                }
            )
    return {"status": "OK", "alerts": safe_alerts, "count": len(safe_alerts)}


def _prometheus_json(path: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:9090{path}", timeout=8) as response:
            payload = json.loads(response.read(MAX_OUTPUT + 1))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {
            "status": "UNKNOWN",
            "error": {"code": "PROMETHEUS_UNAVAILABLE", "detail": str(exc)[:128]},
        }
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return {"status": "UNKNOWN", "error": {"code": "PROMETHEUS_SCHEMA_ERROR"}}
    return {"status": "OK", "data": payload.get("data")}


def _prometheus_query(expression: str) -> dict[str, Any]:
    encoded = urllib.parse.urlencode({"query": expression})
    return _prometheus_json(f"/api/v1/query?{encoded}")


def _prometheus_targets() -> dict[str, Any]:
    result = _prometheus_json("/api/v1/targets?state=active")
    if result.get("status") != "OK":
        return result
    active = result.get("data", {}).get("activeTargets", [])
    targets = []
    if isinstance(active, list):
        for target in active[:100]:
            if isinstance(target, dict):
                labels = target.get("labels", {})
                targets.append(
                    {
                        "job": labels.get("job"),
                        "instance": labels.get("instance"),
                        "health": target.get("health"),
                        "last_error": str(target.get("lastError", ""))[:255],
                        "last_scrape": target.get("lastScrape"),
                        "scrape_url": target.get("scrapeUrl"),
                    }
                )
    return {"status": "OK", "targets": targets, "count": len(targets)}


def _metric_values(expressions: list[str]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for expression in expressions:
        result = _prometheus_query(expression)
        if result.get("status") != "OK":
            continue
        data = result.get("data", {})
        result_rows = data.get("result", []) if isinstance(data, dict) else []
        if isinstance(result_rows, list):
            for row in result_rows[:200]:
                if isinstance(row, dict):
                    metric = row.get("metric", {})
                    value = row.get("value", [])
                    values.append(
                        {
                            "metric": metric.get("__name__")
                            if isinstance(metric, dict)
                            else expression,
                            "labels": {
                                str(key): str(value)[:128]
                                for key, value in (
                                    metric.items() if isinstance(metric, dict) else []
                                )
                                if key != "__name__"
                                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(key))
                            },
                            "value": value[1]
                            if isinstance(value, list) and len(value) > 1
                            else None,
                        }
                    )
    return values


def monitoring_summary() -> dict[str, Any]:
    alerts = monitoring_alerts()
    targets = _prometheus_targets()
    metrics = _metric_values(
        [
            "DCGM_FI_DEV_GPU_UTIL",
            "DCGM_FI_DEV_FB_USED",
            "DCGM_FI_DEV_GPU_TEMP",
            "DCGM_FI_DEV_POWER_USAGE",
            "h100_mellanox_rx_crc_errors_phy_total",
            "h100_mellanox_rx_symbol_err_phy_total",
            "h100_gpu_bypass_guard_last_success",
            "h100_gpu_bypass_guard_managed_users",
            "h100_gpu_bypass_guard_policy_errors",
        ]
    )
    grafana = _probe_http_health("http://127.0.0.1:3000/api/health")
    systemd = systemd_failed()
    slurm = slurm_node()
    containers = containers_list()
    storage = storage_summary()
    statuses = [alerts, targets, systemd, slurm, containers, storage]
    return {
        "status": "OK"
        if all(item.get("status") in {"OK", "PARTIAL"} for item in statuses)
        else "PARTIAL",
        "prometheus": {
            "status": "OK" if targets.get("status") == "OK" else "UNKNOWN",
            "targets": targets,
        },
        "alerts": alerts,
        "metrics": metrics,
        "guard": {
            "timer": _systemd_state("h100-gpu-bypass-guard.timer"),
            "metrics": [
                item
                for item in metrics
                if str(item.get("metric", "")).startswith("h100_gpu_bypass_guard")
            ],
        },
        "mellanox": {
            "risk": "P0_DEFERRED",
            "metrics": [
                item for item in metrics if str(item.get("metric", "")).startswith("h100_mellanox_")
            ],
        },
        "systemd": systemd,
        "docker": {"status": containers.get("status"), "count": containers.get("count")},
        "slurm": {"status": slurm.get("status"), "node": slurm},
        "storage": {"status": storage.get("status"), "mounts": storage.get("mounts", [])},
        "grafana": grafana | {"url": "http://10.10.10.220:3000"},
    }


def _probe_http_health(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - fixed localhost Grafana URL
            payload = json.loads(response.read(16 * 1024))
        return {
            "status": "OK",
            "http_status": response.status,
            "data": payload if isinstance(payload, dict) else {},
        }
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {
            "status": "UNKNOWN",
            "error": {"code": "HTTP_HEALTH_UNAVAILABLE", "detail": str(exc)[:128]},
        }


def _systemd_state(unit: str) -> dict[str, str]:
    enabled = run_fixed("systemctl", ["is-enabled", unit], timeout=10)
    active = run_fixed("systemctl", ["is-active", unit], timeout=10)
    return {
        "unit": unit,
        "enabled": str(enabled.get("stdout", "")).strip() or "unknown",
        "active": str(active.get("stdout", "")).strip() or "unknown",
    }


def registry_status() -> dict[str, Any]:
    local = images_list()
    registries: list[dict[str, Any]] = [
        {
            "name": "Local",
            "status": "AVAILABLE" if local.get("status") in {"OK", "PARTIAL"} else "UNKNOWN",
            "detail": "本地 Docker 镜像库存",
            "endpoint": "local://docker",
        }
    ]
    for name, endpoint in REGISTRY_ENDPOINTS.items():
        probe = _probe_registry(endpoint)
        registries.append(
            {
                "name": name,
                "status": probe["status"],
                "detail": probe["detail"],
                "endpoint": endpoint,
            }
        )
    registries.append(
        {
            "name": "Docker Hub",
            "status": "DEFERRED",
            "detail": "管理员明确延期；不作为运行时依赖",
            "endpoint": "https://registry-1.docker.io/v2/",
        }
    )
    return {"status": "OK", "registries": registries}


def _probe_registry(endpoint: str) -> dict[str, str]:
    request = urllib.request.Request(  # noqa: S310 - endpoint is a fixed registry allowlist
        endpoint, method="GET", headers={"User-Agent": "h100-portal/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310 - endpoint is a fixed registry allowlist
            code = int(response.status)
            return {
                "status": "AVAILABLE" if code in {200, 401, 403} else "UNKNOWN",
                "detail": f"endpoint reachable (HTTP {code})",
            }
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return {"status": "AVAILABLE", "detail": f"endpoint reachable (HTTP {exc.code})"}
        return {"status": "UNKNOWN", "detail": f"HTTP {exc.code}"}
    except (OSError, urllib.error.URLError) as exc:
        return {"status": "UNKNOWN", "detail": f"probe failed: {type(exc).__name__}"}


def images_list() -> dict[str, Any]:
    result = run_fixed(
        "docker",
        ["image", "ls", "--no-trunc", "--digests", "--format", "{{json .}}"],
        timeout=25,
    )
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    images: list[dict[str, Any]] = []
    for line in result.get("stdout", "").splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        image_id = str(raw.get("ID", ""))
        descriptor: dict[str, Any] = {}
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            inspected = run_fixed("docker", ["image", "inspect", image_id], timeout=20)
            if inspected.get("ok"):
                try:
                    parsed = json.loads(inspected.get("stdout", ""))
                    descriptor = parsed[0] if isinstance(parsed, list) and parsed else {}
                except json.JSONDecodeError, IndexError:
                    descriptor = {}
        repo = str(raw.get("Repository", ""))
        tag = str(raw.get("Tag", ""))
        digest = str(raw.get("Digest", ""))
        if digest in {"", "<none>"}:
            digest = image_id
        images.append(
            {
                "registry": repo.split("/", 1)[0]
                if "/" in repo and "." in repo.split("/", 1)[0]
                else "Local",
                "repository": repo,
                "tag": None if tag == "<none>" else tag,
                "digest": digest,
                "image_id": image_id,
                "size": raw.get("Size"),
                "architecture": descriptor.get("Architecture"),
                "os": descriptor.get("Os"),
                "containers": raw.get("Containers"),
                "created_at": raw.get("CreatedAt"),
                "immutable": digest.startswith("sha256:"),
            }
        )
    return {"status": "OK", "images": images, "count": len(images)}


def gpu_isolation_status() -> dict[str, Any]:
    if not GPU_ISOLATED_USERS.exists():
        return {"status": "OK", "managed_users": [], "managed_users_count": 0}
    entries: list[dict[str, Any]] = []
    for line in GPU_ISOLATED_USERS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if (
            len(fields) == 2
            and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", fields[0])
            and fields[1].isdigit()
        ):
            entries.append({"username": fields[0], "uid": int(fields[1])})
    return {"status": "OK", "managed_users": entries, "managed_users_count": len(entries)}


def gpu_health() -> dict[str, Any]:
    discovery = run_fixed("dcgmi", ["discovery", "-l"], timeout=20)
    diag = run_fixed("dcgmi", ["diag", "-r", "1", "-j"], timeout=90)
    kernel = run_fixed(
        "journalctl",
        ["-k", "-b", "--no-pager", "-g", "NVRM: Xid|PCIe Bus Error|AER:.*error"],
        timeout=20,
    )
    event_lines = [
        line
        for line in kernel.get("stdout", "").splitlines()
        if line.strip() and not line.startswith("-- ")
    ]
    no_kernel_matches = (
        kernel.get("exit_code") == 1
        and kernel.get("stdout", "").strip() == "-- No entries --"
        and not kernel.get("stderr", "").strip()
        and not event_lines
    )
    kernel_query_ok = bool(kernel.get("ok") or no_kernel_matches)
    kernel_status = (
        "DETECTED"
        if kernel.get("ok") and event_lines
        else "CLEAR"
        if kernel_query_ok
        else "UNKNOWN"
    )
    per_gpu = _parse_dcgm_json(str(diag.get("stdout", ""))) if diag.get("ok") else {}
    status = (
        "OK" if discovery.get("ok") and diag.get("ok") and kernel_status == "CLEAR" else "PARTIAL"
    )
    return {
        "status": status,
        "discovery": {"ok": discovery.get("ok"), "output": discovery.get("stdout", "")[-4096:]},
        "diag": {"ok": diag.get("ok"), "output": diag.get("stdout", "")[-8192:]},
        "per_gpu": [
            {"index": index, "dcgm_status": value} for index, value in sorted(per_gpu.items())
        ],
        "kernel_errors": {
            "status": kernel_status,
            "count": len(event_lines),
            "query_ok": kernel_query_ok,
        },
    }


def platform_health() -> dict[str, Any]:
    node = slurm_node()
    jobs = slurm_jobs()
    failed = systemd_failed()
    gpu = gpu_list()
    iso = gpu_isolation_status()
    return {
        "status": "OK"
        if all(item.get("status") == "OK" for item in (node, jobs, failed, gpu, iso))
        else "PARTIAL",
        "node": node,
        "jobs": jobs,
        "systemd": failed,
        "gpu": gpu,
        "gpu_isolation": iso,
    }


def script_integrity() -> dict[str, Any]:
    configured: dict[str, str] = {}
    try:
        raw = json.loads(SCRIPT_HASH_CONFIG.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            configured = {str(key): str(value) for key, value in raw.items()}
    except OSError, json.JSONDecodeError:
        pass
    result: dict[str, Any] = {}
    for name, path_string in (SCRIPT_ALLOWLIST | INTEGRITY_FILE_ALLOWLIST).items():
        path = Path(path_string)
        try:
            file_stat = path.lstat()
            regular_file = stat.S_ISREG(file_stat.st_mode)
            symlink = stat.S_ISLNK(file_stat.st_mode)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            hash_configured = name in configured
            hash_matches = hash_configured and configured[name] == digest
            owner_root = file_stat.st_uid == 0
            writable = bool(file_stat.st_mode & 0o022)
            result[name] = {
                "path": path_string,
                "sha256": digest,
                "owner_uid": file_stat.st_uid,
                "mode": oct(file_stat.st_mode & 0o777),
                "regular_file": regular_file,
                "symlink": symlink,
                "hash_configured": hash_configured,
                "hash_matches": hash_matches,
                "writable_by_group_or_other": writable,
                "integrity_ok": regular_file
                and not symlink
                and owner_root
                and not writable
                and hash_matches,
            }
        except OSError as exc:
            result[name] = {"path": path_string, "error": str(exc)[:128]}
    return result


class LifecycleValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _parse_sshd_effective_config(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in output.splitlines():
        keyword, separator, value = raw_line.strip().partition(" ")
        if separator and keyword in SSH_POLICY_FIELDS:
            if keyword in values:
                raise LifecycleValidationError(
                    "HOST_SSH_POLICY_PREFLIGHT_FAILED",
                    "sshd effective configuration contains duplicate policy fields",
                )
            values[keyword] = value.strip()
    missing = sorted(set(SSH_POLICY_FIELDS) - set(values))
    if missing:
        raise LifecycleValidationError(
            "HOST_SSH_POLICY_PREFLIGHT_FAILED",
            "sshd effective configuration is incomplete",
        )
    return values


def _sshd_effective_config(username: str | None) -> dict[str, str]:
    """Read only a fixed effective SSH context; callers cannot supply `-C` data."""
    if username is not None and username not in {PILOT_USERNAME, MANAGEMENT_USERNAME, "codexops"}:
        raise LifecycleValidationError(
            "HOST_SSH_POLICY_TARGET_REJECTED",
            "SSH policy checks are limited to fixed managed and management identities",
        )
    arguments = ["-T"]
    if username is not None:
        arguments.extend(
            [
                "-C",
                f"user={username},host={SSH_REPRESENTATIVE_HOST},"
                f"addr={SSH_REPRESENTATIVE_CLIENT_IP},laddr={MANAGEMENT_IP},lport=22",
            ]
        )
    result = run_fixed("sshd", arguments, timeout=10)
    if not result.get("ok"):
        raise LifecycleValidationError(
            "HOST_SSH_POLICY_PREFLIGHT_FAILED",
            "sshd effective configuration could not be evaluated",
        )
    return _parse_sshd_effective_config(str(result.get("stdout", "")))


def managed_host_ssh_policy(username: str) -> dict[str, Any]:
    if username != PILOT_USERNAME:
        raise LifecycleValidationError(
            "HOST_SSH_POLICY_TARGET_REJECTED",
            "Activate SSH policy is bound to the managed compute identity",
        )
    effective = _sshd_effective_config(username)
    methods_raw = effective["authenticationmethods"]
    policy = {
        "pubkey_authentication": effective["pubkeyauthentication"] == "yes",
        "password_authentication": effective["passwordauthentication"] == "yes",
        "keyboard_interactive_authentication": effective["kbdinteractiveauthentication"] == "yes",
        "authentication_methods": [item for item in re.split(r"[\s,]+", methods_raw) if item],
        "authentication_methods_raw": methods_raw,
        "status": "PASSING",
        "source": "SSHD_EFFECTIVE_CONFIG",
        "representative_host": SSH_REPRESENTATIVE_HOST,
        "representative_client_address": SSH_REPRESENTATIVE_CLIENT_IP,
        "local_address": MANAGEMENT_IP,
        "local_port": 22,
    }
    if not (
        policy["pubkey_authentication"] is True
        and policy["password_authentication"] is False
        and policy["keyboard_interactive_authentication"] is False
        and methods_raw == "publickey"
    ):
        raise LifecycleValidationError(
            "HOST_SSH_POLICY_PREFLIGHT_FAILED",
            "managed compute SSH is not public-key-only",
        )
    return policy


def validate_ssh_policy_no_regression(
    username: str, before: dict[str, str], after: dict[str, str]
) -> None:
    if username not in {MANAGEMENT_USERNAME, "codexops"}:
        raise LifecycleValidationError(
            "HOST_SSH_POLICY_TARGET_REJECTED",
            "no-regression comparison is limited to management identities",
        )
    changed = [
        field
        for field in SSH_MANAGEMENT_NO_REGRESSION_FIELDS
        if before.get(field) != after.get(field)
    ]
    if changed:
        raise LifecycleValidationError(
            "HOST_SSH_POLICY_MANAGEMENT_REGRESSION",
            f"{username} effective SSH policy changed",
        )


def ssh_policy_status() -> dict[str, Any]:
    try:
        global_effective = _sshd_effective_config(None)
        managed = managed_host_ssh_policy(PILOT_USERNAME)
        origin_al = _sshd_effective_config(MANAGEMENT_USERNAME)
        codexops = _sshd_effective_config("codexops")
    except LifecycleValidationError as exc:
        return {
            "status": "PARTIAL",
            "managed_compute_user_policy": {
                "status": "BLOCKED",
                "error_code": exc.code,
            },
        }
    return {
        "status": "OK",
        "global_ssh_policy": {
            "password_authentication": global_effective["passwordauthentication"] == "yes",
            "pubkey_authentication": global_effective["pubkeyauthentication"] == "yes",
            "keyboard_interactive_authentication": global_effective["kbdinteractiveauthentication"]
            == "yes",
            "authentication_methods": global_effective["authenticationmethods"],
        },
        "managed_compute_user_policy": managed,
        "management_identities": {
            MANAGEMENT_USERNAME: {
                field: origin_al[field] for field in SSH_MANAGEMENT_NO_REGRESSION_FIELDS
            },
            "codexops": {field: codexops[field] for field in SSH_MANAGEMENT_NO_REGRESSION_FIELDS},
        },
    }


def _ssh_key_fingerprint(public_key_line: str) -> str:
    executable = BINARIES["ssh-keygen"]
    try:
        completed = subprocess.run(
            [executable, "-lf", "-", "-E", "sha256"],
            cwd="/",
            env=FIXED_ENV,
            input=f"{public_key_line}\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
            shell=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH public-key validation could not run"
        ) from exc
    if completed.returncode != 0:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH public-key validation failed"
        )
    fields = completed.stdout.split()
    if len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH key did not produce a SHA-256 fingerprint"
        )
    return fields[1]


def _validate_public_key_content(public_key_line: str) -> dict[str, Any]:
    upper = public_key_line.upper()
    if "PRIVATE KEY" in upper or "-----BEGIN" in upper or "-----END" in upper:
        raise LifecycleValidationError(
            "SSH_PRIVATE_KEY_UPLOAD_REJECTED", "private-key material is forbidden"
        )
    if (
        public_key_line != public_key_line.strip()
        or "\r" in public_key_line
        or "\n" in public_key_line
    ):
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "one canonical public key is required per record"
        )
    parts = public_key_line.split(maxsplit=2)
    allowed_types = {
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "sk-ssh-ed25519@openssh.com",
    }
    if len(parts) < 2 or parts[0] not in allowed_types:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH key type is not approved"
        )
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH public-key payload is malformed"
        ) from exc

    def read_field(offset: int) -> tuple[bytes, int]:
        if offset + 4 > len(blob):
            raise LifecycleValidationError(
                "PUBLIC_KEY_VALIDATION_FAILED", "SSH public-key blob is truncated"
            )
        length = struct.unpack(">I", blob[offset : offset + 4])[0]
        start = offset + 4
        end = start + length
        if length > MAX_SSH_KEY_FILE_BYTES or end > len(blob):
            raise LifecycleValidationError(
                "PUBLIC_KEY_VALIDATION_FAILED", "SSH public-key field length is invalid"
            )
        return blob[start:end], end

    encoded_type, offset = read_field(0)
    if encoded_type != parts[0].encode("ascii"):
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH public-key type does not match its blob"
        )
    if parts[0] == "ssh-ed25519":
        public_bytes, offset = read_field(offset)
        structure_ok = len(public_bytes) == 32
    elif parts[0] == "ecdsa-sha2-nistp256":
        curve, offset = read_field(offset)
        point, offset = read_field(offset)
        structure_ok = curve == b"nistp256" and len(point) == 65 and point[:1] == b"\x04"
    else:
        public_bytes, offset = read_field(offset)
        application, offset = read_field(offset)
        structure_ok = len(public_bytes) == 32 and bool(application)
    if not structure_ok or offset != len(blob):
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH public-key blob structure is invalid"
        )
    if len(parts) == 3:
        comment = parts[2]
        if len(comment) > 128 or any(
            unicodedata.category(character).startswith("C") for character in comment
        ):
            raise LifecycleValidationError(
                "PUBLIC_KEY_VALIDATION_FAILED", "SSH key comment is invalid"
            )
    canonical_blob = base64.b64encode(blob).decode("ascii")
    canonical = f"{parts[0]} {canonical_blob}"
    if len(parts) == 3:
        canonical = f"{canonical} {parts[2]}"
    if canonical != public_key_line:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH public key is not canonical"
        )
    content = f"{canonical}\n".encode()
    return {
        "key_type": parts[0],
        "fingerprint_sha256": _ssh_key_fingerprint(canonical),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "content": content,
    }


def _open_staging_directory() -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(SSH_KEY_STAGING_ROOT, flags)
        directory_stat = os.fstat(descriptor)
    except OSError as exc:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH key staging directory is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != SSH_KEY_STAGING_OWNER_UID
        or directory_stat.st_gid != SSH_KEY_STAGING_OWNER_GID
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH key staging directory metadata is invalid"
        )
    return descriptor


def _read_staging_component(name: str, maximum_size: int) -> bytes:
    directory = _open_staging_directory()
    try:
        try:
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except OSError as exc:
            raise LifecycleValidationError(
                "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key record is unavailable"
            ) from exc
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != SSH_KEY_STAGING_OWNER_UID
            or before.st_gid != SSH_KEY_STAGING_OWNER_GID
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 0 < before.st_size <= maximum_size
        ):
            raise LifecycleValidationError(
                "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key file metadata is invalid"
            )
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=directory)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise LifecycleValidationError(
                    "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key file changed during open"
                )
            content = os.read(descriptor, maximum_size + 1)
        finally:
            os.close(descriptor)
        if not 0 < len(content) <= maximum_size:
            raise LifecycleValidationError(
                "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key file has an invalid size"
            )
        return content
    except LifecycleValidationError:
        raise
    except OSError as exc:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key file could not be read safely"
        ) from exc
    finally:
        os.close(directory)


def _read_approved_ssh_key_record(record_id: str) -> dict[str, Any]:
    """Read one UUID-named, root-controlled key without following links."""
    try:
        uuid_value = str(uuid.UUID(record_id))
        if uuid_value != record_id:
            raise ValueError("non-canonical UUID")
        content = _read_staging_component(f"{record_id}.pub", MAX_SSH_KEY_FILE_BYTES)
        metadata_content = _read_staging_component(f"{record_id}.meta.json", 4096)
        decoded = content.decode("utf-8", errors="strict")
        metadata = json.loads(metadata_content.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key metadata is invalid"
        ) from exc
    if not isinstance(metadata, dict):
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key metadata is invalid"
        )
    if not decoded.endswith("\n") or "\n" in decoded[:-1] or "\r" in decoded:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "one canonical public key is required per record"
        )
    validated = _validate_public_key_content(decoded[:-1])
    expected = {
        "record_id": record_id,
        "key_type": validated["key_type"],
        "fingerprint_sha256": validated["fingerprint_sha256"],
        "content_sha256": validated["content_sha256"],
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key metadata does not match key bytes"
        )
    try:
        operation_id = str(uuid.UUID(str(metadata.get("operation_id", ""))))
        managed_user_id = str(uuid.UUID(str(metadata.get("managed_user_id", ""))))
    except ValueError as exc:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key association metadata is invalid"
        ) from exc
    username = metadata.get("username")
    if (
        metadata.get("scope") not in {"HOST", "CONTAINER", "BOTH"}
        or operation_id != metadata.get("operation_id")
        or managed_user_id != metadata.get("managed_user_id")
        or (
            username is not None
            and (
                not isinstance(username, str)
                or re.fullmatch(r"[a-z][a-z0-9-]{0,31}", username) is None
                or username in {"root", "origin-al", "codexops", "nobody"}
            )
        )
    ):
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key association metadata is invalid"
        )
    return {
        "record_id": record_id,
        "key_type": validated["key_type"],
        "fingerprint_sha256": validated["fingerprint_sha256"],
        "content_sha256": validated["content_sha256"],
        "scope": metadata["scope"],
        "operation_id": operation_id,
        "managed_user_id": managed_user_id,
        "username": username,
        "size_bytes": len(content),
    }


def validate_approved_ssh_key_records(record_ids: list[str]) -> list[dict[str, Any]]:
    if not 1 <= len(record_ids) <= MAX_APPROVED_SSH_KEYS:
        raise LifecycleValidationError(
            "PUBLIC_KEY_REQUIRED_FOR_ACTIVATION", "approved SSH key records are required"
        )
    results = [_read_approved_ssh_key_record(record_id) for record_id in record_ids]
    fingerprints = [str(item["fingerprint_sha256"]) for item in results]
    if len(set(fingerprints)) != len(fingerprints):
        raise LifecycleValidationError("PUBLIC_KEY_DUPLICATE", "duplicate SSH key fingerprint")
    return results


def _component_exists(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LifecycleValidationError(
            "SSH_KEY_STAGING_FAILED", "SSH key staging component could not be inspected"
        ) from exc


def _write_staging_temp(directory: int, name: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_ssh_key_record(request: WorkerRequest, payload: dict[str, Any]) -> dict[str, Any]:
    record_id = payload["record_id"]
    if (
        request.requested_by != request.approved_by
        or request.idempotency_key != f"ssh-key-enroll:{record_id}"
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "SSH_KEY_PREPARE_APPROVAL_REJECTED",
                "message": "SSH key preparation is not bound to self-service confirmation",
            },
        }
    try:
        validated = _validate_public_key_content(payload["public_key"])
        for supplied, observed in (
            (payload["key_type"], validated["key_type"]),
            (payload["fingerprint_sha256"], validated["fingerprint_sha256"]),
            (payload["content_sha256"], validated["content_sha256"]),
        ):
            if supplied != observed:
                raise LifecycleValidationError(
                    "SSH_KEY_PREPARE_MISMATCH", "SSH key metadata differs from key bytes"
                )
        metadata = {
            "record_id": record_id,
            "operation_id": payload["operation_id"],
            "managed_user_id": payload["managed_user_id"],
            "username": payload["username"],
            "key_type": validated["key_type"],
            "fingerprint_sha256": validated["fingerprint_sha256"],
            "content_sha256": validated["content_sha256"],
            "scope": payload["scope"],
        }
        metadata_content = (
            json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        key_name = f"{record_id}.pub"
        metadata_name = f"{record_id}.meta.json"
        directory = _open_staging_directory()
        key_temp = f".{record_id}.{secrets.token_hex(8)}.pub.tmp"
        metadata_temp = f".{record_id}.{secrets.token_hex(8)}.meta.tmp"
        key_installed = False
        try:
            fcntl.flock(directory, fcntl.LOCK_EX)
            key_exists = _component_exists(directory, key_name)
            metadata_exists = _component_exists(directory, metadata_name)
            if key_exists or metadata_exists:
                if not (key_exists and metadata_exists):
                    raise LifecycleValidationError(
                        "SSH_KEY_STAGING_CONFLICT", "incomplete SSH key staging record exists"
                    )
                existing = _read_approved_ssh_key_record(record_id)
                comparisons = {
                    "key_type": payload["key_type"],
                    "fingerprint_sha256": payload["fingerprint_sha256"],
                    "content_sha256": payload["content_sha256"],
                    "scope": payload["scope"],
                    "operation_id": payload["operation_id"],
                    "managed_user_id": payload["managed_user_id"],
                }
                if any(existing.get(key) != value for key, value in comparisons.items()):
                    raise LifecycleValidationError(
                        "SSH_KEY_STAGING_CONFLICT", "existing SSH key staging record differs"
                    )
                if existing.get("username") not in {None, payload["username"]}:
                    raise LifecycleValidationError(
                        "SSH_KEY_STAGING_CONFLICT", "existing SSH key owner differs"
                    )
                return {
                    "status": "SUCCEEDED",
                    "handler": "ssh_key.prepare",
                    "idempotent_replay": True,
                    **existing,
                }
            try:
                _write_staging_temp(directory, key_temp, validated["content"])
                _write_staging_temp(directory, metadata_temp, metadata_content)
                os.replace(key_temp, key_name, src_dir_fd=directory, dst_dir_fd=directory)
                key_installed = True
                os.replace(metadata_temp, metadata_name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            except OSError as exc:
                if key_installed:
                    with suppress(OSError):
                        os.unlink(key_name, dir_fd=directory)
                raise LifecycleValidationError(
                    "SSH_KEY_STAGING_FAILED", "SSH key staging write failed"
                ) from exc
            finally:
                for temporary in (key_temp, metadata_temp):
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
        finally:
            os.close(directory)
        verified = _read_approved_ssh_key_record(record_id)
        return {
            "status": "SUCCEEDED",
            "handler": "ssh_key.prepare",
            "idempotent_replay": False,
            **verified,
        }
    except LifecycleValidationError as exc:
        return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}


def _discard_ssh_key_record(request: WorkerRequest, payload: dict[str, Any]) -> dict[str, Any]:
    record_id = payload["record_id"]
    if (
        request.requested_by != request.approved_by
        or request.idempotency_key != f"ssh-key-discard:{record_id}"
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "SSH_KEY_DISCARD_APPROVAL_REJECTED",
                "message": "SSH key discard is not bound to its enrollment record",
            },
        }
    key_name = f"{record_id}.pub"
    metadata_name = f"{record_id}.meta.json"
    try:
        directory = _open_staging_directory()
        try:
            fcntl.flock(directory, fcntl.LOCK_EX)
            key_exists = _component_exists(directory, key_name)
            metadata_exists = _component_exists(directory, metadata_name)
            if not key_exists and not metadata_exists:
                return {
                    "status": "SUCCEEDED",
                    "handler": "ssh_key.discard",
                    "record_id": record_id,
                    "removed": False,
                }
            if metadata_exists:
                metadata_raw = _read_staging_component(metadata_name, 4096)
                metadata = json.loads(metadata_raw.decode("utf-8", errors="strict"))
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("record_id") != record_id
                    or metadata.get("operation_id") != payload["operation_id"]
                    or metadata.get("content_sha256") != payload["content_sha256"]
                ):
                    raise LifecycleValidationError(
                        "SSH_KEY_DISCARD_REJECTED", "SSH key staging association does not match"
                    )
            if key_exists:
                key_content = _read_staging_component(key_name, MAX_SSH_KEY_FILE_BYTES)
                if hashlib.sha256(key_content).hexdigest() != payload["content_sha256"]:
                    raise LifecycleValidationError(
                        "SSH_KEY_DISCARD_REJECTED", "SSH key staging bytes do not match"
                    )
            for name, exists in ((key_name, key_exists), (metadata_name, metadata_exists)):
                if exists:
                    os.unlink(name, dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(directory)
        return {
            "status": "SUCCEEDED",
            "handler": "ssh_key.discard",
            "record_id": record_id,
            "removed": True,
        }
    except (LifecycleValidationError, UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        code = getattr(exc, "code", "SSH_KEY_DISCARD_REJECTED")
        return {"status": "ERROR", "error": {"code": code, "message": str(exc)[:255]}}


def build_user_stage_argv(payload: dict[str, Any]) -> list[str]:
    """Build the fixed future real-write argv; no public-key option exists."""
    return [
        SCRIPT_ALLOWLIST["h100-user-create"],
        "--stage",
        str(payload["username"]),
        str(payload["uid"]),
        str(payload["gid"]),
        str(payload["project_id"]),
        str(payload["ssh_port"]),
        str(payload["slurm_account"]),
        str(payload["slurm_qos"]),
        "--confirm-stage",
        str(payload["username"]),
    ]


def _activation_target_records(
    key_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    host_records = [record for record in key_records if record["scope"] in {"HOST", "BOTH"}]
    container_records = [
        record for record in key_records if record["scope"] in {"CONTAINER", "BOTH"}
    ]
    if not host_records or not container_records:
        raise LifecycleValidationError(
            "SSH_KEY_SCOPE_INCOMPLETE",
            "Activate requires approved key coverage for both host and container",
        )
    return host_records, container_records


def _activation_bundle_path(request_id: str, target: str) -> Path:
    try:
        canonical_request_id = str(uuid.UUID(request_id))
    except ValueError as exc:
        raise LifecycleValidationError(
            "ACTIVATE_BUNDLE_PATH_REJECTED", "Activate bundle request ID is invalid"
        ) from exc
    if canonical_request_id != request_id or target not in {"host", "container"}:
        raise LifecycleValidationError(
            "ACTIVATE_BUNDLE_PATH_REJECTED", "Activate bundle target is invalid"
        )
    return SSH_KEY_STAGING_ROOT / f"{canonical_request_id}.{target}.pub"


@contextmanager
def activation_key_bundles(
    request_id: str, key_records: list[dict[str, Any]]
) -> Iterator[tuple[Path, Path]]:
    """Build target-scoped, root-only bundles and remove them after one invocation."""
    host_records, container_records = _activation_target_records(key_records)
    target_records = {"host": host_records, "container": container_records}
    bundle_content: dict[str, bytes] = {}
    bundle_paths = {
        target: _activation_bundle_path(request_id, target) for target in target_records
    }
    for target, records in target_records.items():
        content = b"".join(
            _read_staging_component(f"{record['record_id']}.pub", MAX_SSH_KEY_FILE_BYTES)
            for record in records
        )
        if not 0 < len(content) <= MAX_SSH_KEY_FILE_BYTES:
            raise LifecycleValidationError(
                "PUBLIC_KEY_SIZE_INVALID", f"{target} SSH key bundle has an invalid size"
            )
        bundle_content[target] = content

    directory = _open_staging_directory()
    installed: list[str] = []
    temporary: list[str] = []
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        for target, path in bundle_paths.items():
            if _component_exists(directory, path.name):
                raise LifecycleValidationError(
                    "ACTIVATE_BUNDLE_CONFLICT", "Activate bundle already exists"
                )
            temp_name = f".{request_id}.{target}.{secrets.token_hex(8)}.tmp"
            temporary.append(temp_name)
            _write_staging_temp(directory, temp_name, bundle_content[target])
            os.replace(temp_name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            temporary.remove(temp_name)
            installed.append(path.name)
        os.fsync(directory)
        yield bundle_paths["host"], bundle_paths["container"]
    finally:
        for name in temporary + installed:
            with suppress(FileNotFoundError, OSError):
                os.unlink(name, dir_fd=directory)
        with suppress(OSError):
            os.fsync(directory)
        os.close(directory)


def build_user_activate_argv(
    username: str, host_key_file: Path, container_key_file: Path
) -> list[str]:
    """Build Activate argv only for Worker-owned target-scoped bundle paths."""
    if username != PILOT_USERNAME:
        raise LifecycleValidationError(
            "ACTIVATE_TARGET_REJECTED", "Activate target is outside the approved Pilot identity"
        )
    expected_names = {
        "host": host_key_file,
        "container": container_key_file,
    }
    for target, controlled_key_file in expected_names.items():
        name_match = re.fullmatch(
            rf"([0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-"
            rf"[0-9a-f]{{12}})\.{target}\.pub",
            controlled_key_file.name,
        )
        canonical_request_id = None
        if name_match is not None:
            with suppress(ValueError):
                canonical_request_id = str(uuid.UUID(name_match.group(1)))
        if (
            controlled_key_file.parent != SSH_KEY_STAGING_ROOT
            or name_match is None
            or canonical_request_id != name_match.group(1)
        ):
            raise LifecycleValidationError(
                "ARBITRARY_PATH_REJECTED",
                "Activate key bundle is outside the controlled staging root",
            )
    return [
        SCRIPT_ALLOWLIST["h100-user-create"],
        "--activate",
        username,
        "--host-public-key-file",
        str(host_key_file),
        "--container-public-key-file",
        str(container_key_file),
        "--confirm-activate",
        username,
    ]


def build_user_activate_rollback_argv(username: str) -> list[str]:
    """Build the single fail-closed recovery command for Portal-3E-FINAL."""
    if username != PILOT_USERNAME:
        raise LifecycleValidationError(
            "ACTIVATE_TARGET_REJECTED", "Activate rollback target is outside the approved Pilot"
        )
    return [
        SCRIPT_ALLOWLIST["h100-user-create"],
        "--rollback-activate",
        username,
        "--confirm-rollback-activate",
        username,
    ]


def _safe_file_lines(path: Path) -> list[str]:
    """Read one of the fixed platform metadata files, failing closed."""
    try:
        if path.is_symlink() or not path.is_file():
            return []
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if len(content) > 256 * 1024:
        return []
    return content.splitlines()


def _registry_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in _safe_file_lines(GPU_REGISTRY):
        fields = line.split()
        if len(fields) != 2 or not fields[1].isdigit():
            continue
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", fields[0]):
            continue
        entries.append({"username": fields[0], "uid": int(fields[1])})
    return entries


def _pilot_state_records() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    try:
        paths = sorted(PILOT_STATE_ROOT.glob("*.state"))
    except OSError:
        return records
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        values: dict[str, str] = {}
        for line in _safe_file_lines(path):
            key, separator, value = line.partition("=")
            if separator and re.fullmatch(r"[A-Z0-9_]{1,32}", key):
                values[key] = value[:256]
        if values:
            records.append(values)
    return records


def _used_identity_numbers() -> tuple[set[int], set[int]]:
    passwd_entries = pwd.getpwall()
    uids = {int(item.pw_uid) for item in passwd_entries}
    gids = {int(item.pw_gid) for item in passwd_entries}
    gids.update(int(item.gr_gid) for item in grp.getgrall())
    for entry in _registry_entries():
        uids.add(int(entry["uid"]))
    for record in _pilot_state_records():
        for key, target in (("UID", uids), ("GID", gids)):
            value = record.get(key, "")
            if value.isdigit():
                target.add(int(value))
    return uids, gids


def _ownership_conflict(identity_number: int) -> tuple[str, str | None]:
    """Return PASS/UNKNOWN and a bounded first conflicting path.

    The roots and find expression are constants; the candidate number is
    generated by this handler and is never accepted as an arbitrary path or
    command from the API.
    """
    roots = [str(path) for path in PILOT_SCAN_ROOTS if path.exists()]
    if not roots:
        return "PASS", None
    result = run_fixed(
        "find",
        [
            *roots,
            "-xdev",
            "(",
            "-uid",
            str(identity_number),
            "-o",
            "-gid",
            str(identity_number),
            ")",
            "-print",
            "-quit",
        ],
        timeout=25,
    )
    if not result.get("ok") and result.get("exit_code") not in {0, 1}:
        return "UNKNOWN", None
    first = str(result.get("stdout", "")).splitlines()
    return ("CONFLICT", first[0][:255]) if first else ("PASS", None)


def _candidate_uid_gid(
    excluded: set[int] | None = None,
) -> tuple[int | None, list[dict[str, str]], dict[str, str]]:
    used_uids, used_gids = _used_identity_numbers()
    used_uids.update(excluded or set())
    used_gids.update(excluded or set())
    rejected: list[dict[str, str]] = []
    checks = {
        "uid_range": f"{PILOT_UID_MIN}-{PILOT_UID_MAX}",
        "gid_range": f"{PILOT_UID_MIN}-{PILOT_UID_MAX}",
        "reservation": "PROPOSED — NOT RESERVED",
    }
    for value in range(PILOT_UID_FIRST, PILOT_UID_MAX + 1):
        if value in used_uids or value in used_gids:
            continue
        ownership_status, path = _ownership_conflict(value)
        if ownership_status == "UNKNOWN":
            checks["legacy_ownership_scan"] = "UNKNOWN"
            return None, rejected, checks
        if ownership_status == "CONFLICT":
            if len(rejected) < 8:
                rejected.append(
                    {
                        "candidate": str(value),
                        "reason": "legacy ownership exists",
                        "path": path or "redacted",
                    }
                )
            continue
        checks["legacy_ownership_scan"] = "PASS"
        return value, rejected, checks
    checks["legacy_ownership_scan"] = "NOT_AVAILABLE"
    return None, rejected, checks


def _used_project_ids() -> tuple[set[int], bool]:
    used: set[int] = set()
    for path in (PROJECTS_FILE, PROJID_FILE):
        for line in _safe_file_lines(path):
            left, separator, right = line.partition(":")
            if not separator:
                continue
            candidates = (left, right) if path == PROJID_FILE else (left,)
            for candidate in candidates:
                if candidate.strip().isdigit():
                    used.add(int(candidate.strip()))
    for record in _pilot_state_records():
        value = record.get("PROJECT_ID", "")
        if value.isdigit():
            used.add(int(value))
    quota = run_fixed(
        "xfs_quota",
        ["-x", "-c", "report -p -b -n", str(PILOT_DATA_ROOT.parent)],
        timeout=20,
    )
    if not quota.get("ok"):
        return used, False
    for line in str(quota.get("stdout", "")).splitlines():
        fields = line.split()
        if fields and re.fullmatch(r"#[0-9]+", fields[0]):
            used.add(int(fields[0][1:]))
    return used, True


def _candidate_project_id(excluded: set[int] | None = None) -> tuple[int | None, dict[str, str]]:
    used, quota_readable = _used_project_ids()
    used.update(excluded or set())
    if not quota_readable:
        return None, {
            "range": f"{PROJECT_ID_MIN}-{PROJECT_ID_MAX}",
            "reservation": "NOT_AVAILABLE",
            "source": "/etc/projects,/etc/projid,XFS quota report,platform state",
        }
    for value in range(PROJECT_ID_FIRST, PROJECT_ID_MAX + 1):
        if value not in used:
            return value, {
                "range": f"{PROJECT_ID_MIN}-{PROJECT_ID_MAX}",
                "reservation": "PROPOSED — NOT RESERVED",
                "source": "/etc/projects,/etc/projid,XFS quota report,platform state",
            }
    return None, {
        "range": f"{PROJECT_ID_MIN}-{PROJECT_ID_MAX}",
        "reservation": "NOT_AVAILABLE",
        "source": "/etc/projects,/etc/projid,XFS quota report,platform state",
    }


def _port_numbers_from_text(value: str) -> set[int]:
    ports: set[int] = set()
    for match in re.finditer(r":(\d{1,5})(?=(?:->|,|\s|$))", value):
        number = int(match.group(1))
        if 1 <= number <= 65535:
            ports.add(number)
    return ports


def _used_ssh_ports() -> tuple[set[int], bool]:
    result = run_fixed("ss", ["-H", "-lnt"], timeout=15)
    if not result.get("ok"):
        return set(), False
    ports = _port_numbers_from_text(str(result.get("stdout", "")))
    containers = containers_list()
    if containers.get("status") != "OK":
        return ports, False
    for item in containers.get("containers", []):
        if isinstance(item, dict):
            ports.update(_port_numbers_from_text(str(item.get("Ports") or "")))
    for record in _pilot_state_records():
        value = record.get("SSH_PORT", "")
        if value.isdigit():
            ports.add(int(value))
    return ports, True


def _candidate_ssh_port(excluded: set[int] | None = None) -> tuple[int | None, dict[str, str]]:
    used, readable = _used_ssh_ports()
    used.update(excluded or set())
    if not readable:
        return None, {
            "range": f"{PILOT_SSH_PORT_MIN}-{PILOT_SSH_PORT_MAX}",
            "reservation": "NOT_AVAILABLE",
            "source": "ss/docker/platform state",
        }
    for value in range(PILOT_SSH_PORT_FIRST, PILOT_SSH_PORT_MAX + 1):
        if value not in used:
            return value, {
                "range": f"{PILOT_SSH_PORT_MIN}-{PILOT_SSH_PORT_MAX}",
                "reservation": "PROPOSED — NOT RESERVED",
                "bind_address": PUBLIC_ACCESS_HOST,
                "source": "ss/docker/platform state",
            }
    return None, {
        "range": f"{PILOT_SSH_PORT_MIN}-{PILOT_SSH_PORT_MAX}",
        "reservation": "NOT_AVAILABLE",
        "source": "ss/docker/platform state",
    }


def _group_names(username: str, gid: int) -> list[str]:
    names: list[str] = []
    try:
        gids = os.getgrouplist(username, gid)
    except OSError, KeyError:
        gids = [gid]
    for group in grp.getgrall():
        if group.gr_gid in gids:
            names.append(group.gr_name)
    return sorted(set(names))


def _assoc_exists(username: str) -> bool | None:
    result = run_fixed(
        "sacctmgr",
        ["-n", "-P", "show", "assoc", "where", f"User={username}", "format=User,Account"],
        timeout=20,
    )
    if not result.get("ok"):
        return None
    return any(line.split("|", 1)[0] == username for line in result.get("stdout", "").splitlines())


def _slurm_plan_checks(username: str) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    node = slurm_node()
    node_ok = node.get("status") == "OK" and all(
        "DRAIN" in str(item.get("state", "")).upper() for item in node.get("nodes", [])
    )
    checks.append(
        {
            "check": "slurm_node_drained",
            "status": "PASS" if node_ok else "FAIL",
            "detail": "节点保持 DRAIN；Portal-3A 不执行 RESUME"
            if node_ok
            else "节点状态无法证明为 DRAIN",
        }
    )
    jobs = slurm_jobs()
    queue_empty = jobs.get("status") == "OK" and not jobs.get("jobs")
    checks.append(
        {
            "check": "slurm_queue_empty",
            "status": "PASS" if queue_empty else "FAIL",
            "detail": "当前没有运行或排队作业" if queue_empty else "作业存在，计划不可执行",
        }
    )
    accounts = slurm_accounts()
    account_ok = accounts.get("status") in {"OK", "PARTIAL"} and any(
        str(item.get("account")) == "company" for item in accounts.get("accounts", [])
    )
    checks.append(
        {
            "check": "slurm_account_company",
            "status": "PASS" if account_ok else "FAIL",
            "detail": "company account 已存在" if account_ok else "company account 不可用",
        }
    )
    qos_rows = [item for item in accounts.get("qos", []) if item.get("name") == "general"]
    qos_ok = bool(qos_rows) and "gres/gpu=1" in str(qos_rows[0].get("max_tres_per_user", ""))
    checks.append(
        {
            "check": "slurm_qos_general_max_gpu",
            "status": "PASS" if qos_ok else "FAIL",
            "detail": "general QOS 的 MaxTRESPerUser 为 gres/gpu=1"
            if qos_ok
            else "general QOS GPU 上限无法证明为 1",
        }
    )
    assoc = _assoc_exists(username)
    checks.append(
        {
            "check": "slurm_association_absent",
            "status": "PASS" if assoc is False else "UNKNOWN" if assoc is None else "FAIL",
            "detail": "目标用户尚无 Slurm association"
            if assoc is False
            else "无法读取 association"
            if assoc is None
            else "目标用户已有 Slurm association",
        }
    )
    return checks


def _check(name: str, passed: bool, success: str, failure: str) -> dict[str, str]:
    return {
        "check": name,
        "status": "PASS" if passed else "FAIL",
        "detail": success if passed else failure,
    }


def _target_container_absent(name: str) -> tuple[bool, str]:
    inspected = run_fixed("docker", ["container", "inspect", name], timeout=15)
    if inspected.get("ok"):
        return False, "目标开发容器已存在"
    error = str(inspected.get("stderr", ""))
    absent = "No such container" in error or "No such object" in error
    return (True, "目标开发容器不存在") if absent else (False, "Docker 状态无法安全确认")


def _compute_slurm_checks(username: str) -> list[dict[str, str]]:
    """Read-only Production checks; an IDLE node must not be drained for planning."""
    checks: list[dict[str, str]] = []
    nodes = slurm_node()
    node_rows = nodes.get("nodes", []) if nodes.get("status") == "OK" else []
    node_available = bool(node_rows) and all(
        not any(marker in str(item.get("state", "")).upper() for marker in ("DOWN", "DRAIN"))
        for item in node_rows
        if isinstance(item, dict)
    )
    checks.append(
        _check(
            "slurm_scheduler_available",
            node_available,
            "Slurm 节点可调度；dry-run 不修改节点状态",
            "Slurm 节点不可用或处于 DOWN/DRAIN",
        )
    )
    accounts = slurm_accounts()
    account_ok = accounts.get("status") in {"OK", "PARTIAL"} and any(
        str(item.get("account")) == "company" for item in accounts.get("accounts", [])
    )
    checks.append(
        _check(
            "slurm_account_company",
            account_ok,
            "company account 已存在",
            "company account 不可用",
        )
    )
    qos_rows = [item for item in accounts.get("qos", []) if item.get("name") == "general"]
    qos_ok = bool(qos_rows) and "gres/gpu=1" in str(qos_rows[0].get("max_tres_per_user", ""))
    checks.append(
        _check(
            "slurm_qos_general_max_gpu",
            qos_ok,
            "general QOS 强制每用户最多 1 张 GPU",
            "general QOS 无法证明 MaxTRESPerUser=gres/gpu=1",
        )
    )
    assoc = _assoc_exists(username)
    checks.append(
        _check(
            "slurm_association_absent",
            assoc is False,
            "目标用户尚无 Slurm association",
            "目标 association 已存在或状态无法确认",
        )
    )
    return checks


def _xfs_project_quota_capable() -> bool:
    state = run_fixed("xfs_quota", ["-x", "-c", "state", str(PILOT_DATA_ROOT.parent)], timeout=20)
    output = str(state.get("stdout", ""))
    project_state = output.partition("Project quota state")[2]
    return bool(
        state.get("ok")
        and project_state
        and "Accounting: ON" in project_state
        and "Enforcement: ON" in project_state
    )


def _standard_dev_image_identity() -> dict[str, Any]:
    """Resolve the accepted base strictly through its root-owned local OCI artifact."""
    return local_image_contract()


def _standard_dev_image_available() -> bool:
    """Compatibility predicate backed by the complete Stage image validator."""
    return bool(_standard_dev_image_identity().get("status") == "PASS")


def _compute_platform_checks(
    username: str, *, image_identity: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    data_parent_ok = PILOT_DATA_ROOT.exists() and PILOT_DATA_ROOT.is_dir()
    checks.append(
        _check(
            "storage_parent_available",
            data_parent_ok,
            "/srv/gpu-platform/users 存在；未创建用户目录",
            "标准用户存储父目录不可用",
        )
    )
    checks.append(
        _check(
            "xfs_project_quota_capability",
            _xfs_project_quota_capable(),
            "XFS project quota accounting/enforcement 已启用",
            "XFS project quota capability 无法证明",
        )
    )
    checks.extend(_compute_slurm_checks(username))
    global_dropins = [
        Path("/etc/systemd/system/user.slice.d/50-h100-gpu-isolation.conf"),
        Path("/etc/systemd/system/user-.slice.d/50-h100-gpu-isolation.conf"),
    ]
    checks.append(
        _check(
            "gpu_isolation_no_global_dropin",
            not any(path.exists() for path in global_dropins),
            "GPU 隔离保持精确 per-UID 策略，无全局 user slice 放行",
            "发现禁止的全局 GPU policy drop-in",
        )
    )
    integrity = script_integrity()
    lifecycle_ok = all(
        integrity.get(name, {}).get("integrity_ok", False) for name in STAGE_REQUIRED_SCRIPTS
    )
    checks.append(
        _check(
            "gpu_isolation_lifecycle_integrity",
            lifecycle_ok,
            "固定用户/Container/GPU 隔离生命周期脚本完整性通过",
            "固定生命周期脚本完整性失败",
        )
    )
    timer_enabled = run_fixed(
        "systemctl", ["is-enabled", "h100-gpu-bypass-guard.timer"], timeout=10
    )
    timer_active = run_fixed("systemctl", ["is-active", "h100-gpu-bypass-guard.timer"], timeout=10)
    guard_ready = (
        str(timer_enabled.get("stdout", "")).strip() == "enabled"
        and str(timer_active.get("stdout", "")).strip() == "active"
    )
    checks.append(
        _check(
            "gpu_bypass_guard_readiness",
            guard_ready,
            "Guard timer enabled/active；未直接执行 Guard 主程序",
            "Guard timer readiness 无法证明",
        )
    )
    observed_image = _standard_dev_image_identity() if image_identity is None else image_identity
    checks.append(
        _check(
            "standard_container_image_available",
            observed_image.get("status") == "PASS",
            "已验收 Pilot 开发容器镜像身份、标签与 build user 均通过",
            f"标准开发容器镜像校验失败：{observed_image.get('failure_code', 'UNKNOWN')}",
        )
    )
    return checks


def _compute_target_checks(username: str, container_name: str) -> list[dict[str, str]]:
    user_absent = group_absent = False
    try:
        pwd.getpwnam(username)
    except KeyError:
        user_absent = True
    try:
        grp.getgrnam(username)
    except KeyError:
        group_absent = True
    container_absent, container_detail = _target_container_absent(container_name)
    stale_paths = (
        MANAGED_HOME_ROOT / username,
        PILOT_DATA_ROOT / username,
        PILOT_STATE_ROOT / f"{username}.state",
        PILOT_COMPOSE_ROOT / username,
    )
    stale_absent = not any(path.exists() for path in stale_paths)
    exact_dropins = list(Path("/etc/systemd/system").glob("user-*.slice.d"))
    # The target UID is not known in the allocator pass, so only target-name
    # artifacts are checked here; exact UID policy is checked in final dry-run.
    return [
        _check(
            "linux_username_available",
            user_absent and group_absent,
            "目标 Linux 用户和同名组不存在",
            "目标 Linux 用户或组已存在",
        ),
        _check(
            "container_name_available",
            container_absent,
            container_detail,
            container_detail,
        ),
        _check(
            "target_platform_paths_absent",
            stale_absent,
            "目标 home/data/state/compose 尚未创建",
            "发现目标用户的宿主资源遗留",
        ),
        _check(
            "host_access_policy",
            True,
            "计划固定为 nologin、password LOCKED、Host SSH DISABLED",
            "Host access policy mismatch",
        ),
        _check(
            "target_name_does_not_select_systemd_policy",
            all(username not in path.name for path in exact_dropins),
            "未发现按用户名伪装的 systemd GPU policy",
            "发现目标用户名相关的异常 systemd policy",
        ),
    ]


def _compute_conflicts(checks: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"code": item["check"].upper(), "message": item["detail"]}
        for item in checks
        if item.get("status") != "PASS"
    ]


def _compute_provision_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Allocate candidates using host truth plus Portal DB reservation exclusions."""
    username = str(payload["username"])
    container_name = f"gpu-dev-{username}"
    checks = _compute_target_checks(username, container_name)
    checks.extend(_compute_platform_checks(username))
    if container_name in set(payload["reserved_container_names"]):
        checks.append(
            _check(
                "container_name_reservation_available",
                False,
                "容器名 reservation 可用",
                "容器名已被另一个 Provision Plan reservation 占用",
            )
        )
    else:
        checks.append(
            _check(
                "container_name_reservation_available",
                True,
                "容器名未被其他 Provision Plan reservation 占用",
                "容器名 reservation 冲突",
            )
        )
    excluded_identity = set(payload["reserved_uids"]) | set(payload["reserved_gids"])
    uid, rejections, uid_checks = _candidate_uid_gid(excluded_identity)
    project_id, project_checks = _candidate_project_id(set(payload["reserved_project_ids"]))
    ssh_port, port_checks = _candidate_ssh_port(set(payload["reserved_ssh_ports"]))
    checks.extend(
        [
            _check(
                "uid_gid_candidate",
                uid is not None,
                "UID/GID 候选同时避开宿主与 Portal reservations",
                "没有可用 UID/GID 候选",
            ),
            _check(
                "project_id_candidate",
                project_id is not None,
                "Project ID 候选同时避开 XFS 映射与 Portal reservations",
                "没有可用 Project ID 候选",
            ),
            _check(
                "ssh_port_candidate",
                ssh_port is not None,
                "SSH Port 候选同时避开监听、Docker 与 Portal reservations",
                "没有可用 SSH Port 候选",
            ),
        ]
    )
    conflicts = _compute_conflicts(checks)
    ready = not conflicts and uid is not None and project_id is not None and ssh_port is not None
    return {
        "status": "DRY_RUN",
        "handler": "compute.provision.plan",
        "plan_status": "READY" if ready else "CONFLICT",
        "execution_enabled": False,
        "request_id": payload["request_id"],
        "portal_account_id": payload["portal_account_id"],
        "username": username,
        "proposed_uid": uid,
        "proposed_gid": uid,
        "proposed_project_id": project_id,
        "proposed_ssh_port": ssh_port,
        "proposed_container_name": container_name,
        "proposed_storage_bytes": STANDARD_COMPUTE_STORAGE_BYTES,
        "proposed_container_profile": STANDARD_COMPUTE_PROFILE,
        "proposed_container": {
            "cpus": 8,
            "memory_gb": 32,
            "pids_limit": 4096,
            "gpu": "NONE",
        },
        "proposed_slurm": {
            "account": "company",
            "qos": "general",
            "max_gpu": payload["requested_gpu_max"],
        },
        "proposed_host_access": {
            "ssh": "DISABLED",
            "shell": "/usr/sbin/nologin",
            "password": "LOCKED",
        },
        "proposed_lease": {
            "duration_seconds": STANDARD_COMPUTE_LEASE_SECONDS,
            "state": "NOT_STARTED",
            "starts_at": None,
            "expires_at": None,
        },
        "validation_results": checks,
        "candidate_rejections": rejections,
        "allocator_sources": {
            "uid_gid": uid_checks,
            "project_id": project_checks,
            "ssh_port": port_checks,
            "portal_reservations_checked": True,
        },
        "conflicts": conflicts,
        "infrastructure_side_effects": "NONE",
    }


def _compute_exact_resource_checks(payload: dict[str, Any]) -> list[dict[str, str]]:
    uid = int(payload["uid"])
    gid = int(payload["gid"])
    used_uids, used_gids = _used_identity_numbers()
    ownership_status, _path = _ownership_conflict(uid)
    used_project_ids, project_quota_readable = _used_project_ids()
    project_available = (
        project_quota_readable and int(payload["project_id"]) not in used_project_ids
    )
    used_ports, ports_readable = _used_ssh_ports()
    port_available = ports_readable and int(payload["ssh_port"]) not in used_ports
    dropin = Path(f"/etc/systemd/system/user-{uid}.slice.d/{GPU_DROPIN_NAME}")
    return [
        _check(
            "reserved_uid_gid_still_available",
            uid not in used_uids and gid not in used_gids,
            "Reserved UID/GID 仍未出现在宿主身份库或平台 state",
            "Reserved UID/GID 已被占用",
        ),
        _check(
            "reserved_uid_gid_ownership_absent",
            ownership_status == "PASS",
            "Reserved UID/GID 未发现 legacy ownership",
            "Reserved UID/GID ownership 不安全",
        ),
        _check(
            "reserved_project_id_still_available",
            project_available,
            "Reserved Project ID 仍未写入 XFS mappings",
            "Reserved Project ID 已被占用",
        ),
        _check(
            "reserved_ssh_port_still_available",
            port_available,
            "Reserved SSH Port 仍未监听且未被容器使用",
            "Reserved SSH Port 已占用或监听状态不可读",
        ),
        _check(
            "per_uid_gpu_policy_absent",
            not dropin.exists(),
            "目标 per-UID GPU policy 尚未创建",
            "目标 per-UID GPU policy 已存在",
        ),
    ]


def _compute_stage_argv(payload: dict[str, Any]) -> list[str]:
    """Build the only host argv accepted by multi-user compute Stage."""
    argv: list[str] = []
    for _role, source, value in COMPUTE_STAGE_ARGV_BINDINGS:
        argv.append(value if source == "literal" else str(payload[value]))
    return argv


def _stage_contract_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compute_stage_contract(
    payload: dict[str, Any],
    *,
    integrity: dict[str, Any] | None = None,
    image_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and identify the deployed Stage artifact without exposing argv values."""
    observed_integrity = script_integrity() if integrity is None else integrity
    handler = observed_integrity.get(COMPUTE_STAGE_HANDLER_IDENTITY, {})
    handler_sha256 = handler.get("sha256")
    execution_artifacts: dict[str, str] = {}
    execution_artifacts_valid = True
    for name in sorted(COMPUTE_STAGE_REQUIRED_SCRIPTS):
        artifact = observed_integrity.get(name, {})
        artifact_sha256 = artifact.get("sha256")
        artifact_valid = bool(
            artifact.get("integrity_ok") is True
            and isinstance(artifact_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", artifact_sha256)
        )
        execution_artifacts[name] = artifact_sha256 if artifact_valid else "UNAVAILABLE"
        execution_artifacts_valid = execution_artifacts_valid and artifact_valid
    handler_valid = bool(
        SCRIPT_ALLOWLIST.get(COMPUTE_STAGE_HANDLER_IDENTITY) == COMPUTE_STAGE_HANDLER_PATH
        and handler.get("integrity_ok") is True
        and isinstance(handler_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", handler_sha256)
    )

    contract_payload = dict(payload)
    contract_payload.setdefault("dry_run_operation_id", "00000000-0000-0000-0000-000000000000")
    argv = _compute_stage_argv(contract_payload)
    expected_argv = [
        value if source == "literal" else str(contract_payload[value])
        for _role, source, value in COMPUTE_STAGE_ARGV_BINDINGS
    ]
    shape_valid = len(argv) == len(COMPUTE_STAGE_ARGV_BINDINGS)
    bindings_valid = shape_valid and argv == expected_argv
    argument_14_valid = shape_valid and argv[14] == "--confirm-stage"
    username = str(payload["username"])
    argument_15_valid = (
        shape_valid and argv[2] == username and argv[15] == username and argv[2] == argv[15]
    )
    confirmation_valid = argument_14_valid and argument_15_valid
    image_contract = (
        _standard_dev_image_identity() if image_identity is None else dict(image_identity)
    )
    image_valid = bool(
        image_contract.get("status") == "PASS"
        and image_contract.get("validator_version") == COMPUTE_STAGE_IMAGE_VALIDATOR_VERSION
        and image_contract.get("source_type") == "LOCAL_OCI_LAYOUT"
        and isinstance(image_contract.get("canonical_local_image_identity"), str)
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            str(image_contract.get("canonical_local_image_identity")),
        )
        and isinstance(image_contract.get("artifact_path"), str)
        and re.fullmatch(
            r"/srv/gpu-platform/artifacts/oci/standard-dev-base/[0-9a-f]{64}/layout",
            str(image_contract.get("artifact_path")),
        )
        and isinstance(image_contract.get("manifest_digest"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(image_contract.get("manifest_digest")))
        and str(image_contract.get("artifact_path")).endswith(
            f"/{str(image_contract.get('manifest_digest')).removeprefix('sha256:')}/layout"
        )
        and image_contract.get("platform") == "linux/amd64"
        and image_contract.get("effective_user") == "root"
        and isinstance(image_contract.get("approved_deployment_version"), str)
        and re.fullmatch(r"[0-9a-f]{40}", str(image_contract.get("approved_deployment_version")))
        and image_contract.get("failure_code") is None
    )

    binding_identity = [
        {
            "argv_index": index,
            "shell_position": index if index else None,
            "semantic_role": role,
            "binding_source": source,
            "binding_name": value,
        }
        for index, (role, source, value) in enumerate(COMPUTE_STAGE_ARGV_BINDINGS)
    ]
    argv_contract_sha256 = _stage_contract_sha256(
        {
            "version": COMPUTE_STAGE_ARGV_CONTRACT_VERSION,
            "bindings": binding_identity,
        }
    )
    validator_sha256 = _stage_contract_sha256(
        {
            "version": COMPUTE_STAGE_CONFIRMATION_VALIDATOR_VERSION,
            "rules": [
                "ARGV_LENGTH_EQUALS_CONTRACT",
                "ARGV_VALUES_EQUAL_DECLARED_BINDINGS",
                "SHELL_ARGUMENT_14_EQUALS_CONFIRMATION_FLAG",
                "SHELL_ARGUMENT_15_EQUALS_TARGET_USERNAME",
                "SHELL_ARGUMENT_2_EQUALS_ARGUMENT_15",
            ],
        }
    )
    contract_sha256 = _stage_contract_sha256(
        {
            "handler_identity": COMPUTE_STAGE_HANDLER_IDENTITY,
            "handler_path": COMPUTE_STAGE_HANDLER_PATH,
            "handler_sha256": handler_sha256 if handler_valid else "UNAVAILABLE",
            "execution_artifacts": execution_artifacts,
            "argv_contract_version": COMPUTE_STAGE_ARGV_CONTRACT_VERSION,
            "argv_contract_sha256": argv_contract_sha256,
            "confirmation_validator_version": COMPUTE_STAGE_CONFIRMATION_VALIDATOR_VERSION,
            "confirmation_validator_sha256": validator_sha256,
            "image_validator_version": image_contract.get("validator_version"),
            "canonical_local_image_identity": image_contract.get("canonical_local_image_identity"),
        }
    )
    passed = (
        handler_valid
        and execution_artifacts_valid
        and shape_valid
        and bindings_valid
        and confirmation_valid
        and image_valid
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "contract_sha256": contract_sha256,
        "handler": {
            "identity": COMPUTE_STAGE_HANDLER_IDENTITY,
            "deployed_path": COMPUTE_STAGE_HANDLER_PATH,
            "sha256": handler_sha256,
            "integrity_status": "PASS" if handler_valid else "FAIL",
        },
        "argv_contract": {
            "version": COMPUTE_STAGE_ARGV_CONTRACT_VERSION,
            "sha256": argv_contract_sha256,
            "shape_status": "PASS" if shape_valid and bindings_valid else "FAIL",
            "shell_argument_count": max(len(argv) - 1, 0),
            "expected_shell_argument_count": len(COMPUTE_STAGE_ARGV_BINDINGS) - 1,
            "multi_digit_position_status": (
                "PASS" if shape_valid and bindings_valid and confirmation_valid else "FAIL"
            ),
            "argument_14": {
                "index": 14,
                "semantic_role": "EXPLICIT_STAGE_CONFIRMATION_FLAG",
                "binding_status": "VALID" if argument_14_valid else "INVALID",
            },
            "argument_15": {
                "index": 15,
                "semantic_role": "CONFIRMED_TARGET_USERNAME",
                "binding_status": "VALID" if argument_15_valid else "INVALID",
            },
        },
        "confirmation_gate": {
            "identity": "EXPLICIT_STAGE_CONFIRMATION_GATE",
            "validator_version": COMPUTE_STAGE_CONFIRMATION_VALIDATOR_VERSION,
            "validator_sha256": validator_sha256,
            "status": "PASS" if confirmation_valid else "FAIL",
        },
        "image_contract": image_contract,
    }


def _compute_provision_dry_run(
    payload: dict[str, Any],
    *,
    image_identity: dict[str, Any] | None = None,
    integrity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    username = str(payload["username"])
    observed_image = _standard_dev_image_identity() if image_identity is None else image_identity
    checks = _compute_target_checks(username, str(payload["container_name"]))
    checks.extend(_compute_exact_resource_checks(payload))
    checks.extend(_compute_platform_checks(username, image_identity=observed_image))
    stage_contract = _compute_stage_contract(
        payload,
        integrity=integrity,
        image_identity=observed_image,
    )
    checks.extend(
        [
            _check(
                "lease_not_started",
                payload["lease_state"] == "NOT_STARTED",
                "Lease starts_at/expires_at 保持 NULL，未开始计时",
                "Lease 被提前启动",
            ),
            _check(
                "provision_execution_gate",
                payload["execution_enabled"] is False,
                "execution_enabled=false；真实 Provision 不可达",
                "真实 Provision gate 意外开启",
            ),
            _check(
                "container_profile_gpu_contract",
                payload["container_gpu"]
                == (1 if payload["container_profile"] == GPU_DEVELOPMENT_PROFILE else 0),
                "开发容器配置与固定 CPU/GPU Profile 一致",
                "开发容器 Profile 与 GPU 计数不一致",
            ),
            _check(
                "stage_handler_contract",
                stage_contract["status"] == "PASS",
                "Deployed Stage handler、argv contract 与 validator 已绑定",
                "Deployed Stage handler contract 无法验证",
            ),
            _check(
                "explicit_stage_confirmation_gate",
                stage_contract["confirmation_gate"]["status"] == "PASS",
                "Stage argument 14/15 显式确认绑定有效",
                "Stage argument 14/15 显式确认绑定无效",
            ),
        ]
    )
    conflicts = _compute_conflicts(checks)
    return {
        "status": "DRY_RUN",
        "handler": "compute.provision.dry_run",
        "dry_run_status": (
            "CONTRACT_INCOMPLETE"
            if stage_contract["status"] != "PASS"
            else "READY_FOR_PROVISION"
            if not conflicts
            else "CONFLICT"
        ),
        "execution_enabled": False,
        "request_id": payload["request_id"],
        "plan_id": payload["plan_id"],
        "portal_account_id": payload["portal_account_id"],
        "username": username,
        "validation_results": checks,
        "conflicts": conflicts,
        "stage_contract": stage_contract,
        "resource_writes": {
            "linux_user": False,
            "container": False,
            "xfs_quota": False,
            "slurm_association": False,
            "gpu_policy": False,
            "lease": False,
        },
        "infrastructure_side_effects": "NONE",
        "next_gate": "ADMINISTRATOR_PROVISION_APPROVAL_REQUIRED",
    }


def _compute_stage_state(payload: dict[str, Any]) -> dict[str, str]:
    path = PILOT_STATE_ROOT / f"{payload['username']}.state"
    values: dict[str, str] = {}
    for line in _safe_file_lines(path):
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z0-9_]{1,32}", key):
            values[key] = value[:256]
    expected = {
        "VERSION": "4",
        "STATUS": "STAGED",
        "USERNAME": str(payload["username"]),
        "UID": str(payload["uid"]),
        "GID": str(payload["gid"]),
        "PROJECT_ID": str(payload["project_id"]),
        "SSH_PORT": str(payload["ssh_port"]),
        "SLURM_ACCOUNT": str(payload["slurm_account"]),
        "SLURM_QOS": str(payload["slurm_qos"]),
        "MAX_GPUS": str(payload["gpu_max"]),
        "REQUEST_ID": str(payload["request_id"]),
        "PLAN_ID": str(payload["plan_id"]),
        "DRY_RUN_OPERATION_ID": str(payload["dry_run_operation_id"]),
        "DEVELOPMENT_PROFILE": str(
            payload.get("container_profile") or payload.get("development_profile")
        ),
        "WORKSPACE_PATH": str(workspace_path(int(payload["uid"]))),
        "SSH_KEY_STATE": "REQUIRED_BEFORE_ACTIVATION",
        "LEASE_STATE": "NOT_STARTED",
        "LEASE_START": "",
        "LEASE_EXPIRES": "",
    }
    mismatches = sorted(key for key, value in expected.items() if values.get(key) != value)
    if mismatches:
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED",
            f"staged lifecycle state mismatch: {','.join(mismatches)}",
        )
    return values


def _verified_guard_metrics_for_user(username: str, uid: int) -> dict[str, Any]:
    content = _read_guard_metric_file()
    scalar_metrics: dict[str, int] = {}
    timestamp: int | None = None
    user_success: int | None = None
    scalar_pattern = re.compile(r"^(h100_gpu_bypass_guard_[a-z_]+) ([0-9]+)$")
    user_pattern = re.compile(
        rf'^h100_gpu_user_isolation_success\{{username="{re.escape(username)}",uid="{uid}"\}} ([01])$'
    )
    for line in content.splitlines():
        scalar_match = scalar_pattern.fullmatch(line)
        if scalar_match:
            value = int(scalar_match.group(2))
            if scalar_match.group(1) == "h100_gpu_bypass_guard_timestamp_seconds":
                timestamp = value
            else:
                scalar_metrics[scalar_match.group(1)] = value
        user_match = user_pattern.fullmatch(line)
        if user_match:
            user_success = int(user_match.group(1))
    managed = scalar_metrics.get("h100_gpu_bypass_guard_managed_users")
    verified = scalar_metrics.get("h100_gpu_bypass_guard_users_verified")
    now = int(time.time())
    if not (
        scalar_metrics.get("h100_gpu_bypass_guard_last_success") == 1
        and isinstance(managed, int)
        and managed >= 1
        and verified == managed
        and scalar_metrics.get("h100_gpu_bypass_guard_policy_errors") == 0
        and scalar_metrics.get("h100_gpu_bypass_guard_device_open_failures") == 0
        and scalar_metrics.get("h100_gpu_bypass_guard_cuda_context_failures") == 0
        and scalar_metrics.get("h100_gpu_bypass_guard_slurm_constrain_devices") == 1
        and scalar_metrics.get("h100_gpu_bypass_guard_nvidia_gpu_count") == 4
        and scalar_metrics.get("h100_gpu_bypass_guard_slurm_gpu_count") == 4
        and user_success == 1
        and timestamp is not None
        and timestamp <= now + 60
        and now - timestamp <= GUARD_METRIC_MAX_AGE_SECONDS
    ):
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED",
            "GPU bypass Guard metrics do not prove all managed identities",
        )
    return {
        "status": "PASSING",
        "managed_users": managed,
        "users_verified": verified,
        "target_user_verified": True,
        "metric_timestamp": timestamp,
    }


def _compute_project_mapping_entries(payload: dict[str, Any]) -> tuple[str, str]:
    username = str(payload["username"])
    project_id = int(payload["project_id"])
    return (
        f"{project_id}:{PILOT_DATA_ROOT / username}",
        f"h100_{username}:{project_id}",
    )


def _compute_stage_expected_mount_sources(payload: dict[str, Any]) -> set[str]:
    username = str(payload["username"])
    user_root = PILOT_DATA_ROOT / username
    return {
        str(user_root / "home"),
        str(workspace_path(int(payload["uid"]))),
        str(user_root / "shared"),
        f"/srv/gpu-platform/container-data/{username}/ssh-host-keys",
    }


def _compute_stage_postconditions(payload: dict[str, Any]) -> dict[str, Any]:
    username = str(payload["username"])
    _compute_stage_state(payload)
    try:
        account = pwd.getpwnam(username)
        private_group = grp.getgrnam(username)
    except KeyError as exc:
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED", "staged Linux identity is missing"
        ) from exc
    if (
        account.pw_uid != payload["uid"]
        or account.pw_gid != payload["gid"]
        or private_group.gr_gid != payload["gid"]
        or account.pw_shell != "/usr/sbin/nologin"
        or set(_group_names(username, account.pw_gid)) != {username}
    ):
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED", "staged Linux identity differs from plan"
        )
    password = run_fixed("passwd", ["-S", username], timeout=10)
    password_fields = str(password.get("stdout", "")).split()
    if not password.get("ok") or len(password_fields) < 2 or password_fields[1] != "L":
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED", "staged Linux password is not locked"
        )
    host_key = Path(account.pw_dir) / ".ssh/authorized_keys"
    container_key = PILOT_DATA_ROOT / username / "home/.ssh/authorized_keys"
    if any(path.exists() or path.is_symlink() for path in (host_key, container_key)):
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED", "authorized_keys exists before enrollment"
        )

    dropin = Path(f"/etc/systemd/system/user-{payload['uid']}.slice.d/{GPU_DROPIN_NAME}")
    if not (
        dropin.is_file()
        and not dropin.is_symlink()
        and dropin.read_text(encoding="utf-8") == GPU_DROPIN_CONTENT
        and stat.S_IMODE(dropin.stat().st_mode) == 0o644
        and dropin.stat().st_uid == 0
        and dropin.stat().st_gid == 0
    ):
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED", "per-UID GPU policy is invalid"
        )
    matching = [
        item
        for item in _registry_entries()
        if item["username"] == username or item["uid"] == payload["uid"]
    ]
    if matching != [{"username": username, "uid": payload["uid"]}]:
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED", "GPU registry binding is invalid"
        )

    workspace = Path(workspace_path(int(payload["uid"])))
    required_workspace_directories = (
        "projects",
        "datasets",
        "outputs",
        ".portal",
        ".portal/job-scripts",
        ".portal/jobs",
    )
    try:
        workspace_metadata = workspace.lstat()
        directory_metadata = [
            (workspace / relative).lstat() for relative in required_workspace_directories
        ]
    except OSError as exc:
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED", "canonical workspace is incomplete"
        ) from exc
    if (
        not stat.S_ISDIR(workspace_metadata.st_mode)
        or stat.S_ISLNK(workspace_metadata.st_mode)
        or workspace_metadata.st_uid != int(payload["uid"])
        or workspace_metadata.st_gid != int(payload["gid"])
        or stat.S_IMODE(workspace_metadata.st_mode) != 0o700
        or any(
            not stat.S_ISDIR(item.st_mode)
            or stat.S_ISLNK(item.st_mode)
            or item.st_uid != int(payload["uid"])
            or item.st_gid != int(payload["gid"])
            or stat.S_IMODE(item.st_mode) != 0o700
            for item in directory_metadata
        )
    ):
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED",
            "canonical workspace ownership or mode is invalid",
        )
    projects_entry, projid_entry = _compute_project_mapping_entries(payload)
    if projects_entry not in _safe_file_lines(
        PROJECTS_FILE
    ) or projid_entry not in _safe_file_lines(PROJID_FILE):
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED", "XFS project mapping is missing"
        )
    quota = _verified_project_quota(int(payload["project_id"]), 300)

    association = run_fixed(
        "sacctmgr",
        [
            "-n",
            "-P",
            "show",
            "assoc",
            "where",
            f"User={username}",
            f"Account={payload['slurm_account']}",
            "format=User,Account,QOS,DefaultQOS,MaxTRES",
        ],
        timeout=20,
    )
    association_matches = False
    expected_max_tres = f"gres/gpu={payload['gpu_max']}"
    if association.get("ok"):
        for line in str(association.get("stdout", "")).splitlines():
            fields = line.split("|")
            if (
                len(fields) >= 5
                and fields[:2] == [username, payload["slurm_account"]]
                and payload["slurm_qos"] in fields[2].split(",")
                and fields[3] == payload["slurm_qos"]
                and expected_max_tres in fields[4].split(",")
            ):
                association_matches = True
                break
    if not association_matches:
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED",
            "Slurm association or association-level GPU entitlement differs from plan",
        )

    inspected = containers_inspect({"name": str(payload["container_name"])})
    container = inspected.get("container", {})
    state = container.get("state", {}) if isinstance(container, dict) else {}
    mounts = container.get("mounts", []) if isinstance(container, dict) else []
    expected_sources = _compute_stage_expected_mount_sources(payload)
    observed_sources = {str(item.get("Source", "")) for item in mounts if isinstance(item, dict)}
    if not (
        inspected.get("status") == "OK"
        and isinstance(container, dict)
        and str(container.get("name", "")).lstrip("/") == payload["container_name"]
        and container.get("owner") == username
        and container.get("cpu_limit") == 8.0
        and container.get("memory_limit_bytes") == 32 * 1024**3
        and container.get("pids_limit") == 4096
        and container.get("ssh_port") == str(payload["ssh_port"])
        and container.get("ssh_host_ip") == CONTAINER_PUBLISH_HOST
        and container.get("privileged") is False
        and container.get("network_mode") != "host"
        and container.get("pid_mode") != "host"
        and container.get("ipc_mode") != "host"
        and container.get("gpu") == "NONE"
        and not container.get("docker_socket_mounted")
        and not any(
            str(item.get("Destination", "")) == "/run/munge"
            or str(item.get("Source", "")) in {"/", str(PILOT_DATA_ROOT)}
            for item in mounts
            if isinstance(item, dict)
        )
        and isinstance(state, dict)
        and state.get("Running") is False
        and state.get("Status") in {"created", "exited"}
        and observed_sources == expected_sources
    ):
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED", "staged container security state is invalid"
        )
    listener = run_fixed("ss", ["-H", "-lnt", f"sport = :{payload['ssh_port']}"], timeout=10)
    if not listener.get("ok") or str(listener.get("stdout", "")).strip():
        raise LifecycleValidationError(
            "COMPUTE_STAGE_POSTCONDITION_FAILED", "staged container port is listening"
        )
    guard = _verified_guard_metrics_for_user(username, int(payload["uid"]))
    return {
        "username": username,
        "uid": payload["uid"],
        "gid": payload["gid"],
        "shell": "/usr/sbin/nologin",
        "password": "LOCKED",
        "host_ssh": "DISABLED",
        "host_authorized_keys": "ABSENT",
        "container_authorized_keys": "ABSENT",
        "gpu_policy": {
            "unit": f"user-{payload['uid']}.slice",
            "device_policy": "closed",
            "open_probe": "DENIED",
            "cuda_context_probe": "DENIED",
        },
        "quota": quota,
        "storage_path": str(workspace),
        "container_workspace": "/workspace",
        "default_job_workdir": str(workspace / "projects"),
        "slurm": {
            "account": payload["slurm_account"],
            "qos": payload["slurm_qos"],
            "max_gpus": payload["gpu_max"],
            "max_tres": expected_max_tres,
        },
        "container": {
            "name": payload["container_name"],
            "state": "STOPPED",
            "gpu": "NONE",
            "development_profile": (
                payload.get("container_profile") or payload.get("development_profile")
            ),
            "ssh_port": payload["ssh_port"],
            "image_digest": container.get("image_digest") or container.get("image_id"),
        },
        "guard": guard,
        "filesystem_isolation": {
            "origin_pilot_to_target": "DENIED",
            "target_to_origin_pilot": "DENIED",
            "markers_removed": True,
        },
        "lease": {"state": "NOT_STARTED", "starts_at": None, "expires_at": None},
        "onboarding_state": "STAGED",
        "ssh_key_state": "REQUIRED_BEFORE_ACTIVATION",
    }


def _compute_stage_retained_resources(payload: dict[str, Any]) -> list[str]:
    username = str(payload["username"])
    retained: set[str] = set()
    for label, path in {
        "host-home": MANAGED_HOME_ROOT / username,
        "managed-data": PILOT_DATA_ROOT / username,
        "managed-workspace": Path(workspace_path(int(payload["uid"]))),
        "container-data": Path("/srv/gpu-platform/container-data") / username,
        "container-config": PILOT_COMPOSE_ROOT / username,
        "lifecycle-state": PILOT_STATE_ROOT / f"{username}.state",
        "gpu-policy": Path(f"/etc/systemd/system/user-{payload['uid']}.slice.d/{GPU_DROPIN_NAME}"),
    }.items():
        if path.exists() or path.is_symlink():
            retained.add(label)
    with suppress(KeyError):
        pwd.getpwnam(username)
        retained.add("linux-user")
    with suppress(KeyError):
        grp.getgrnam(username)
        retained.add("linux-group")
    with suppress(KeyError):
        pwd.getpwuid(int(payload["uid"]))
        retained.add("linux-uid")
    with suppress(KeyError):
        grp.getgrgid(int(payload["gid"]))
        retained.add("linux-gid")
    for label, identity_number in (
        ("uid-ownership", int(payload["uid"])),
        ("gid-ownership", int(payload["gid"])),
    ):
        ownership_status, _path = _ownership_conflict(identity_number)
        if ownership_status == "CONFLICT":
            retained.add(label)
        elif ownership_status == "UNKNOWN":
            retained.add(f"{label}-unknown")
    association = _assoc_exists(username)
    if association is True:
        retained.add("slurm-association")
    elif association is None:
        retained.add("slurm-association-unknown")
    container = run_fixed("docker", ["container", "inspect", str(payload["container_name"])])
    if container.get("ok"):
        retained.add("container")
    elif "no such" not in str(container.get("stderr", "")).casefold():
        retained.add("container-state-unknown")

    used_ports, ports_readable = _used_ssh_ports()
    if not ports_readable:
        retained.add("ssh-port-state-unknown")
    elif int(payload["ssh_port"]) in used_ports:
        retained.add("ssh-port-allocation")

    derived_image = f"h100-local/dev-container:ubuntu24.04-{username}-{payload['plan_id']}"
    image = run_fixed("docker", ["image", "inspect", derived_image], timeout=20)
    if image.get("ok"):
        retained.add("derived-image")
    elif "no such" not in str(image.get("stderr", "")).casefold():
        retained.add("derived-image-state-unknown")

    projects_entry, projid_entry = _compute_project_mapping_entries(payload)
    if projects_entry in _safe_file_lines(PROJECTS_FILE) or projid_entry in _safe_file_lines(
        PROJID_FILE
    ):
        retained.add("xfs-project-mapping")
    quota = run_fixed(
        "xfs_quota",
        ["-x", "-c", "report -p -b -n", str(PILOT_DATA_ROOT.parent)],
        timeout=20,
    )
    if quota.get("ok"):
        project_prefix = f"#{payload['project_id']}"
        if any(
            line.split() and line.split()[0] == project_prefix
            for line in str(quota.get("stdout", "")).splitlines()
        ):
            retained.add("xfs-project-quota")
    else:
        retained.add("xfs-quota-state-unknown")

    registry_matches = [
        item
        for item in _registry_entries()
        if item["username"] == username or item["uid"] == payload["uid"]
    ]
    if registry_matches:
        retained.add("gpu-registry")

    node = slurm_node()
    if node.get("status") != "OK":
        retained.add("slurm-node-state-unknown")
    else:
        expected_reason = f"GPU bypass guard failure: policy-{payload['uid']}"
        for item in node.get("nodes", []):
            if not isinstance(item, dict):
                retained.add("slurm-node-state-unknown")
                continue
            state = str(item.get("state", "")).upper()
            reason = str(item.get("reason", ""))
            if "DRAIN" in state and reason.startswith(expected_reason):
                retained.add("slurm-node-drain")
    return sorted(retained)


def _compute_retry_verification(payload: dict[str, Any]) -> dict[str, Any]:
    integrity = script_integrity()
    failed_scripts = sorted(
        name
        for name in COMPUTE_STAGE_REQUIRED_SCRIPTS
        if not integrity.get(name, {}).get("integrity_ok", False)
    )
    retained = _compute_stage_retained_resources(payload)
    unknown = sorted(item for item in retained if "unknown" in item)
    ready = not failed_scripts and not retained
    return {
        "status": "DRY_RUN",
        "handler": "compute.provision.retry_verify",
        "retry_verification_status": "VERIFIED_ZERO_RESIDUE" if ready else "CONFLICT",
        "request_id": payload["request_id"],
        "plan_id": payload["plan_id"],
        "stage_operation_id": payload["stage_operation_id"],
        "script_integrity": "PASS" if not failed_scripts else "FAIL",
        "failed_scripts": failed_scripts,
        "resource_residue": retained,
        "unknown_resource_state": unknown,
        "infrastructure_side_effects": "NONE",
        "execution_enabled": False,
    }


def _stage_evidence_marker(stderr: str, label: str) -> str | None:
    pattern = re.compile(rf"^COMPUTE STAGE {re.escape(label)}: ([A-Z0-9_]+)$")
    for line in stderr.splitlines():
        match = pattern.fullmatch(line.strip())
        if match:
            return match.group(1)
    return None


def _workspace_alias_failure_marker(stderr: str) -> str | None:
    pattern = re.compile(r"^WORKSPACE ALIAS FAILURE CODE: ([A-Z0-9_]+)$")
    for line in stderr.splitlines():
        match = pattern.fullmatch(line.strip())
        if match:
            return match.group(1)
    return None


def _compute_stage_safe_failure_message(evidence: dict[str, Any]) -> str:
    alias_failure = evidence.get("workspace_alias_failure_code")
    if alias_failure:
        return f"workspace alias validation failed closed ({alias_failure})"
    return "transactional compute Stage script failed"


def _stage_rollback_steps(stderr: str) -> dict[str, str]:
    pattern = re.compile(
        r"^COMPUTE STAGE ROLLBACK STEP: "
        r"([A-Z0-9_]+)=(SUCCEEDED|FAILED|BLOCKED|NOT_REQUIRED)$"
    )
    steps: dict[str, str] = {}
    for line in stderr.splitlines():
        match = pattern.fullmatch(line.strip())
        if match:
            steps[match.group(1)] = match.group(2)
    return dict(sorted(steps.items()))


def _compute_stage_failure_evidence(
    execution: dict[str, Any], retained: list[str]
) -> dict[str, Any]:
    stderr = str(execution.get("stderr", ""))
    declared = _stage_evidence_marker(stderr, "SIDE EFFECT CLASSIFICATION")
    first_failed_step = _stage_evidence_marker(stderr, "FIRST FAILED STEP")
    last_successful_step = _stage_evidence_marker(stderr, "LAST SUCCESSFUL STEP")
    stage_failure_code = _stage_evidence_marker(stderr, "FAILURE CODE")
    workspace_alias_failure_code = _workspace_alias_failure_marker(stderr)
    unknown_resources = any("unknown" in item for item in retained)
    execution_error = str(execution.get("error_code", ""))
    if unknown_resources or execution_error in {
        "STAGE_EXECUTION_TIMEOUT",
        "SCRIPT_EXECUTION_ERROR",
    }:
        classification = "PARTIAL_UNKNOWN"
    elif retained:
        classification = "PARTIAL_ROLLBACK_FAILED"
    elif declared in {"NO_SIDE_EFFECT", "PARTIAL_ROLLED_BACK"}:
        classification = declared
    else:
        classification = "PARTIAL_UNKNOWN"
    rollback_status = {
        "NO_SIDE_EFFECT": "NOT_REQUIRED",
        "PARTIAL_ROLLED_BACK": "ROLLED_BACK",
        "PARTIAL_ROLLBACK_FAILED": "ROLLBACK_FAILED",
        "PARTIAL_UNKNOWN": "REQUIRES_MANUAL_REVIEW",
    }[classification]
    return {
        "side_effect_classification": classification,
        "rollback_status": rollback_status,
        "last_successful_step": last_successful_step or "WORKER_PREFLIGHT",
        "first_failed_step": first_failed_step or "FIXED_STAGE_SCRIPT",
        "failed_handler": "h100-provision-stage",
        "retained_resources": retained,
        "rollback_steps": _stage_rollback_steps(stderr),
        "stage_failure_code": stage_failure_code or "FIXED_STAGE_SCRIPT_FAILED",
        "workspace_alias_failure_code": workspace_alias_failure_code,
    }


def _compute_stage_contract_mismatches(
    expected: dict[str, Any], current: dict[str, Any]
) -> list[str]:
    checks = {
        "CONTRACT_STATUS": (expected.get("status"), current.get("status")),
        "CONTRACT_SHA256": (
            expected.get("contract_sha256"),
            current.get("contract_sha256"),
        ),
        "HANDLER_IDENTITY": (
            expected.get("handler", {}).get("identity"),
            current.get("handler", {}).get("identity"),
        ),
        "HANDLER_SHA256": (
            expected.get("handler", {}).get("sha256"),
            current.get("handler", {}).get("sha256"),
        ),
        "ARGV_CONTRACT_VERSION": (
            expected.get("argv_contract", {}).get("version"),
            current.get("argv_contract", {}).get("version"),
        ),
        "ARGV_CONTRACT_SHA256": (
            expected.get("argv_contract", {}).get("sha256"),
            current.get("argv_contract", {}).get("sha256"),
        ),
        "ARGUMENT_14_BINDING": (
            expected.get("argv_contract", {}).get("argument_14", {}).get("binding_status"),
            current.get("argv_contract", {}).get("argument_14", {}).get("binding_status"),
        ),
        "ARGUMENT_15_BINDING": (
            expected.get("argv_contract", {}).get("argument_15", {}).get("binding_status"),
            current.get("argv_contract", {}).get("argument_15", {}).get("binding_status"),
        ),
        "CONFIRMATION_VALIDATOR_VERSION": (
            expected.get("confirmation_gate", {}).get("validator_version"),
            current.get("confirmation_gate", {}).get("validator_version"),
        ),
        "CONFIRMATION_VALIDATOR_SHA256": (
            expected.get("confirmation_gate", {}).get("validator_sha256"),
            current.get("confirmation_gate", {}).get("validator_sha256"),
        ),
        "CONFIRMATION_GATE_STATUS": (
            expected.get("confirmation_gate", {}).get("status"),
            current.get("confirmation_gate", {}).get("status"),
        ),
        "IMAGE_VALIDATOR_VERSION": (
            expected.get("image_contract", {}).get("validator_version"),
            current.get("image_contract", {}).get("validator_version"),
        ),
        "CANONICAL_LOCAL_IMAGE_IDENTITY": (
            expected.get("image_contract", {}).get("canonical_local_image_identity"),
            current.get("image_contract", {}).get("canonical_local_image_identity"),
        ),
        "LOCAL_IMAGE_SOURCE_TYPE": (
            expected.get("image_contract", {}).get("source_type"),
            current.get("image_contract", {}).get("source_type"),
        ),
        "LOCAL_IMAGE_ARTIFACT_PATH": (
            expected.get("image_contract", {}).get("artifact_path"),
            current.get("image_contract", {}).get("artifact_path"),
        ),
        "LOCAL_IMAGE_MANIFEST_DIGEST": (
            expected.get("image_contract", {}).get("manifest_digest"),
            current.get("image_contract", {}).get("manifest_digest"),
        ),
        "LOCAL_IMAGE_DEPLOYMENT_VERSION": (
            expected.get("image_contract", {}).get("approved_deployment_version"),
            current.get("image_contract", {}).get("approved_deployment_version"),
        ),
        "IMAGE_VALIDATION_STATUS": (
            expected.get("image_contract", {}).get("status"),
            current.get("image_contract", {}).get("status"),
        ),
    }
    return sorted(label for label, values in checks.items() if values[0] != values[1])


def _execute_compute_provision_stage(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    if request.requested_by != request.approved_by or request.dry_run:
        return {
            "status": "ERROR",
            "error": {
                "code": "COMPUTE_STAGE_APPROVAL_BINDING_REJECTED",
                "message": "real Stage requires one recently reauthenticated administrator",
            },
            "side_effect_classification": "NO_SIDE_EFFECT",
            "rollback_status": "NOT_REQUIRED",
            "last_successful_step": "NONE",
            "first_failed_step": "WORKER_AUTHORIZATION",
            "failed_handler": "compute.provision.stage",
            "retained_resources": [],
        }
    if request.idempotency_key != f"compute-stage:{payload['stage_operation_id']}":
        return {
            "status": "ERROR",
            "error": {
                "code": "COMPUTE_STAGE_IDEMPOTENCY_BINDING_REJECTED",
                "message": "Stage idempotency key is not bound to its Operation",
            },
            "side_effect_classification": "NO_SIDE_EFFECT",
            "rollback_status": "NOT_REQUIRED",
            "last_successful_step": "WORKER_AUTHORIZATION",
            "first_failed_step": "WORKER_IDEMPOTENCY_BINDING",
            "failed_handler": "compute.provision.stage",
            "retained_resources": [],
        }
    integrity = script_integrity()
    failed = sorted(
        name
        for name in COMPUTE_STAGE_REQUIRED_SCRIPTS
        if not integrity.get(name, {}).get("integrity_ok", False)
    )
    if failed:
        return {
            "status": "ERROR",
            "error": {
                "code": "SCRIPT_INTEGRITY_FAILED",
                "message": "fixed compute Stage script integrity check failed",
                "scripts": failed,
            },
            "side_effect_classification": "NO_SIDE_EFFECT",
            "rollback_status": "NOT_REQUIRED",
            "last_successful_step": "WORKER_IDEMPOTENCY_BINDING",
            "first_failed_step": "SCRIPT_INTEGRITY",
            "failed_handler": "compute.provision.stage",
            "retained_resources": [],
        }
    expected_contract = payload["dry_run_stage_contract"]
    current_image_identity = _standard_dev_image_identity()
    current_contract = _compute_stage_contract(
        payload,
        integrity=integrity,
        image_identity=current_image_identity,
    )
    contract_mismatches = _compute_stage_contract_mismatches(expected_contract, current_contract)
    if current_contract.get("status") != "PASS" or contract_mismatches:
        return {
            "status": "ERROR",
            "error": {
                "code": "DRY_RUN_STAGE_CONTRACT_MISMATCH",
                "message": "Stage execution contract changed; a new dry-run is required",
            },
            "contract_verification": {
                "status": "FAIL",
                "mismatches": contract_mismatches or ["CURRENT_CONTRACT_INVALID"],
                "expected_contract_sha256": expected_contract.get("contract_sha256"),
                "current_contract_sha256": current_contract.get("contract_sha256"),
            },
            "side_effect_classification": "NO_SIDE_EFFECT",
            "rollback_status": "NOT_REQUIRED",
            "last_successful_step": "SCRIPT_INTEGRITY",
            "first_failed_step": "DRY_RUN_STAGE_CONTRACT_BINDING",
            "failed_handler": "compute.provision.stage",
            "retained_resources": [],
        }
    state_path = PILOT_STATE_ROOT / f"{payload['username']}.state"
    if state_path.exists() or state_path.is_symlink():
        try:
            stage = _compute_stage_postconditions(payload)
        except LifecycleValidationError as exc:
            return {
                "status": "ERROR",
                "error": {"code": "IDEMPOTENCY_STATE_MISMATCH", "message": str(exc)},
                "rollback_status": "REQUIRES_MANUAL_REVIEW",
                "side_effect_classification": "PARTIAL_UNKNOWN",
                "last_successful_step": "LIFECYCLE_STATE_PRESENT",
                "first_failed_step": "IDEMPOTENCY_POSTCONDITION",
                "failed_handler": "compute.provision.stage",
                "retained_resources": _compute_stage_retained_resources(payload),
            }
        return {
            "status": "SUCCEEDED",
            "handler": "compute.provision.stage",
            "execution_enabled": True,
            "idempotent_replay": True,
            "stage": stage,
        }
    preflight = _compute_provision_dry_run(
        {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "stage_operation_id",
                "dry_run_stage_contract",
                "reservation_ids",
            }
        }
        | {"execution_enabled": False},
        image_identity=current_image_identity,
        integrity=integrity,
    )
    if preflight.get("dry_run_status") != "READY_FOR_PROVISION":
        return {
            "status": "ERROR",
            "error": {
                "code": "COMPUTE_STAGE_PREFLIGHT_CONFLICT",
                "message": "reserved resources changed after dry-run",
            },
            "preflight": preflight,
            "side_effect_classification": "NO_SIDE_EFFECT",
            "rollback_status": "NOT_REQUIRED",
            "last_successful_step": "SCRIPT_INTEGRITY",
            "first_failed_step": "WORKER_PREFLIGHT",
            "failed_handler": "compute.provision.stage",
            "retained_resources": [],
        }
    execution = run_allowlisted_script(
        _compute_stage_argv(payload),
        timeout=STAGE_EXECUTION_TIMEOUT_SECONDS,
        expected_local_image_identity=str(current_image_identity["canonical_local_image_identity"]),
    )
    if not execution.get("ok"):
        retained = _compute_stage_retained_resources(payload)
        evidence = _compute_stage_failure_evidence(execution, retained)
        return {
            "status": "ERROR",
            "error": {
                "code": str(execution.get("error_code", "COMPUTE_STAGE_FAILED"))[:64],
                "message": _compute_stage_safe_failure_message(evidence),
                "exit_code": execution.get("exit_code"),
            },
            **evidence,
        }
    try:
        stage = _compute_stage_postconditions(payload)
    except LifecycleValidationError as exc:
        retained = _compute_stage_retained_resources(payload)
        return {
            "status": "ERROR",
            "error": {"code": exc.code, "message": str(exc)},
            "rollback_status": "REQUIRES_MANUAL_REVIEW",
            "side_effect_classification": "PARTIAL_UNKNOWN",
            "last_successful_step": "FIXED_STAGE_SCRIPT",
            "first_failed_step": "WORKER_POSTCONDITION",
            "failed_handler": "compute.provision.stage",
            "retained_resources": retained,
        }
    return {
        "status": "SUCCEEDED",
        "handler": "compute.provision.stage",
        "execution_enabled": True,
        "idempotent_replay": False,
        "stage": stage,
    }


def _user_plan(requested_username: str) -> dict[str, Any]:
    """Build a host-only onboarding plan; this function never performs writes."""
    if requested_username != PILOT_USERNAME:
        return {
            "status": "ERROR",
            "error": {"code": "PLAN_USERNAME_REJECTED", "message": "Portal-3A 只规划 origin-pilot"},
            "execution_enabled": False,
        }

    conflicts: list[dict[str, str]] = []
    validations: list[dict[str, str]] = []
    try:
        origin = pwd.getpwnam(MANAGEMENT_USERNAME)
        origin_groups = _group_names(MANAGEMENT_USERNAME, origin.pw_gid)
        high_privilege = sorted(
            set(origin_groups)
            & {"sudo", "adm", "lxd", "docker", "video", "render", "gpu-platform-admin"}
        )
        validations.append(
            {
                "check": "origin_al_management_identity",
                "status": "PASS",
                "detail": f"origin-al UID {origin.pw_uid} 保留为 Portal platform_owner；管理组职责不转换为 Pilot",
            }
        )
        if high_privilege:
            validations.append(
                {
                    "check": "origin_al_privileged_groups",
                    "status": "PASS",
                    "detail": f"管理职责组已保留（数量 {len(high_privilege)}）；不修改现有账号",
                }
            )
        else:
            validations.append(
                {
                    "check": "origin_al_privileged_groups",
                    "status": "PASS",
                    "detail": "Portal platform_owner 映射本身足以要求身份分离",
                }
            )
    except KeyError:
        validations.append(
            {
                "check": "origin_al_management_identity",
                "status": "FAIL",
                "detail": "Linux 管理映射 origin-al 不存在",
            }
        )
        conflicts.append({"code": "ORIGIN_AL_MISSING", "message": "不能证明 Portal 管理映射存在"})

    user_exists = False
    group_exists = False
    try:
        pwd.getpwnam(PILOT_USERNAME)
        user_exists = True
    except KeyError:
        pass
    try:
        grp.getgrnam(PILOT_USERNAME)
        group_exists = True
    except KeyError:
        pass
    if user_exists or group_exists:
        conflicts.append(
            {"code": "USERNAME_CONFLICT", "message": "origin-pilot Linux 用户或组已存在"}
        )
    validations.append(
        {
            "check": "origin_pilot_username_available",
            "status": "FAIL" if user_exists or group_exists else "PASS",
            "detail": "origin-pilot 用户和同名组不存在"
            if not (user_exists or group_exists)
            else "存在同名 Linux 对象",
        }
    )

    exact_container = run_fixed(
        "docker", ["container", "inspect", "gpu-dev-origin-pilot"], timeout=15
    )
    inspect_error = str(exact_container.get("stderr", ""))
    container_absent = not exact_container.get("ok") and (
        "No such container" in inspect_error or "No such object" in inspect_error
    )
    container_unknown = not exact_container.get("ok") and not container_absent
    if not container_absent:
        conflicts.append(
            {
                "code": "CONTAINER_ADAPTER_UNKNOWN" if container_unknown else "CONTAINER_CONFLICT",
                "message": "Docker 状态无法确认"
                if container_unknown
                else "gpu-dev-origin-pilot 容器已存在",
            }
        )
    validations.append(
        {
            "check": "container_name_available",
            "status": "PASS" if container_absent else "UNKNOWN" if container_unknown else "FAIL",
            "detail": "gpu-dev-origin-pilot 不存在"
            if container_absent
            else "Docker 状态无法读取"
            if container_unknown
            else "目标容器已存在",
        }
    )

    stale_paths = [
        Path("/home") / PILOT_USERNAME,
        PILOT_DATA_ROOT / PILOT_USERNAME,
        PILOT_STATE_ROOT / f"{PILOT_USERNAME}.state",
        PILOT_COMPOSE_ROOT / PILOT_USERNAME / "compose.yml",
    ]
    stale_path_conflicts = [str(path) for path in stale_paths if path.exists()]
    registry_name_conflict = any(
        entry.get("username") == PILOT_USERNAME for entry in _registry_entries()
    )
    validations.append(
        {
            "check": "stale_platform_identity_absent",
            "status": "PASS" if not stale_path_conflicts and not registry_name_conflict else "FAIL",
            "detail": "没有目标数据目录、state、compose 或 GPU registry 遗留"
            if not stale_path_conflicts and not registry_name_conflict
            else "发现目标名的平台遗留记录",
        }
    )
    if stale_path_conflicts or registry_name_conflict:
        conflicts.append(
            {
                "code": "STALE_PLATFORM_IDENTITY",
                "message": "origin-pilot 存在数据目录、state、compose 或 GPU registry 遗留",
            }
        )

    uid, uid_rejections, uid_checks = _candidate_uid_gid()
    project_id, project_checks = _candidate_project_id()
    ssh_port, port_checks = _candidate_ssh_port()
    if uid is None:
        conflicts.append({"code": "UID_GID_UNAVAILABLE", "message": "没有可用的 Pilot UID/GID"})
    if project_id is None:
        conflicts.append({"code": "PROJECT_ID_UNAVAILABLE", "message": "没有可用的 project ID"})
    if ssh_port is None:
        conflicts.append({"code": "SSH_PORT_UNAVAILABLE", "message": "没有可用的受控容器 SSH 端口"})
    validations.extend(
        [
            {
                "check": "uid_gid_candidate",
                "status": "PASS" if uid is not None else "FAIL",
                "detail": f"候选 UID/GID={uid}，只提出不预留"
                if uid is not None
                else "候选 UID/GID 不可用",
            },
            {
                "check": "project_id_candidate",
                "status": "PASS" if project_id is not None else "FAIL",
                "detail": f"候选 project ID={project_id}，只提出不预留"
                if project_id is not None
                else "候选 project ID 不可用",
            },
            {
                "check": "ssh_port_candidate",
                "status": "PASS" if ssh_port is not None else "FAIL",
                "detail": f"候选端口={ssh_port}，用户入口 {PUBLIC_ACCESS_HOST}，只提出不开放"
                if ssh_port is not None
                else "候选端口不可用",
            },
            {
                "check": "legacy_uid_gid_ownership",
                "status": str(uid_checks.get("legacy_ownership_scan", "UNKNOWN")),
                "detail": "候选 UID/GID 没有发现固定扫描根下的 ownership 遗留"
                if uid_checks.get("legacy_ownership_scan") == "PASS"
                else "候选 UID/GID ownership 扫描不可证明通过",
            },
        ]
    )

    validations.extend(_slurm_plan_checks(PILOT_USERNAME))
    slurm_failures = [
        item
        for item in validations
        if item["check"].startswith("slurm_") and item["status"] == "FAIL"
    ]
    conflicts.extend(
        {"code": item["check"].upper(), "message": item["detail"]} for item in slurm_failures
    )

    global_dropins = [
        "/etc/systemd/system/user.slice.d/50-h100-gpu-isolation.conf",
        "/etc/systemd/system/user-.slice.d/50-h100-gpu-isolation.conf",
    ]
    global_conflicts = [path for path in global_dropins if Path(path).exists()]
    validations.append(
        {
            "check": "global_gpu_dropins_absent",
            "status": "PASS" if not global_conflicts else "FAIL",
            "detail": "user.slice 与 user-.slice 全局 GPU drop-in 均不存在"
            if not global_conflicts
            else "发现禁止的全局 GPU drop-in",
        }
    )
    if global_conflicts:
        conflicts.append({"code": "GLOBAL_GPU_POLICY", "message": "禁止的全局 user slice 策略存在"})

    dropin_path = (
        f"/etc/systemd/system/user-{uid}.slice.d/{GPU_DROPIN_NAME}" if uid is not None else None
    )
    exact_dropin_exists = bool(dropin_path and Path(dropin_path).exists())
    validations.append(
        {
            "check": "exact_uid_dropin_unreserved",
            "status": "PASS" if not exact_dropin_exists else "FAIL",
            "detail": "候选 UID 的持久 drop-in 不存在；不会在计划阶段写入"
            if not exact_dropin_exists
            else "候选 UID 已有 drop-in",
        }
    )
    if exact_dropin_exists:
        conflicts.append(
            {"code": "UID_DROPIN_CONFLICT", "message": "候选 UID 已存在持久 GPU drop-in"}
        )

    integrity = script_integrity()
    required_scripts = {
        "h100-user-create",
        "h100-user-gpu-isolation",
        "h100-container-create",
        "h100-container-stop",
        "h100-gpu-bypass-guard",
    }
    integrity_ok = all(
        integrity.get(name, {}).get("integrity_ok", False) for name in required_scripts
    )
    validations.append(
        {
            "check": "lifecycle_script_integrity",
            "status": "PASS" if integrity_ok else "FAIL",
            "detail": "用户、隔离、容器停止和 Guard 生命周期脚本均为 root-owned 且 hash 匹配"
            if integrity_ok
            else "生命周期脚本完整性校验失败",
        }
    )
    if not integrity_ok:
        conflicts.append(
            {"code": "SCRIPT_INTEGRITY_FAILED", "message": "固定生命周期脚本完整性失败"}
        )

    timer_enabled = run_fixed(
        "systemctl", ["is-enabled", "h100-gpu-bypass-guard.timer"], timeout=10
    )
    timer_active = run_fixed("systemctl", ["is-active", "h100-gpu-bypass-guard.timer"], timeout=10)
    timer_ok = (
        timer_enabled.get("stdout", "").strip() == "disabled"
        and timer_active.get("stdout", "").strip() == "inactive"
    )
    validations.append(
        {
            "check": "guard_timer_not_enabled",
            "status": "PASS" if timer_ok else "FAIL",
            "detail": "无 Pilot 用户时 Guard timer 保持 disabled/inactive"
            if timer_ok
            else "Guard timer 状态不是 disabled/inactive",
        }
    )
    if not timer_ok:
        conflicts.append(
            {
                "code": "GUARD_TIMER_STATE",
                "message": "没有 STAGED Pilot 用户时 Guard timer 必须保持 disabled/inactive",
            }
        )

    data_path = str(workspace_path(uid)) if uid is not None else None
    compose_path = f"/srv/gpu-platform/platform/config/dev-containers/{PILOT_USERNAME}/compose.yml"
    plan_ready = (
        not conflicts and uid is not None and project_id is not None and ssh_port is not None
    )
    plan: dict[str, Any] = {
        "status": "DRY_RUN",
        "plan_status": "READY" if plan_ready else "CONFLICT",
        "handler": "user.plan",
        "execution_enabled": False,
        "proposal_state": "DRAFT",
        "proposed_username": PILOT_USERNAME,
        "proposed_host_access": {"enabled": True, "shell_before_activate": "/usr/sbin/nologin"},
        "portal_owner": "Origin-al",
        "portal_owner_normalized": MANAGEMENT_USERNAME,
        "management_identity_preserved": True,
        "proposed_uid": uid,
        "proposed_gid": uid,
        "proposed_project_id": project_id,
        "proposed_ssh_port": ssh_port,
        "uid_reservation": "PROPOSED — NOT RESERVED",
        "gid_reservation": "PROPOSED — NOT RESERVED",
        "project_reservation": project_checks.get("reservation"),
        "ssh_port_reservation": port_checks.get("reservation"),
        "proposed_slurm_account": "company",
        "proposed_qos": "general",
        "proposed_max_gpus": 1,
        "proposed_quota_bytes": 300 * 1024**3,
        "proposed_quota_hard_limit_gb": 300,
        "proposed_container": {
            "enabled": True,
            "name": "gpu-dev-origin-pilot",
            "cpus": 8,
            "memory_gb": 32,
            "pids_limit": 4096,
            "gpu": "none",
            "network_bind": PUBLIC_ACCESS_HOST,
        },
        "proposed_gpu_policy": {
            "method": "systemd-user-uid-slice",
            "unit": f"user-{uid}.slice" if uid is not None else None,
            "dropin_path": dropin_path,
            "content": GPU_DROPIN_CONTENT,
            "device_policy": "closed",
            "device_allow": [],
            "global_user_slice_modified": False,
            "global_user_template_modified": False,
        },
        "proposed_data_path": data_path,
        "proposed_compose_path": compose_path,
        "ssh_key_status": "REQUIRED BEFORE ACTIVATION",
        "stage_ssh_key_status": "NOT_REQUIRED_FOR_STAGE",
        "validation_results": validations,
        "candidate_rejections": uid_rejections,
        "conflicts": conflicts,
        "stage_steps": [
            "创建普通账号并保持 /usr/sbin/nologin、密码锁定",
            "在首次登录前应用精确 user-UID.slice DevicePolicy=closed",
            "执行 GPU deny self-test；不写入全局 user.slice 或 user-.slice",
            "创建受控目录并建立 XFS project quota",
            "创建 company/general Slurm association（max_gpus=1）",
            "创建 GPU=none 的长期容器并验证安全属性",
            "停止长期容器并确认 SSH 端口不监听",
            "启用 Guard timer 并验证全部受管 UID",
            "保持 STAGED；不安装 authorized_keys",
        ],
        "activate_steps": [
            "重新验证精确 UID slice、无高权限组和 GPU=none 容器",
            "要求经批准的 SSH 公钥（当前缺少，故不可激活）",
            "安装公钥后再启用普通 shell",
            "重新验证 Guard，并执行本人登录、CPU 与单 GPU Slurm 验收",
            "通过人工验收后标记 ACTIVE",
        ],
        "rollback_steps": [
            "Stage 失败时保持 nologin/锁定并恢复受控配置备份",
            "回滚精确 UID drop-in、registry、quota、association 和容器配置",
            "Activate 失败时移除公钥、恢复 nologin，并停止用户容器",
            "不删除用户数据，不恢复 Slurm 调度",
        ],
        "expected_backups": [
            "/etc/projects",
            "/etc/projid",
            "/etc/h100-platform/gpu-isolated-users",
            "精确 user-UID.slice drop-in（若未来存在）",
        ],
    }
    if not plan_ready:
        plan["execution_blocked_reason"] = (
            "存在冲突或候选资源不可用；仅允许管理员重新选择并重新规划"
        )
    return plan


def _user_stage_dry_run(payload: dict[str, Any]) -> dict[str, Any]:
    """Revalidate the approved plan without invoking the lifecycle script."""
    stage_argv = build_user_stage_argv(payload)
    if "--public-key-file" in stage_argv:
        raise LifecycleValidationError(
            "PUBLIC_KEY_NOT_ALLOWED_DURING_STAGE", "Stage argv unexpectedly contains a key"
        )
    plan = _user_plan(str(payload["username"]))
    if plan.get("status") != "DRY_RUN" or plan.get("plan_status") != "READY":
        plan["handler"] = "user.stage"
        plan["stage_status"] = "CONFLICT"
        plan["ssh_key_status"] = "NOT_REQUIRED_FOR_STAGE"
        return plan
    comparisons = {
        "uid": plan.get("proposed_uid"),
        "gid": plan.get("proposed_gid"),
        "project_id": plan.get("proposed_project_id"),
        "ssh_port": plan.get("proposed_ssh_port"),
        "quota_gb": plan.get("proposed_quota_hard_limit_gb"),
        "slurm_account": plan.get("proposed_slurm_account"),
        "slurm_qos": plan.get("proposed_qos"),
        "max_gpus": plan.get("proposed_max_gpus"),
    }
    container = plan.get("proposed_container", {})
    if isinstance(container, dict):
        comparisons.update(
            {
                "container_name": container.get("name"),
                "cpus": container.get("cpus"),
                "memory_gb": container.get("memory_gb"),
                "pids_limit": container.get("pids_limit"),
                "gpu": container.get("gpu"),
            }
        )
    mismatches = [field for field, current in comparisons.items() if payload.get(field) != current]
    if mismatches:
        return {
            "status": "ERROR",
            "handler": "user.stage",
            "stage_status": "CONFLICT",
            "execution_enabled": False,
            "error": {
                "code": "STAGE_PLAN_MISMATCH",
                "message": "validated Stage values changed",
                "fields": sorted(mismatches),
            },
        }
    return {
        **plan,
        "handler": "user.stage",
        "stage_status": "READY",
        "execution_enabled": False,
        "ssh_key_status": "NOT_REQUIRED_FOR_STAGE",
        "post_stage_ssh_key_state": "REQUIRED_BEFORE_ACTIVATION",
        "public_key_validation": "DEFERRED_TO_ACTIVATE",
        "authorized_keys_expected": "ABSENT",
        "host_login_expected": "DISABLED",
        "stage_cli_contract": "NO_PUBLIC_KEY_ARGUMENT",
    }


def _staged_origin_pilot_state() -> dict[str, str]:
    path = PILOT_STATE_ROOT / f"{PILOT_USERNAME}.state"
    values: dict[str, str] = {}
    for line in _safe_file_lines(path):
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z0-9_]{1,32}", key):
            values[key] = value[:256]
    if (
        values.get("STATUS") != "STAGED"
        or values.get("USERNAME") != PILOT_USERNAME
        or values.get("SSH_KEY_STATE", "REQUIRED_BEFORE_ACTIVATION") != "REQUIRED_BEFORE_ACTIVATION"
    ):
        raise LifecycleValidationError(
            "USER_NOT_IN_STAGED_STATE", "origin-pilot is not in the STAGED state"
        )
    return values


def _verified_project_quota(project_id: int, quota_gb: int) -> dict[str, Any]:
    state = run_fixed("xfs_quota", ["-x", "-c", "state", str(PILOT_DATA_ROOT.parent)], timeout=20)
    state_output = str(state.get("stdout", ""))
    project_state = state_output.partition("Project quota state")[2]
    if (
        not state.get("ok")
        or not project_state
        or "Accounting: ON" not in project_state
        or "Enforcement: ON" not in project_state
    ):
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "XFS project quota is not enforced"
        )

    report = run_fixed(
        "xfs_quota",
        ["-x", "-c", "report -p -b -n", str(PILOT_DATA_ROOT.parent)],
        timeout=20,
    )
    expected_hard_blocks = quota_gb * 1024 * 1024
    observed_hard_blocks: int | None = None
    if report.get("ok"):
        for line in str(report.get("stdout", "")).splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[0] == f"#{project_id}" and fields[3].isdigit():
                observed_hard_blocks = int(fields[3])
                break
    if observed_hard_blocks != expected_hard_blocks:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "XFS project hard quota differs from approval"
        )
    return {
        "project_id": project_id,
        "hard_limit_gb": quota_gb,
        "accounting": "ON",
        "enforcement": "ON",
    }


def _read_guard_metric_file() -> str:
    try:
        before = GUARD_METRIC_FILE.lstat()
    except OSError as exc:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "GPU bypass Guard metrics are unavailable"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != GUARD_METRIC_OWNER_UID
        or before.st_gid != GUARD_METRIC_OWNER_GID
        or stat.S_IMODE(before.st_mode) != 0o644
        or not 0 < before.st_size <= 64 * 1024
    ):
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "GPU bypass Guard metric metadata is invalid"
        )
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(GUARD_METRIC_FILE, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise LifecycleValidationError(
                    "STAGE_POSTCONDITION_FAILED", "GPU bypass Guard metrics changed during open"
                )
            content = os.read(descriptor, 64 * 1024 + 1)
        finally:
            os.close(descriptor)
    except LifecycleValidationError:
        raise
    except OSError as exc:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "GPU bypass Guard metrics could not be read safely"
        ) from exc
    if not 0 < len(content) <= 64 * 1024:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "GPU bypass Guard metrics have an invalid size"
        )
    try:
        return content.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "GPU bypass Guard metrics are not ASCII"
        ) from exc


def _verified_guard_metrics(uid: int) -> dict[str, Any]:
    content = _read_guard_metric_file()
    scalar_metrics: dict[str, int] = {}
    user_success: int | None = None
    timestamp: int | None = None
    scalar_pattern = re.compile(r"^(h100_gpu_bypass_guard_[a-z_]+) ([0-9]+)$")
    user_pattern = re.compile(
        rf'^h100_gpu_user_isolation_success\{{username="{re.escape(PILOT_USERNAME)}",'
        rf'uid="{uid}"\}} ([01])$'
    )
    for line in content.splitlines():
        scalar_match = scalar_pattern.fullmatch(line)
        if scalar_match:
            value = int(scalar_match.group(2))
            if scalar_match.group(1) == "h100_gpu_bypass_guard_timestamp_seconds":
                timestamp = value
            else:
                scalar_metrics[scalar_match.group(1)] = value
            continue
        user_match = user_pattern.fullmatch(line)
        if user_match:
            user_success = int(user_match.group(1))

    expected = {
        "h100_gpu_bypass_guard_last_success": 1,
        "h100_gpu_bypass_guard_managed_users": 1,
        "h100_gpu_bypass_guard_users_verified": 1,
        "h100_gpu_bypass_guard_policy_errors": 0,
        "h100_gpu_bypass_guard_device_open_failures": 0,
        "h100_gpu_bypass_guard_cuda_context_failures": 0,
        "h100_gpu_bypass_guard_slurm_constrain_devices": 1,
        "h100_gpu_bypass_guard_nvidia_gpu_count": 4,
        "h100_gpu_bypass_guard_slurm_gpu_count": 4,
    }
    now = int(time.time())
    metrics_fresh = (
        timestamp is not None
        and timestamp <= now + 60
        and now - timestamp <= GUARD_METRIC_MAX_AGE_SECONDS
    )
    if any(scalar_metrics.get(name) != value for name, value in expected.items()) or not (
        user_success == 1 and metrics_fresh
    ):
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "GPU bypass Guard metrics do not prove isolation"
        )
    return {
        "timer": "ENABLED_ACTIVE",
        "status": "PASSING",
        "managed_users": 1,
        "users_verified": 1,
        "open_probe": "DENIED",
        "cuda_context_probe": "DENIED",
        "constrain_devices": 1,
        "nvidia_gpu_count": 4,
        "slurm_gpu_count": 4,
        "metric_timestamp": timestamp,
    }


def _stage_postcondition_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-read the fixed Stage target after the transactional script succeeds."""
    staged = _staged_origin_pilot_state()
    expected_state = {
        "USERNAME": payload["username"],
        "UID": str(payload["uid"]),
        "GID": str(payload["gid"]),
        "PROJECT_ID": str(payload["project_id"]),
        "SSH_PORT": str(payload["ssh_port"]),
        "SLURM_ACCOUNT": payload["slurm_account"],
        "SLURM_QOS": payload["slurm_qos"],
        "SSH_KEY_STATE": "REQUIRED_BEFORE_ACTIVATION",
        "KEY_SHA256": "",
        "KEY_FINGERPRINTS": "",
    }
    mismatches = sorted(
        key for key, expected in expected_state.items() if staged.get(key) != expected
    )
    if mismatches:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED",
            f"staged state differs from the approved payload: {','.join(mismatches)}",
        )

    try:
        account = pwd.getpwnam(PILOT_USERNAME)
        private_group = grp.getgrnam(PILOT_USERNAME)
    except KeyError as exc:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "staged account or private group is missing"
        ) from exc
    if (
        account.pw_uid != payload["uid"]
        or account.pw_gid != payload["gid"]
        or private_group.gr_gid != payload["gid"]
        or account.pw_shell != "/usr/sbin/nologin"
    ):
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "staged account identity does not match approval"
        )
    groups = _group_names(PILOT_USERNAME, account.pw_gid)
    unexpected_groups = sorted(set(groups) - {PILOT_USERNAME})
    if unexpected_groups or set(groups) & FORBIDDEN_PILOT_GROUPS:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "staged account has unexpected supplemental groups"
        )

    password = run_fixed("passwd", ["-S", PILOT_USERNAME], timeout=10)
    password_fields = str(password.get("stdout", "")).split()
    if not password.get("ok") or len(password_fields) < 2 or password_fields[1] != "L":
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "staged account password is not locked"
        )
    authorized_key_paths = (
        Path(account.pw_dir) / ".ssh" / "authorized_keys",
        PILOT_DATA_ROOT / PILOT_USERNAME / "home" / ".ssh" / "authorized_keys",
    )
    if any(path.exists() or path.is_symlink() for path in authorized_key_paths):
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "authorized_keys exists before activation"
        )

    dropin = Path(f"/etc/systemd/system/user-{payload['uid']}.slice.d/{GPU_DROPIN_NAME}")
    try:
        dropin_stat = dropin.lstat()
        dropin_content = dropin.read_text(encoding="utf-8")
    except OSError as exc:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "exact UID GPU policy is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(dropin_stat.st_mode)
        or stat.S_ISLNK(dropin_stat.st_mode)
        or dropin_stat.st_uid != 0
        or dropin_stat.st_gid != 0
        or stat.S_IMODE(dropin_stat.st_mode) != 0o644
        or dropin_content != GPU_DROPIN_CONTENT
        or "DeviceAllow" in dropin_content
    ):
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "exact UID GPU policy metadata is invalid"
        )
    matching_registry = [
        item
        for item in _registry_entries()
        if item["username"] == PILOT_USERNAME or item["uid"] == payload["uid"]
    ]
    if matching_registry != [{"username": PILOT_USERNAME, "uid": payload["uid"]}]:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "managed GPU isolation registry is inconsistent"
        )

    projects_entry = f"{payload['project_id']}:{PILOT_DATA_ROOT / PILOT_USERNAME}"
    projid_entry = f"h100_{PILOT_USERNAME}:{payload['project_id']}"
    if projects_entry not in _safe_file_lines(
        PROJECTS_FILE
    ) or projid_entry not in _safe_file_lines(PROJID_FILE):
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "XFS project mappings are missing"
        )
    quota = _verified_project_quota(payload["project_id"], payload["quota_gb"])

    association = run_fixed(
        "sacctmgr",
        [
            "-n",
            "-P",
            "show",
            "assoc",
            "where",
            f"User={PILOT_USERNAME}",
            f"Account={payload['slurm_account']}",
            "format=User,Account,QOS,DefaultQOS",
        ],
        timeout=20,
    )
    association_ok = association.get("ok") and any(
        line.split("|")[:2] == [PILOT_USERNAME, payload["slurm_account"]]
        for line in str(association.get("stdout", "")).splitlines()
    )
    if not association_ok:
        raise LifecycleValidationError("STAGE_POSTCONDITION_FAILED", "Slurm association is missing")

    container_result = containers_inspect({"name": payload["container_name"]})
    container = container_result.get("container", {})
    state = container.get("state", {}) if isinstance(container, dict) else {}
    mounts = container.get("mounts", []) if isinstance(container, dict) else []
    approved_mount_roots = (
        str(PILOT_DATA_ROOT / PILOT_USERNAME),
        "/srv/gpu-platform/container-data/origin-pilot",
    )
    mount_sources_ok = all(
        isinstance(item, dict)
        and any(str(item.get("Source", "")).startswith(root) for root in approved_mount_roots)
        for item in mounts
    )
    container_ok = (
        container_result.get("status") == "OK"
        and isinstance(container, dict)
        and str(container.get("name", "")).lstrip("/") == payload["container_name"]
        and container.get("owner") == PILOT_USERNAME
        and container.get("cpu_limit") == float(payload["cpus"])
        and container.get("memory_limit_bytes") == payload["memory_gb"] * 1024**3
        and container.get("pids_limit") == payload["pids_limit"]
        and container.get("ssh_port") == str(payload["ssh_port"])
        and container.get("ssh_host_ip") == CONTAINER_PUBLISH_HOST
        and container.get("privileged") is False
        and container.get("network_mode") != "host"
        and container.get("pid_mode") != "host"
        and container.get("ipc_mode") != "host"
        and container.get("gpu") == "NONE"
        and not container.get("docker_socket_mounted")
        and isinstance(state, dict)
        and state.get("Running") is False
        and state.get("Status") in {"exited", "created"}
        and mount_sources_ok
    )
    if not container_ok:
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "staged container security state is invalid"
        )

    guard_enabled = run_fixed(
        "systemctl", ["is-enabled", "h100-gpu-bypass-guard.timer"], timeout=10
    )
    guard_active = run_fixed("systemctl", ["is-active", "h100-gpu-bypass-guard.timer"], timeout=10)
    if (
        str(guard_enabled.get("stdout", "")).strip() != "enabled"
        or str(guard_active.get("stdout", "")).strip() != "active"
    ):
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "GPU bypass Guard timer is not enabled and active"
        )
    guard = _verified_guard_metrics(payload["uid"])
    node = slurm_node()
    jobs = slurm_jobs()
    if (
        node.get("status") != "OK"
        or not node.get("nodes")
        or not all("DRAIN" in str(item.get("state", "")).upper() for item in node["nodes"])
        or jobs.get("status") != "OK"
        or jobs.get("jobs")
    ):
        raise LifecycleValidationError(
            "STAGE_POSTCONDITION_FAILED", "Slurm is not drained with an empty queue"
        )

    return {
        "username": PILOT_USERNAME,
        "uid": payload["uid"],
        "gid": payload["gid"],
        "shell": "/usr/sbin/nologin",
        "password": "LOCKED",
        "authorized_keys": "ABSENT",
        "host_authorized_keys": "ABSENT",
        "container_authorized_keys": "ABSENT",
        "supplemental_groups": [],
        "gpu_policy": {
            "unit": f"user-{payload['uid']}.slice",
            "path": str(dropin),
            "device_policy": "closed",
            "device_allow": [],
            "open_probe": "DENIED",
            "cuda_context_probe": "DENIED",
        },
        "quota": quota,
        "slurm": {
            "account": payload["slurm_account"],
            "qos": payload["slurm_qos"],
            "max_gpus": payload["max_gpus"],
            "node_state": "DRAIN",
            "queue": "EMPTY",
        },
        "container": {
            "name": payload["container_name"],
            "state": "STOPPED",
            "gpu": "NONE",
            "cpus": payload["cpus"],
            "memory_gb": payload["memory_gb"],
            "pids_limit": payload["pids_limit"],
            "ssh_port": payload["ssh_port"],
            "image_digest": container.get("image_digest") or container.get("image_id"),
        },
        "guard": guard,
        "ssh_key_state": "REQUIRED_BEFORE_ACTIVATION",
        "host_access": "DISABLED",
        "onboarding_state": "STAGED",
    }


def _stage_retained_resources(payload: dict[str, Any]) -> list[str]:
    """Conservatively classify host state after a failed Stage script.

    useradd can persist account database entries before reporting a later home
    creation error.  The state file is therefore not an authoritative rollback
    marker.  Any unreadable adapter is retained for manual review rather than
    being mislabeled as a complete rollback.
    """
    retained: set[str] = set()
    state_path = PILOT_STATE_ROOT / f"{PILOT_USERNAME}.state"
    if state_path.exists() or state_path.is_symlink():
        retained.add("pilot-state")
    try:
        pwd.getpwnam(PILOT_USERNAME)
        retained.add("linux-user")
    except KeyError:
        pass
    try:
        grp.getgrnam(PILOT_USERNAME)
        retained.add("linux-group")
    except KeyError:
        pass
    try:
        uid_owner = pwd.getpwuid(int(payload["uid"]))
        retained.add(
            "linux-user" if uid_owner.pw_name == PILOT_USERNAME else "approved-uid-conflict"
        )
    except KeyError:
        pass
    try:
        gid_owner = grp.getgrgid(int(payload["gid"]))
        retained.add(
            "linux-group" if gid_owner.gr_name == PILOT_USERNAME else "approved-gid-conflict"
        )
    except KeyError:
        pass

    paths = {
        "host-home": Path("/home") / PILOT_USERNAME,
        "managed-data": PILOT_DATA_ROOT / PILOT_USERNAME,
        "container-data": Path("/srv/gpu-platform/container-data") / PILOT_USERNAME,
        "container-config": PILOT_COMPOSE_ROOT / PILOT_USERNAME,
        "gpu-policy": Path(f"/etc/systemd/system/user-{payload['uid']}.slice.d/{GPU_DROPIN_NAME}"),
    }
    for label, path in paths.items():
        if path.exists() or path.is_symlink():
            retained.add(label)
    if any(
        item["username"] == PILOT_USERNAME or item["uid"] == payload["uid"]
        for item in _registry_entries()
    ):
        retained.add("gpu-registry")
    projects_entry = f"{payload['project_id']}:{PILOT_DATA_ROOT / PILOT_USERNAME}"
    projid_entry = f"h100_{PILOT_USERNAME}:{payload['project_id']}"
    if projects_entry in _safe_file_lines(PROJECTS_FILE) or projid_entry in _safe_file_lines(
        PROJID_FILE
    ):
        retained.add("xfs-project-mapping")

    association = _assoc_exists(PILOT_USERNAME)
    if association is True:
        retained.add("slurm-association")
    elif association is None:
        retained.add("slurm-association-unknown")
    container = run_fixed(
        "docker", ["container", "inspect", str(payload["container_name"])], timeout=15
    )
    if container.get("ok"):
        retained.add("container")
    elif "no such" not in str(container.get("stderr", "")).casefold():
        retained.add("container-state-unknown")
    for command, expected_absent in (("is-enabled", "disabled"), ("is-active", "inactive")):
        timer = run_fixed("systemctl", [command, "h100-gpu-bypass-guard.timer"], timeout=10)
        observed = str(timer.get("stdout", "")).strip()
        if observed and observed != expected_absent:
            retained.add("guard-timer")
        elif not observed:
            retained.add("guard-timer-state-unknown")
    return sorted(retained)


def _execute_origin_pilot_stage(request: WorkerRequest, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute the one Portal-3C write approved by the administrator."""
    if (
        request.requested_by != MANAGEMENT_USERNAME
        or request.approved_by != MANAGEMENT_USERNAME
        or request.idempotency_key != PORTAL3C_STAGE_IDEMPOTENCY_KEY
        or payload.get("approval_reference") != PORTAL3C_STAGE_APPROVAL_REFERENCE
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "STAGE_APPROVAL_BINDING_REJECTED",
                "message": "real Stage is not bound to the approved Portal-3C operation",
            },
        }
    integrity = script_integrity()
    failed_scripts = sorted(
        name
        for name in STAGE_REQUIRED_SCRIPTS
        if not integrity.get(name, {}).get("integrity_ok", False)
    )
    if failed_scripts:
        return {
            "status": "ERROR",
            "error": {
                "code": "SCRIPT_INTEGRITY_FAILED",
                "message": "fixed lifecycle script integrity check failed",
                "scripts": failed_scripts,
            },
        }

    state_path = PILOT_STATE_ROOT / f"{PILOT_USERNAME}.state"
    if state_path.exists() or state_path.is_symlink():
        try:
            summary = _stage_postcondition_summary(payload)
        except LifecycleValidationError as exc:
            return {
                "status": "ERROR",
                "error": {"code": "IDEMPOTENCY_STATE_MISMATCH", "message": str(exc)},
                "rollback_status": "REQUIRES_MANUAL_REVIEW",
            }
        return {
            "status": "SUCCEEDED",
            "handler": "user.stage",
            "idempotent_replay": True,
            "execution_enabled": True,
            "stage": summary,
        }

    try:
        preflight = _user_stage_dry_run(payload)
    except LifecycleValidationError as exc:
        return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}
    if preflight.get("status") != "DRY_RUN" or preflight.get("stage_status") != "READY":
        error = preflight.get("error", {})
        code = (
            error.get("code", "PLAN_VALUES_CHANGED")
            if isinstance(error, dict)
            else "PLAN_VALUES_CHANGED"
        )
        return {
            "status": "ERROR",
            "error": {
                "code": str(code)[:64],
                "message": "approved resource identifiers are no longer available",
            },
            "preflight": {
                "stage_status": preflight.get("stage_status", "CONFLICT"),
                "conflicts": preflight.get("conflicts", []),
            },
        }

    execution = run_allowlisted_script(
        build_user_stage_argv(payload), timeout=STAGE_EXECUTION_TIMEOUT_SECONDS
    )
    if not execution.get("ok"):
        retained_resources = _stage_retained_resources(payload)
        return {
            "status": "ERROR",
            "error": {
                "code": str(execution.get("error_code", "STAGE_EXECUTION_FAILED"))[:64],
                "message": "transactional Stage script failed",
                "exit_code": execution.get("exit_code"),
            },
            "rollback_status": "PARTIAL_RETAINED" if retained_resources else "ROLLED_BACK",
            "host_resources_retained": bool(retained_resources),
            "retained_resources": retained_resources,
        }
    try:
        summary = _stage_postcondition_summary(payload)
    except LifecycleValidationError as exc:
        return {
            "status": "ERROR",
            "error": {"code": exc.code, "message": str(exc)},
            "rollback_status": "REQUIRES_MANUAL_REVIEW",
            "host_resources_retained": True,
        }
    return {
        "status": "SUCCEEDED",
        "handler": "user.stage",
        "idempotent_replay": False,
        "execution_enabled": True,
        "stage": summary,
    }


def _read_managed_lifecycle_state(username: str) -> dict[str, str]:
    if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", username) is None or username in {
        "root",
        "origin-al",
        "codexops",
        "nobody",
    }:
        raise LifecycleValidationError(
            "CONTAINER_START_STATE_REJECTED", "managed lifecycle username is invalid"
        )
    path = PILOT_STATE_ROOT / f"{username}.state"
    try:
        before = path.lstat()
    except OSError as exc:
        raise LifecycleValidationError(
            "CONTAINER_START_STATE_REJECTED", "managed lifecycle state is unavailable"
        ) from exc
    try:
        state_group_gid = grp.getgrnam("gpu-platform-admin").gr_gid
    except KeyError as exc:
        raise LifecycleValidationError(
            "CONTAINER_START_STATE_REJECTED", "managed lifecycle state group is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != state_group_gid
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o640
        or not 0 < before.st_size <= 16 * 1024
    ):
        raise LifecycleValidationError(
            "CONTAINER_START_STATE_REJECTED", "managed lifecycle state metadata is invalid"
        )
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise LifecycleValidationError(
                    "CONTAINER_START_STATE_REJECTED", "managed lifecycle state changed during open"
                )
            content = os.read(descriptor, 16 * 1024 + 1)
        finally:
            os.close(descriptor)
    except LifecycleValidationError:
        raise
    except OSError as exc:
        raise LifecycleValidationError(
            "CONTAINER_START_STATE_REJECTED", "managed lifecycle state could not be read safely"
        ) from exc
    try:
        lines = content.decode("ascii", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise LifecycleValidationError(
            "CONTAINER_START_STATE_REJECTED", "managed lifecycle state is not ASCII"
        ) from exc
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z0-9_]{1,32}", key):
            if key in values:
                raise LifecycleValidationError(
                    "CONTAINER_START_STATE_REJECTED",
                    "managed lifecycle state contains duplicate fields",
                )
            values[key] = value[:256]
    return values


def _read_active_pilot_state(username: str) -> dict[str, str]:
    values = _read_managed_lifecycle_state(username)
    if (
        values.get("STATUS") != "ACTIVE"
        or values.get("USERNAME") != username
        or values.get("SSH_KEY_STATE") != "INSTALLED"
        or re.fullmatch(
            r"SHA256:[A-Za-z0-9+/]+(?:,SHA256:[A-Za-z0-9+/]+){0,4}",
            values.get("CONTAINER_KEY_FINGERPRINTS", ""),
        )
        is None
    ):
        raise LifecycleValidationError(
            "CONTAINER_START_STATE_REJECTED",
            "managed compute identity is not ACTIVE with a container SSH key",
        )
    return values


def _installed_key_fingerprints(path: Path, expected_uid: int, expected_gid: int) -> list[str]:
    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise LifecycleValidationError(
            "CONTAINER_START_KEY_REJECTED", "container .ssh directory is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != expected_uid
        or parent.st_gid != expected_gid
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise LifecycleValidationError(
            "CONTAINER_START_KEY_REJECTED", "container .ssh directory metadata is invalid"
        )
    try:
        before = path.lstat()
    except OSError as exc:
        raise LifecycleValidationError(
            "CONTAINER_START_KEY_REJECTED", "container authorized_keys is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != expected_uid
        or before.st_gid != expected_gid
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
        or not 0 < before.st_size <= MAX_SSH_KEY_FILE_BYTES
    ):
        raise LifecycleValidationError(
            "CONTAINER_START_KEY_REJECTED", "container authorized_keys metadata is invalid"
        )
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise LifecycleValidationError(
                    "CONTAINER_START_KEY_REJECTED", "container authorized_keys changed during open"
                )
            content = os.read(descriptor, MAX_SSH_KEY_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
    except LifecycleValidationError:
        raise
    except OSError as exc:
        raise LifecycleValidationError(
            "CONTAINER_START_KEY_REJECTED", "container authorized_keys could not be read safely"
        ) from exc
    try:
        lines = [line for line in content.decode("utf-8", errors="strict").splitlines() if line]
    except UnicodeDecodeError as exc:
        raise LifecycleValidationError(
            "CONTAINER_START_KEY_REJECTED", "container authorized_keys is not valid UTF-8"
        ) from exc
    if not 1 <= len(lines) <= MAX_APPROVED_SSH_KEYS:
        raise LifecycleValidationError(
            "CONTAINER_START_KEY_REJECTED", "container authorized_keys count is invalid"
        )
    fingerprints = [str(_validate_public_key_content(line)["fingerprint_sha256"]) for line in lines]
    if len(set(fingerprints)) != len(fingerprints):
        raise LifecycleValidationError(
            "CONTAINER_START_KEY_REJECTED", "container authorized_keys contains duplicates"
        )
    return fingerprints


def _verify_managed_container_start_preconditions(payload: dict[str, Any]) -> dict[str, Any]:
    username = str(payload["username"])
    name = str(payload["name"])
    if username != PILOT_USERNAME or name != APPROVED_STAGE_PAYLOAD["container_name"]:
        raise LifecycleValidationError(
            "CONTAINER_OWNERSHIP_REJECTED", "container is outside the approved Pilot identity"
        )
    lifecycle = _read_active_pilot_state(username)
    try:
        uid = int(lifecycle["UID"])
        gid = int(lifecycle["GID"])
        account = pwd.getpwnam(username)
    except (KeyError, ValueError) as exc:
        raise LifecycleValidationError(
            "CONTAINER_START_STATE_REJECTED", "managed Linux identity is inconsistent"
        ) from exc
    if (
        account.pw_uid != uid
        or account.pw_gid != gid
        or account.pw_shell not in {"/bin/bash", "/usr/sbin/nologin"}
    ):
        raise LifecycleValidationError(
            "CONTAINER_START_STATE_REJECTED", "managed Linux identity is not activated safely"
        )
    password = run_fixed("passwd", ["-S", username], timeout=10)
    password_fields = str(password.get("stdout", "")).split()
    if not password.get("ok") or len(password_fields) < 2 or password_fields[1] != "L":
        raise LifecycleValidationError(
            "CONTAINER_START_STATE_REJECTED", "managed Linux password is not locked"
        )
    installed_fingerprints = _installed_key_fingerprints(
        PILOT_DATA_ROOT / username / "home/.ssh/authorized_keys", uid, gid
    )
    expected_fingerprints = lifecycle["CONTAINER_KEY_FINGERPRINTS"].split(",")
    if installed_fingerprints != expected_fingerprints:
        raise LifecycleValidationError(
            "CONTAINER_START_KEY_REJECTED",
            "container authorized_keys fingerprints differ from lifecycle state",
        )

    inspected = containers_inspect({"name": name})
    container = inspected.get("container", {})
    state = container.get("state", {}) if isinstance(container, dict) else {}
    mounts = container.get("mounts", []) if isinstance(container, dict) else []
    expected_mounts = {
        (str(PILOT_DATA_ROOT / username / "home"), f"/home/{username}"),
        (str(workspace_path(uid)), "/workspace"),
        (str(PILOT_DATA_ROOT / username / "shared"), "/shared"),
        (
            f"/srv/gpu-platform/container-data/{username}/ssh-host-keys",
            "/etc/ssh/persistent",
        ),
    }
    observed_mounts = {
        (str(item.get("Source", "")), str(item.get("Destination", "")))
        for item in mounts
        if isinstance(item, dict) and item.get("Type") == "bind" and item.get("RW") is True
    }
    mounts_safe = len(mounts) == len(expected_mounts) and all(
        isinstance(item, dict)
        and item.get("Type") == "bind"
        and item.get("RW") is True
        and (str(item.get("Source", "")), str(item.get("Destination", ""))) in expected_mounts
        for item in mounts
    )
    if not (
        inspected.get("status") == "OK"
        and isinstance(container, dict)
        and str(container.get("name", "")).lstrip("/") == name
        and container.get("owner") == username
        and container.get("image") == f"h100-local/dev-container:ubuntu24.04-{username}-20260804"
        and str(container.get("image_id", "")).startswith("sha256:")
        and container.get("cpu_limit") == float(APPROVED_STAGE_PAYLOAD["cpus"])
        and container.get("memory_limit_bytes") == APPROVED_STAGE_PAYLOAD["memory_gb"] * 1024**3
        and container.get("pids_limit") == APPROVED_STAGE_PAYLOAD["pids_limit"]
        and container.get("ssh_port") == str(APPROVED_STAGE_PAYLOAD["ssh_port"])
        and container.get("ssh_host_ip") == CONTAINER_PUBLISH_HOST
        and container.get("privileged") is False
        and container.get("network_mode") != "host"
        and container.get("pid_mode") != "host"
        and container.get("ipc_mode") != "host"
        and container.get("gpu") == "NONE"
        and not container.get("docker_socket_mounted")
        and isinstance(state, dict)
        and state.get("Running") is False
        and state.get("Status") in {"exited", "created"}
        and mounts_safe
        and observed_mounts == expected_mounts
    ):
        raise LifecycleValidationError(
            "CONTAINER_START_SECURITY_REJECTED", "managed container preconditions are invalid"
        )
    return {"lifecycle": lifecycle, "container": container}


def _stop_managed_container_after_failed_start(username: str, name: str) -> bool:
    try:
        stopped = run_allowlisted_script(
            [SCRIPT_ALLOWLIST["h100-container-stop"], username], timeout=60
        )
        inspected = containers_inspect({"name": name})
        container = inspected.get("container", {})
        state = container.get("state", {}) if isinstance(container, dict) else {}
        return bool(
            stopped.get("ok")
            and inspected.get("status") == "OK"
            and isinstance(state, dict)
            and state.get("Running") is False
            and state.get("Status") in {"exited", "created"}
        )
    except Exception:
        return False


def _execute_managed_container_start(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    expected_idempotency = re.fullmatch(
        rf"container-start:{re.escape(str(payload['managed_user_id']))}:"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        request.idempotency_key,
    )
    if request.requested_by != request.approved_by or expected_idempotency is None:
        return {
            "status": "ERROR",
            "error": {
                "code": "CONTAINER_START_APPROVAL_REJECTED",
                "message": "container start is not bound to its managed identity",
            },
        }
    integrity = script_integrity()
    if not all(
        integrity.get(name, {}).get("integrity_ok", False)
        for name in ("h100-container-start", "h100-container-stop")
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "SCRIPT_INTEGRITY_FAILED",
                "message": "fixed container lifecycle script integrity check failed",
            },
        }
    try:
        _verify_managed_container_start_preconditions(payload)
        execution = run_allowlisted_script(
            [SCRIPT_ALLOWLIST["h100-container-start"], str(payload["username"])], timeout=150
        )
        if not execution.get("ok"):
            if not _stop_managed_container_after_failed_start(
                str(payload["username"]), str(payload["name"])
            ):
                raise LifecycleValidationError(
                    "CONTAINER_STOP_RECOVERY_FAILED",
                    "managed container could not be proven stopped after start failure",
                )
            raise LifecycleValidationError(
                "CONTAINER_START_FAILED", "managed container start script failed"
            )
        inspected = containers_inspect({"name": str(payload["name"])})
        container = inspected.get("container", {})
        state = container.get("state", {}) if isinstance(container, dict) else {}
        health = state.get("Health", {}) if isinstance(state, dict) else {}
        if not (
            inspected.get("status") == "OK"
            and isinstance(state, dict)
            and state.get("Running") is True
            and isinstance(health, dict)
            and health.get("Status") == "healthy"
            and isinstance(container, dict)
            and container.get("gpu") == "NONE"
            and container.get("privileged") is False
            and container.get("network_mode") != "host"
            and not container.get("docker_socket_mounted")
        ):
            if not _stop_managed_container_after_failed_start(
                str(payload["username"]), str(payload["name"])
            ):
                raise LifecycleValidationError(
                    "CONTAINER_STOP_RECOVERY_FAILED",
                    "managed container could not be proven stopped after unsafe postcondition",
                )
            raise LifecycleValidationError(
                "CONTAINER_START_POSTCONDITION_FAILED",
                "managed container did not reach a safe healthy state",
            )
        return {
            "status": "SUCCEEDED",
            "handler": "container.start",
            "name": payload["name"],
            "username": payload["username"],
            "container_state": "RUNNING",
            "container_gpu": "NONE",
        }
    except LifecycleValidationError as exc:
        return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}


def _activation_runtime_binding(payload: dict[str, Any]) -> None:
    try:
        installed = DEPLOYMENT_VERSION_PATH.read_text(encoding="ascii").strip()
    except OSError:
        installed = "SOURCE_WORKTREE"
    if installed != payload["deployment_version"]:
        raise LifecycleValidationError(
            "ACTIVATION_RUNTIME_BINDING_REJECTED",
            "API and Root Worker deployment versions differ",
        )


def _activation_lifecycle_common(payload: dict[str, Any]) -> dict[str, str]:
    return {
        "VERSION": "4",
        "USERNAME": str(payload["username"]),
        "UID": str(payload["uid"]),
        "GID": str(payload["gid"]),
        "PROJECT_ID": str(payload["project_id"]),
        "SSH_PORT": str(payload["ssh_port"]),
        "SLURM_ACCOUNT": str(payload["slurm_account"]),
        "SLURM_QOS": str(payload["slurm_qos"]),
        "MAX_GPUS": str(payload["gpu_max"]),
        "REQUEST_ID": str(payload["request_id"]),
        "PLAN_ID": str(payload["plan_id"]),
        "DRY_RUN_OPERATION_ID": str(payload["dry_run_operation_id"]),
        "DEVELOPMENT_PROFILE": str(payload["development_profile"]),
        "WORKSPACE_LAYOUT": "LEGACY_BIND_ALIAS",
        "STORAGE_ROOT": str(PILOT_DATA_ROOT / str(payload["username"])),
        "BACKING_WORKSPACE": str(PILOT_DATA_ROOT / str(payload["username"]) / "workspace"),
        "WORKSPACE_PATH": str(payload["workspace_path"]),
    }


def _validate_activation_lifecycle_common(
    lifecycle: dict[str, str], payload: dict[str, Any]
) -> None:
    expected = _activation_lifecycle_common(payload)
    mismatches = sorted(key for key, value in expected.items() if lifecycle.get(key) != value)
    if mismatches:
        raise LifecycleValidationError(
            "ACTIVATION_LIFECYCLE_BINDING_REJECTED",
            f"managed lifecycle differs from the owner-bound plan: {','.join(mismatches)}",
        )


def _activation_staged_lifecycle(payload: dict[str, Any]) -> dict[str, str]:
    lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
    _validate_activation_lifecycle_common(lifecycle, payload)
    expected = {
        "STATUS": "STAGED",
        "SSH_KEY_STATE": "REQUIRED_BEFORE_ACTIVATION",
        "LEASE_STATE": "NOT_STARTED",
        "LEASE_START": "",
        "LEASE_EXPIRES": "",
    }
    mismatches = sorted(key for key, value in expected.items() if lifecycle.get(key) != value)
    if mismatches:
        raise LifecycleValidationError(
            "ACTIVATION_STATE_REJECTED",
            f"managed compute identity is not safely STAGED: {','.join(mismatches)}",
        )
    return lifecycle


def _activation_in_progress_lifecycle(
    payload: dict[str, Any], expected_fingerprints: list[str]
) -> dict[str, str]:
    lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
    _validate_activation_lifecycle_common(lifecycle, payload)
    expected = {
        "STATUS": "ACTIVATING",
        "SSH_KEY_STATE": "INSTALLED",
        "CONTAINER_KEY_FINGERPRINTS": ",".join(expected_fingerprints),
        "ACTIVATION_OPERATION_ID": str(payload["activation_operation_id"]),
        "LEASE_ID": str(payload["lease_id"]),
        "LEASE_STATE": "NOT_STARTED",
        "LEASE_START": "",
        "LEASE_EXPIRES": "",
    }
    mismatches = sorted(key for key, value in expected.items() if lifecycle.get(key) != value)
    if mismatches:
        raise LifecycleValidationError(
            "ACTIVATION_STATE_CONFLICT",
            f"ACTIVATING lifecycle differs from this operation: {','.join(mismatches)}",
        )
    allocation_job_id = lifecycle.get("GPU_ALLOCATION_JOB_ID", "")
    allocation_uuid = lifecycle.get("GPU_ALLOCATION_UUID", "")
    if (not allocation_job_id) != (not allocation_uuid) or (
        payload["development_profile"] == CPU_DEVELOPMENT_PROFILE
        and (allocation_job_id or allocation_uuid)
    ):
        raise LifecycleValidationError(
            "ACTIVATION_STATE_CONFLICT",
            "ACTIVATING lifecycle GPU allocation binding is invalid",
        )
    if allocation_job_id and (
        not allocation_job_id.isdigit()
        or re.fullmatch(r"GPU-[0-9a-fA-F-]{32,40}", allocation_uuid) is None
    ):
        raise LifecycleValidationError(
            "ACTIVATION_STATE_CONFLICT",
            "ACTIVATING lifecycle GPU allocation coordinates are invalid",
        )
    return lifecycle


def _activation_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleValidationError(
            "ACTIVATION_LEASE_STATE_REJECTED", f"managed lifecycle {field} is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise LifecycleValidationError(
            "ACTIVATION_LEASE_STATE_REJECTED", f"managed lifecycle {field} lacks a timezone"
        )
    return parsed.astimezone(UTC)


def _activation_active_lifecycle(
    payload: dict[str, Any], expected_fingerprints: list[str]
) -> tuple[dict[str, str], datetime, datetime]:
    lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
    _validate_activation_lifecycle_common(lifecycle, payload)
    expected = {
        "STATUS": "ACTIVE",
        "SSH_KEY_STATE": "INSTALLED",
        "CONTAINER_KEY_FINGERPRINTS": ",".join(expected_fingerprints),
        "ACTIVATION_OPERATION_ID": str(payload["activation_operation_id"]),
        "LEASE_ID": str(payload["lease_id"]),
        "LEASE_STATE": "ACTIVE",
    }
    mismatches = sorted(key for key, value in expected.items() if lifecycle.get(key) != value)
    if mismatches:
        raise LifecycleValidationError(
            "ACTIVATION_STATE_CONFLICT",
            f"ACTIVE lifecycle is bound to another operation or key set: {','.join(mismatches)}",
        )
    starts_at = _activation_timestamp(lifecycle.get("LEASE_START", ""), "Lease start")
    expires_at = _activation_timestamp(lifecycle.get("LEASE_EXPIRES", ""), "Lease expiry")
    if (
        expires_at - starts_at != timedelta(seconds=STANDARD_COMPUTE_LEASE_SECONDS)
        or starts_at > datetime.now(UTC)
        or expires_at <= datetime.now(UTC)
    ):
        raise LifecycleValidationError(
            "ACTIVATION_LEASE_STATE_REJECTED",
            "managed lifecycle does not contain one current 96-hour Lease",
        )
    allocation_job_id = lifecycle.get("GPU_ALLOCATION_JOB_ID", "")
    allocation_uuid = lifecycle.get("GPU_ALLOCATION_UUID", "")
    gpu_profile = payload["development_profile"] == GPU_DEVELOPMENT_PROFILE
    if gpu_profile != bool(allocation_job_id and allocation_uuid) or (
        allocation_job_id
        and (
            not allocation_job_id.isdigit()
            or re.fullmatch(r"GPU-[0-9a-fA-F-]{32,40}", allocation_uuid) is None
        )
    ):
        raise LifecycleValidationError(
            "ACTIVATION_STATE_CONFLICT", "ACTIVE lifecycle GPU allocation binding is invalid"
        )
    return lifecycle, starts_at, expires_at


def _open_activation_state_directory() -> tuple[int, int]:
    try:
        group_gid = grp.getgrnam("gpu-platform-admin").gr_gid
    except KeyError as exc:
        raise LifecycleValidationError(
            "ACTIVATION_STATE_WRITE_FAILED", "managed lifecycle group is unavailable"
        ) from exc
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory = os.open(PILOT_STATE_ROOT, flags)
        metadata = os.fstat(directory)
    except OSError as exc:
        raise LifecycleValidationError(
            "ACTIVATION_STATE_WRITE_FAILED", "managed lifecycle directory is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != group_gid
        or stat.S_IMODE(metadata.st_mode) != 0o750
    ):
        os.close(directory)
        raise LifecycleValidationError(
            "ACTIVATION_STATE_WRITE_FAILED", "managed lifecycle directory metadata is invalid"
        )
    return directory, group_gid


def _write_activation_lifecycle_state(
    payload: dict[str, Any],
    *,
    status: str,
    fingerprints: list[str],
    starts_at: datetime | None,
    expires_at: datetime | None,
    gpu_allocation_job_id: int | None = None,
    gpu_allocation_uuid: str | None = None,
) -> None:
    has_lease = starts_at is not None and expires_at is not None
    if (
        status not in {"STAGED", "ACTIVATING", "ACTIVE"}
        or (status == "ACTIVE") != has_lease
        or (status != "ACTIVE" and has_lease)
    ):
        raise LifecycleValidationError(
            "ACTIVATION_STATE_WRITE_FAILED", "managed lifecycle transition is invalid"
        )
    if (gpu_allocation_job_id is None) != (gpu_allocation_uuid is None) or (
        gpu_allocation_job_id is not None
        and payload["development_profile"] != GPU_DEVELOPMENT_PROFILE
    ):
        raise LifecycleValidationError(
            "ACTIVATION_STATE_WRITE_FAILED", "GPU allocation transition is invalid"
        )
    values = {"VERSION": "4", "STATUS": status, **_activation_lifecycle_common(payload)}
    if status in {"ACTIVATING", "ACTIVE"}:
        values.update(
            {
                "SSH_KEY_STATE": "INSTALLED",
                "CONTAINER_KEY_FINGERPRINTS": ",".join(fingerprints),
                "ACTIVATION_OPERATION_ID": str(payload["activation_operation_id"]),
                "LEASE_ID": str(payload["lease_id"]),
                "LEASE_STATE": "ACTIVE" if status == "ACTIVE" else "NOT_STARTED",
                "LEASE_START": starts_at.astimezone(UTC).isoformat() if starts_at else "",
                "LEASE_EXPIRES": expires_at.astimezone(UTC).isoformat() if expires_at else "",
                "GPU_ALLOCATION_JOB_ID": (
                    str(gpu_allocation_job_id) if gpu_allocation_job_id is not None else ""
                ),
                "GPU_ALLOCATION_UUID": gpu_allocation_uuid or "",
            }
        )
    else:
        values.update(
            {
                "SSH_KEY_STATE": "REQUIRED_BEFORE_ACTIVATION",
                "LEASE_STATE": "NOT_STARTED",
                "LEASE_START": "",
                "LEASE_EXPIRES": "",
                "GPU_ALLOCATION_JOB_ID": "",
                "GPU_ALLOCATION_UUID": "",
            }
        )
    content = "".join(f"{key}={value}\n" for key, value in values.items()).encode("ascii")
    directory, group_gid = _open_activation_state_directory()
    temporary = (
        f".{payload['username']}.{payload['activation_operation_id']}.{secrets.token_hex(8)}.tmp"
    )
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o640, dir_fd=directory)
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, 0, group_gid)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short lifecycle state write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            f"{payload['username']}.state",
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    except OSError as exc:
        raise LifecycleValidationError(
            "ACTIVATION_STATE_WRITE_FAILED", "managed lifecycle state could not be committed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError, OSError):
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def _commit_managed_lifecycle_values(username: str, values: dict[str, str]) -> None:
    """Atomically replace one validated root-owned managed lifecycle record."""

    if (
        re.fullmatch(r"[a-z][a-z0-9-]{0,31}", username) is None
        or username in {"root", "origin-al", "codexops", "nobody"}
        or values.get("USERNAME") != username
        or not values
        or any(
            re.fullmatch(r"[A-Z0-9_]{1,32}", key) is None
            or not isinstance(value, str)
            or len(value) > 256
            or "\n" in value
            or "\r" in value
            or not value.isascii()
            for key, value in values.items()
        )
    ):
        raise LifecycleValidationError(
            "LIFECYCLE_STATE_WRITE_REJECTED",
            "managed lifecycle replacement contains invalid fields",
        )
    content = "".join(f"{key}={value}\n" for key, value in values.items()).encode("ascii")
    if not 0 < len(content) <= 16 * 1024:
        raise LifecycleValidationError(
            "LIFECYCLE_STATE_WRITE_REJECTED", "managed lifecycle replacement is too large"
        )
    try:
        directory, group_gid = _open_activation_state_directory()
    except LifecycleValidationError as exc:
        raise LifecycleValidationError(
            "LIFECYCLE_STATE_WRITE_FAILED", "managed lifecycle directory is unavailable"
        ) from exc
    temporary = f".{username}.lifecycle.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o640, dir_fd=directory)
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, 0, group_gid)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short lifecycle state write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            f"{username}.state",
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    except OSError as exc:
        raise LifecycleValidationError(
            "LIFECYCLE_STATE_WRITE_FAILED", "managed lifecycle state could not be committed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError, OSError):
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def _validate_resource_lifecycle_binding(
    lifecycle: dict[str, str],
    payload: dict[str, Any],
    fingerprints: list[str],
    *,
    allow_v2_restore: bool = False,
) -> None:
    version = lifecycle.get("VERSION")
    expected = {
        "USERNAME": str(payload["username"]),
        "UID": str(payload["uid"]),
        "GID": str(payload["gid"]),
        "SLURM_ACCOUNT": str(payload["slurm_account"]),
        "SLURM_QOS": str(payload["slurm_qos"]),
        "CONTAINER_KEY_FINGERPRINTS": ",".join(fingerprints),
    }
    mismatches = sorted(key for key, value in expected.items() if lifecycle.get(key) != value)
    supported_version = version in {"3", "4"} or (allow_v2_restore and version == "2")
    if not supported_version or mismatches:
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_BINDING_REJECTED",
            "managed lifecycle does not match the owner-bound resource",
        )
    if version in {"2", "3"}:
        if payload["development_profile"] != CPU_DEVELOPMENT_PROFILE:
            raise LifecycleValidationError(
                "RESOURCE_LIFECYCLE_BINDING_REJECTED",
                "legacy lifecycle state cannot authorize a GPU development profile",
            )
        return
    version_four = {
        "DEVELOPMENT_PROFILE": str(payload["development_profile"]),
        "WORKSPACE_LAYOUT": "LEGACY_BIND_ALIAS",
        "STORAGE_ROOT": str(PILOT_DATA_ROOT / str(payload["username"])),
        "BACKING_WORKSPACE": str(PILOT_DATA_ROOT / str(payload["username"]) / "workspace"),
        "WORKSPACE_PATH": str(payload["workspace_path"]),
    }
    if any(lifecycle.get(key) != value for key, value in version_four.items()):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_BINDING_REJECTED",
            "managed lifecycle workspace or development profile binding changed",
        )


def _resource_lifecycle_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED", f"managed lifecycle {field} is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            f"managed lifecycle {field} lacks a timezone",
        )
    return parsed.astimezone(UTC)


def _restored_lease_window(payload: dict[str, Any]) -> tuple[datetime, datetime]:
    starts_at = _resource_lifecycle_timestamp(str(payload["lease_starts_at"]), "Lease start")
    expires_at = _resource_lifecycle_timestamp(str(payload["lease_expires_at"]), "Lease expiry")
    duration = expires_at - starts_at
    now = datetime.now(UTC)
    if (
        not timedelta(seconds=1) <= duration <= timedelta(seconds=STANDARD_COMPUTE_LEASE_SECONDS)
        or starts_at > now + timedelta(seconds=30)
        or expires_at <= now
    ):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "restore does not carry one current bounded Lease",
        )
    return starts_at, expires_at


def _recycled_lease_window(payload: dict[str, Any]) -> tuple[str, datetime, datetime]:
    try:
        lease_id = str(uuid.UUID(str(payload["recycle_lease_id"])))
    except (KeyError, ValueError) as exc:
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED", "recycled Lease ID is invalid"
        ) from exc
    if lease_id != str(payload["recycle_lease_id"]):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED", "recycled Lease ID is invalid"
        )
    starts_at = _resource_lifecycle_timestamp(
        str(payload["recycle_lease_starts_at"]), "recycled Lease start"
    )
    expires_at = _resource_lifecycle_timestamp(
        str(payload["recycle_lease_expires_at"]), "recycled Lease expiry"
    )
    if not timedelta(seconds=1) <= expires_at - starts_at <= timedelta(
        seconds=STANDARD_COMPUTE_LEASE_SECONDS
    ) or expires_at > datetime.now(UTC):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED", "recycled Lease window is invalid"
        )
    return lease_id, starts_at, expires_at


def _lifecycle_fingerprints(lifecycle: dict[str, str]) -> list[str]:
    raw = lifecycle.get("CONTAINER_KEY_FINGERPRINTS", "")
    fingerprints = raw.split(",") if raw else []
    if (
        not 1 <= len(fingerprints) <= 5
        or len(set(fingerprints)) != len(fingerprints)
        or any(re.fullmatch(r"SHA256:[A-Za-z0-9+/]+", item) is None for item in fingerprints)
    ):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "managed lifecycle Container key binding is invalid",
        )
    return fingerprints


def _active_container_lease_window(payload: dict[str, Any]) -> tuple[datetime, datetime]:
    starts_at = _resource_lifecycle_timestamp(str(payload["lease_starts_at"]), "Lease start")
    expires_at = _resource_lifecycle_timestamp(str(payload["lease_expires_at"]), "Lease expiry")
    now = datetime.now(UTC)
    if (
        not timedelta(seconds=1)
        <= expires_at - starts_at
        <= timedelta(seconds=STANDARD_COMPUTE_LEASE_SECONDS)
        or starts_at > now
        or expires_at <= now
    ):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "container action does not carry one current bounded Lease",
        )
    return starts_at, expires_at


def _bind_active_container_lifecycle(
    payload: dict[str, Any],
    *,
    gpu_allocation_job_id: int | None,
    gpu_allocation_uuid: str | None,
) -> None:
    """Atomically bind one start/restart to the current Lease and GPU allocation."""

    lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
    _validate_resource_lifecycle_binding(lifecycle, payload, _lifecycle_fingerprints(lifecycle))
    if (
        lifecycle.get("STATUS") != "ACTIVE"
        or lifecycle.get("SSH_KEY_STATE") != "INSTALLED"
        or lifecycle.get("LEASE_STATE") != "ACTIVE"
    ):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "managed lifecycle is not active for a container start",
        )
    current_start = _resource_lifecycle_timestamp(
        lifecycle.get("LEASE_START", ""), "current Lease start"
    )
    current_expiry = _resource_lifecycle_timestamp(
        lifecycle.get("LEASE_EXPIRES", ""), "current Lease expiry"
    )
    if (
        not timedelta(seconds=1)
        <= current_expiry - current_start
        <= timedelta(seconds=STANDARD_COMPUTE_LEASE_SECONDS)
    ):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "managed lifecycle current Lease window is invalid",
        )
    starts_at, expires_at = _active_container_lease_window(payload)
    lease_id = str(payload["lease_id"])
    current_lease_id = lifecycle.get("LEASE_ID", "")
    same_window = current_start == starts_at and current_expiry == expires_at
    successor_window = current_expiry <= datetime.now(UTC) and starts_at == current_expiry
    if current_lease_id == lease_id:
        lease_transition_valid = same_window
    elif current_lease_id:
        lease_transition_valid = successor_window
    else:
        # VERSION=3 production records predate LEASE_ID.  They may be bound
        # only to their identical current window or to its contiguous successor.
        lease_transition_valid = same_window or successor_window
    if not lease_transition_valid:
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "container action Lease does not match the managed lifecycle",
        )
    gpu_profile = payload["development_profile"] == GPU_DEVELOPMENT_PROFILE
    if gpu_profile != bool(gpu_allocation_job_id is not None and gpu_allocation_uuid is not None):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "container action GPU lifecycle binding is incomplete",
        )
    if gpu_allocation_job_id is not None and (
        not 0 < gpu_allocation_job_id < 2**63
        or re.fullmatch(r"GPU-[0-9a-fA-F-]{32,40}", str(gpu_allocation_uuid)) is None
    ):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "container action GPU lifecycle binding is invalid",
        )
    updated = dict(lifecycle)
    updated.update(
        {
            "LEASE_ID": lease_id,
            "LEASE_START": starts_at.isoformat(),
            "LEASE_EXPIRES": expires_at.isoformat(),
            "GPU_ALLOCATION_JOB_ID": (
                str(gpu_allocation_job_id) if gpu_allocation_job_id is not None else ""
            ),
            "GPU_ALLOCATION_UUID": gpu_allocation_uuid or "",
        }
    )
    if updated != lifecycle:
        _commit_managed_lifecycle_values(str(payload["username"]), updated)


def _clear_active_gpu_lifecycle_binding(
    payload: dict[str, Any],
    *,
    expected_job_id: int,
    expected_gpu_uuid: str,
) -> None:
    lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
    _validate_resource_lifecycle_binding(lifecycle, payload, _lifecycle_fingerprints(lifecycle))
    observed_job_id = lifecycle.get("GPU_ALLOCATION_JOB_ID", "")
    observed_uuid = lifecycle.get("GPU_ALLOCATION_UUID", "")
    if (observed_job_id or observed_uuid) and (
        observed_job_id != str(expected_job_id) or observed_uuid != expected_gpu_uuid
    ):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "managed lifecycle GPU allocation differs from the stopped container",
        )
    updated = dict(lifecycle)
    updated.update({"GPU_ALLOCATION_JOB_ID": "", "GPU_ALLOCATION_UUID": ""})
    if updated != lifecycle:
        _commit_managed_lifecycle_values(str(payload["username"]), updated)


def _activate_restored_lifecycle(
    payload: dict[str, Any],
    fingerprints: list[str],
    *,
    gpu_allocation_job_id: int | None,
    gpu_allocation_uuid: str | None,
) -> dict[str, str]:
    """Bind the fixed start scripts to the new restore Lease before runtime start."""

    lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
    _validate_resource_lifecycle_binding(lifecycle, payload, fingerprints, allow_v2_restore=True)
    version = lifecycle.get("VERSION")
    status = lifecycle.get("STATUS")
    ssh_state = lifecycle.get("SSH_KEY_STATE")
    lease_state = lifecycle.get("LEASE_STATE")
    recycle_lease_id, recycle_start, recycle_expiry = _recycled_lease_window(payload)
    legacy_v2_recycled = (
        version == "2"
        and status == "ACTIVE"
        and ssh_state == "INSTALLED"
        and not any(
            lifecycle.get(field)
            for field in ("LEASE_STATE", "LEASE_ID", "LEASE_START", "LEASE_EXPIRES")
        )
    )
    legacy_expired = False
    if (
        version == "3"
        and status == "ACTIVE"
        and ssh_state == "INSTALLED"
        and lease_state == "ACTIVE"
    ):
        prior_start = _resource_lifecycle_timestamp(
            lifecycle.get("LEASE_START", ""), "prior Lease start"
        )
        prior_expiry = _resource_lifecycle_timestamp(
            lifecycle.get("LEASE_EXPIRES", ""), "prior Lease expiry"
        )
        legacy_expired = (
            prior_start == recycle_start
            and prior_expiry == recycle_expiry
            and lifecycle.get("LEASE_ID", recycle_lease_id) == recycle_lease_id
        )
    recycled = (
        version == "4"
        and status == "RECYCLED"
        and ssh_state == "SUSPENDED_BY_RECYCLE"
        and lease_state == "RECYCLE_BIN"
        and lifecycle.get("LEASE_ID") == recycle_lease_id
        and _resource_lifecycle_timestamp(
            lifecycle.get("LEASE_START", ""), "recorded recycled Lease start"
        )
        == recycle_start
        and _resource_lifecycle_timestamp(
            lifecycle.get("LEASE_EXPIRES", ""), "recorded recycled Lease expiry"
        )
        == recycle_expiry
    )
    if not legacy_v2_recycled and not legacy_expired and not recycled:
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "managed lifecycle is not a recoverable expired resource",
        )
    gpu_profile = payload["development_profile"] == GPU_DEVELOPMENT_PROFILE
    if gpu_profile != bool(gpu_allocation_job_id is not None and gpu_allocation_uuid is not None):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "restore lifecycle GPU allocation binding is incomplete",
        )
    if gpu_allocation_job_id is not None and (
        not 0 < gpu_allocation_job_id < 2**63
        or re.fullmatch(r"GPU-[0-9a-fA-F-]{32,40}", str(gpu_allocation_uuid)) is None
    ):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "restore lifecycle GPU allocation binding is invalid",
        )
    starts_at, expires_at = _restored_lease_window(payload)
    updated = dict(lifecycle)
    updated.update(
        {
            "VERSION": "4",
            "DEVELOPMENT_PROFILE": str(payload["development_profile"]),
            "WORKSPACE_LAYOUT": "LEGACY_BIND_ALIAS",
            "STORAGE_ROOT": str(PILOT_DATA_ROOT / str(payload["username"])),
            "BACKING_WORKSPACE": str(PILOT_DATA_ROOT / str(payload["username"]) / "workspace"),
            "WORKSPACE_PATH": str(payload["workspace_path"]),
            "STATUS": "ACTIVE",
            "SSH_KEY_STATE": "INSTALLED",
            "LEASE_STATE": "ACTIVE",
            "LEASE_ID": str(payload["lease_id"]),
            "LEASE_START": starts_at.isoformat(),
            "LEASE_EXPIRES": expires_at.isoformat(),
            "RESTORE_REQUEST_ID": str(payload["restore_request_id"]),
            "GPU_ALLOCATION_JOB_ID": (
                str(gpu_allocation_job_id) if gpu_allocation_job_id is not None else ""
            ),
            "GPU_ALLOCATION_UUID": gpu_allocation_uuid or "",
        }
    )
    _commit_managed_lifecycle_values(str(payload["username"]), updated)
    return lifecycle


def _validate_active_restored_lifecycle(
    payload: dict[str, Any],
    fingerprints: list[str],
    *,
    gpu_allocation_job_id: int | None,
    gpu_allocation_uuid: str | None,
) -> None:
    lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
    _validate_resource_lifecycle_binding(lifecycle, payload, fingerprints)
    starts_at, expires_at = _restored_lease_window(payload)
    expected = {
        "STATUS": "ACTIVE",
        "SSH_KEY_STATE": "INSTALLED",
        "LEASE_STATE": "ACTIVE",
        "LEASE_ID": str(payload["lease_id"]),
        "LEASE_START": starts_at.isoformat(),
        "LEASE_EXPIRES": expires_at.isoformat(),
        "RESTORE_REQUEST_ID": str(payload["restore_request_id"]),
        "GPU_ALLOCATION_JOB_ID": (
            str(gpu_allocation_job_id) if gpu_allocation_job_id is not None else ""
        ),
        "GPU_ALLOCATION_UUID": gpu_allocation_uuid or "",
    }
    if any(lifecycle.get(key) != value for key, value in expected.items()):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "running restored resource is not bound to this Lease and request",
        )


def _restore_prior_lifecycle(payload: dict[str, Any], lifecycle: dict[str, str]) -> None:
    _commit_managed_lifecycle_values(str(payload["username"]), lifecycle)


def _mark_recycled_lifecycle(
    payload: dict[str, Any], fingerprints: list[str], *, restore_rollback: bool = False
) -> None:
    lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
    _validate_resource_lifecycle_binding(lifecycle, payload, fingerprints)
    status = lifecycle.get("STATUS")
    target_lease_id = str(payload["recycle_lease_id"] if restore_rollback else payload["lease_id"])
    target_expiry = _resource_lifecycle_timestamp(
        str(payload["recycle_lease_expires_at"] if restore_rollback else payload["expires_at"]),
        "recycled Lease expiry",
    )
    target_start = (
        _resource_lifecycle_timestamp(
            str(payload["recycle_lease_starts_at"]), "recycled Lease start"
        )
        if restore_rollback
        else None
    )
    if target_expiry > datetime.now(UTC) or (
        target_start is not None
        and not timedelta(seconds=1)
        <= target_expiry - target_start
        <= timedelta(seconds=STANDARD_COMPUTE_LEASE_SECONDS)
    ):
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED", "recycled Lease window is invalid"
        )
    lifecycle_expiry = _resource_lifecycle_timestamp(
        lifecycle.get("LEASE_EXPIRES", ""), "recorded Lease expiry"
    )
    if status == "ACTIVE":
        starts_at = _resource_lifecycle_timestamp(
            lifecycle.get("LEASE_START", ""), "recorded Lease start"
        )
        expected_active_id = str(
            payload["attempted_lease_id"] if restore_rollback else payload["lease_id"]
        )
        expected_active_start = (
            _resource_lifecycle_timestamp(
                str(payload["attempted_lease_starts_at"]), "attempted Lease start"
            )
            if restore_rollback
            else starts_at
        )
        expected_active_expiry = (
            _resource_lifecycle_timestamp(
                str(payload["attempted_lease_expires_at"]), "attempted Lease expiry"
            )
            if restore_rollback
            else target_expiry
        )
        if (
            lifecycle.get("SSH_KEY_STATE") != "INSTALLED"
            or lifecycle.get("LEASE_STATE") != "ACTIVE"
            or starts_at != expected_active_start
            or lifecycle_expiry != expected_active_expiry
            or not timedelta(seconds=1)
            <= lifecycle_expiry - starts_at
            <= timedelta(seconds=STANDARD_COMPUTE_LEASE_SECONDS)
            or (
                (restore_rollback or lifecycle.get("VERSION") == "4")
                and lifecycle.get("LEASE_ID") != expected_active_id
            )
            or (
                restore_rollback
                and lifecycle.get("RESTORE_REQUEST_ID") != str(payload["restore_request_id"])
            )
        ):
            raise LifecycleValidationError(
                "RESOURCE_LIFECYCLE_STATE_REJECTED",
                "recycle request is not bound to the current active Lease",
            )
        expected_job_id = payload.get("gpu_allocation_job_id")
        expected_uuid = payload.get("gpu_allocation_uuid")
        if payload["development_profile"] == GPU_DEVELOPMENT_PROFILE:
            if (
                not isinstance(expected_job_id, int)
                or not isinstance(expected_uuid, str)
                or lifecycle.get("GPU_ALLOCATION_JOB_ID") != str(expected_job_id)
                or lifecycle.get("GPU_ALLOCATION_UUID") != expected_uuid
            ):
                raise LifecycleValidationError(
                    "RESOURCE_LIFECYCLE_STATE_REJECTED",
                    "recycle request does not match the active GPU allocation",
                )
        elif lifecycle.get("VERSION") == "4" and (
            lifecycle.get("GPU_ALLOCATION_JOB_ID") or lifecycle.get("GPU_ALLOCATION_UUID")
        ):
            raise LifecycleValidationError(
                "RESOURCE_LIFECYCLE_STATE_REJECTED",
                "CPU development lifecycle unexpectedly contains a GPU allocation",
            )
    elif status == "RECYCLED":
        if (
            lifecycle.get("SSH_KEY_STATE") != "SUSPENDED_BY_RECYCLE"
            or lifecycle.get("LEASE_STATE") != "RECYCLE_BIN"
            or lifecycle.get("LEASE_ID") != target_lease_id
            or lifecycle_expiry != target_expiry
            or (
                target_start is not None
                and _resource_lifecycle_timestamp(
                    lifecycle.get("LEASE_START", ""), "recorded recycled Lease start"
                )
                != target_start
            )
            or lifecycle.get("GPU_ALLOCATION_JOB_ID")
            or lifecycle.get("GPU_ALLOCATION_UUID")
        ):
            raise LifecycleValidationError(
                "RESOURCE_LIFECYCLE_STATE_REJECTED",
                "recycled lifecycle does not match this Lease",
            )
    else:
        raise LifecycleValidationError(
            "RESOURCE_LIFECYCLE_STATE_REJECTED",
            "managed lifecycle cannot transition to the recycle bin",
        )
    updated = dict(lifecycle)
    updated.update(
        {
            "STATUS": "RECYCLED",
            "SSH_KEY_STATE": "SUSPENDED_BY_RECYCLE",
            "LEASE_STATE": "RECYCLE_BIN",
            "LEASE_ID": target_lease_id,
            "LEASE_EXPIRES": target_expiry.isoformat(),
            "RESTORE_REQUEST_ID": "",
            "GPU_ALLOCATION_JOB_ID": "",
            "GPU_ALLOCATION_UUID": "",
        }
    )
    if target_start is not None:
        updated["LEASE_START"] = target_start.isoformat()
    _commit_managed_lifecycle_values(str(payload["username"]), updated)


def _activation_account_preflight(payload: dict[str, Any]) -> pwd.struct_passwd:
    account = _managed_account(payload)
    username = str(payload["username"])
    host_ssh = Path(account.pw_dir) / ".ssh"
    host_keys = host_ssh / "authorized_keys"
    if (
        account.pw_dir != f"/home/{username}"
        or account.pw_shell != "/usr/sbin/nologin"
        or set(_group_names(username, account.pw_gid)) != {username}
        or host_ssh.is_symlink()
        or host_keys.exists()
        or host_keys.is_symlink()
    ):
        raise LifecycleValidationError(
            "HOST_SSH_POLICY_REJECTED",
            "owner-bound activation requires nologin and absent Host authorized_keys",
        )
    return account


def _open_container_ssh_directory(payload: dict[str, Any]) -> int:
    uid = int(payload["uid"])
    gid = int(payload["gid"])
    root = PILOT_DATA_ROOT / str(payload["username"])
    paths = (root, root / "home", root / "home/.ssh")
    for path in paths:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise LifecycleValidationError(
                "CONTAINER_KEY_INSTALL_FAILED", "managed Container SSH directory is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != uid
            or metadata.st_gid != gid
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise LifecycleValidationError(
                "CONTAINER_KEY_INSTALL_FAILED",
                "managed Container SSH directory metadata is invalid",
            )
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory = os.open(paths[-1], flags)
        opened = os.fstat(directory)
    except OSError as exc:
        raise LifecycleValidationError(
            "CONTAINER_KEY_INSTALL_FAILED", "managed Container SSH directory could not be opened"
        ) from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != uid
        or opened.st_gid != gid
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        os.close(directory)
        raise LifecycleValidationError(
            "CONTAINER_KEY_INSTALL_FAILED", "managed Container SSH directory changed during open"
        )
    return directory


def _activation_key_content(records: list[dict[str, Any]]) -> bytes:
    content = b"".join(
        _read_staging_component(f"{record['record_id']}.pub", MAX_SSH_KEY_FILE_BYTES)
        for record in records
    )
    if not 0 < len(content) <= MAX_SSH_KEY_FILE_BYTES:
        raise LifecycleValidationError(
            "CONTAINER_KEY_INSTALL_FAILED", "Container authorized_keys content is invalid"
        )
    return content


def _install_activation_keys(
    payload: dict[str, Any], records: list[dict[str, Any]], fingerprints: list[str]
) -> bool:
    path = PILOT_DATA_ROOT / str(payload["username"]) / "home/.ssh/authorized_keys"
    if path.exists() or path.is_symlink():
        installed = _installed_key_fingerprints(path, int(payload["uid"]), int(payload["gid"]))
        if installed != fingerprints:
            raise LifecycleValidationError(
                "CONTAINER_KEY_CONFLICT", "Container authorized_keys differs from approved keys"
            )
        return False
    content = _activation_key_content(records)
    directory = _open_container_ssh_directory(payload)
    temporary = f".authorized_keys.{payload['activation_operation_id']}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, int(payload["uid"]), int(payload["gid"]))
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short authorized_keys write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if _component_exists(directory, "authorized_keys"):
            raise LifecycleValidationError(
                "CONTAINER_KEY_CONFLICT", "Container authorized_keys appeared during activation"
            )
        os.replace(temporary, "authorized_keys", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except LifecycleValidationError:
        raise
    except OSError as exc:
        raise LifecycleValidationError(
            "CONTAINER_KEY_INSTALL_FAILED", "Container authorized_keys could not be committed"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError, OSError):
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)
    installed = _installed_key_fingerprints(path, int(payload["uid"]), int(payload["gid"]))
    if installed != fingerprints:
        raise LifecycleValidationError(
            "CONTAINER_KEY_INSTALL_FAILED", "Container authorized_keys verification failed"
        )
    return True


def _remove_activation_keys(payload: dict[str, Any], fingerprints: list[str]) -> bool:
    path = PILOT_DATA_ROOT / str(payload["username"]) / "home/.ssh/authorized_keys"
    if not path.exists() and not path.is_symlink():
        return False
    if _installed_key_fingerprints(path, int(payload["uid"]), int(payload["gid"])) != fingerprints:
        raise LifecycleValidationError(
            "ACTIVATION_ROLLBACK_KEY_CONFLICT",
            "Container authorized_keys changed; automatic removal was refused",
        )
    directory = _open_container_ssh_directory(payload)
    try:
        before = os.stat("authorized_keys", dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise LifecycleValidationError(
                "ACTIVATION_ROLLBACK_KEY_CONFLICT",
                "Container authorized_keys changed before rollback",
            )
        os.unlink("authorized_keys", dir_fd=directory)
        os.fsync(directory)
    except LifecycleValidationError:
        raise
    except OSError as exc:
        raise LifecycleValidationError(
            "ACTIVATION_ROLLBACK_FAILED", "Container authorized_keys could not be removed"
        ) from exc
    finally:
        os.close(directory)
    if path.exists() or path.is_symlink():
        raise LifecycleValidationError(
            "ACTIVATION_ROLLBACK_FAILED", "Container authorized_keys remains after rollback"
        )
    return True


def _activation_container_security(
    payload: dict[str, Any],
    *,
    require_running: bool,
    expected_fingerprints: list[str] | None,
) -> dict[str, Any]:
    _activation_account_preflight(payload)
    container = _managed_container_security(
        {**payload, "name": str(payload["container_name"])},
        require_running=require_running,
    )
    state = container.get("state", {})
    mounts = container.get("mounts", [])
    username = str(payload["username"])
    expected_mounts = {
        (str(PILOT_DATA_ROOT / username / "home"), f"/home/{username}"),
        (str(payload["workspace_path"]), "/workspace"),
        (str(PILOT_DATA_ROOT / username / "shared"), "/shared"),
        (
            f"/srv/gpu-platform/container-data/{username}/ssh-host-keys",
            "/etc/ssh/persistent",
        ),
    }
    observed_mounts = {
        (str(item.get("Source", "")), str(item.get("Destination", "")))
        for item in mounts
        if isinstance(item, dict) and item.get("Type") == "bind" and item.get("RW") is True
    }
    health = state.get("Health", {}) if isinstance(state, dict) else {}
    if not (
        str(container.get("name", "")).lstrip("/") == payload["container_name"]
        and container.get("cpu_limit") == 8.0
        and container.get("memory_limit_bytes") == 32 * 1024**3
        and container.get("pids_limit") == 4096
        and container.get("ssh_port") == str(payload["ssh_port"])
        and container.get("ssh_host_ip") == CONTAINER_PUBLISH_HOST
        and container.get("privileged") is False
        and container.get("network_mode") != "host"
        and container.get("pid_mode") != "host"
        and container.get("ipc_mode") != "host"
        and container.get("gpu")
        == (
            "REQUESTED"
            if payload["development_profile"] == GPU_DEVELOPMENT_PROFILE and require_running
            else "NONE"
        )
        and not container.get("docker_socket_mounted")
        and isinstance(mounts, list)
        and len(mounts) == len(expected_mounts)
        and observed_mounts == expected_mounts
        and not any(
            "munge" in str(item.get("Destination", "")).casefold()
            or str(item.get("Source", "")) in {"/", "/etc/munge", "/run/munge"}
            for item in mounts
            if isinstance(item, dict)
        )
        and isinstance(state, dict)
        and state.get("Running") is require_running
        and (
            isinstance(health, dict) and health.get("Status") == "healthy"
            if require_running
            else state.get("Status") in {"created", "exited"}
        )
    ):
        raise LifecycleValidationError(
            "ACTIVATION_CONTAINER_SECURITY_REJECTED",
            "development Container differs from the fixed profile security contract",
        )
    if expected_fingerprints is not None:
        installed = _installed_key_fingerprints(
            PILOT_DATA_ROOT / username / "home/.ssh/authorized_keys",
            int(payload["uid"]),
            int(payload["gid"]),
        )
        if installed != expected_fingerprints:
            raise LifecycleValidationError(
                "ACTIVATION_CONTAINER_KEY_REJECTED",
                "Container authorized_keys differs from owner-approved key records",
            )
    return container


def _activation_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    records = validate_approved_ssh_key_records(list(payload["ssh_key_record_ids"]))
    fingerprints = [str(record["fingerprint_sha256"]) for record in records]
    if (
        fingerprints != payload["ssh_key_fingerprints"]
        or any(record["managed_user_id"] != payload["managed_user_id"] for record in records)
        or any(record["username"] not in {None, payload["username"]} for record in records)
        or any(record["scope"] != "CONTAINER" for record in records)
    ):
        raise LifecycleValidationError(
            "ACTIVATION_KEY_BINDING_REJECTED",
            "approved SSH key records are not Container-only records owned by the target",
        )
    return records, fingerprints


def _activation_container_running(payload: dict[str, Any]) -> bool:
    inspected = containers_inspect({"name": str(payload["container_name"])})
    raw_container = inspected.get("container", {})
    container = raw_container if isinstance(raw_container, dict) else {}
    raw_state = container.get("state", {})
    state = raw_state if isinstance(raw_state, dict) else {}
    running = state.get("Running")
    if (
        inspected.get("status") != "OK"
        or str(container.get("name", "")).lstrip("/") != payload["container_name"]
        or container.get("owner") != payload["username"]
        or not isinstance(running, bool)
    ):
        raise LifecycleValidationError(
            "ACTIVATION_CONTAINER_STATE_UNKNOWN",
            "development Container running state could not be authoritatively resolved",
        )
    return running


def _activation_result(
    request: WorkerRequest,
    payload: dict[str, Any],
    fingerprints: list[str],
    starts_at: datetime,
    expires_at: datetime,
    gpu_allocation_job_id: int | None,
    gpu_allocation_uuid: str | None,
    *,
    replay: bool,
) -> dict[str, Any]:
    return {
        "status": "SUCCEEDED",
        "handler": "compute.activate.self",
        "activation_operation_id": payload["activation_operation_id"],
        "managed_user_id": payload["managed_user_id"],
        "compute_request_id": payload["request_id"],
        "plan_id": payload["plan_id"],
        "username": payload["username"],
        "container_name": payload["container_name"],
        "container_state": "RUNNING",
        "container_gpu": payload["expected_gpu"],
        "gpu_allocation_job_id": gpu_allocation_job_id,
        "gpu_allocation_uuid": gpu_allocation_uuid,
        "container_cpus": 8,
        "container_memory_gb": 32,
        "container_pids_limit": 4096,
        "container_privileged": False,
        "docker_socket": "ABSENT",
        "munge": "ABSENT",
        "host_namespaces": "DISABLED",
        "container_key_fingerprints": fingerprints,
        "host_authorized_keys": "ABSENT",
        "host_shell": "/usr/sbin/nologin",
        "host_password": "LOCKED",
        "lease_starts_at": starts_at.isoformat(),
        "lease_expires_at": expires_at.isoformat(),
        "lease_duration_seconds": STANDARD_COMPUTE_LEASE_SECONDS,
        "deployment_version": payload["deployment_version"],
        "worker_request_id": request.request_id,
        "idempotent_replay": replay,
    }


def _rollback_self_activation(payload: dict[str, Any], fingerprints: list[str]) -> bool:
    lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
    _validate_activation_lifecycle_common(lifecycle, payload)
    if lifecycle.get("STATUS") == "STAGED":
        try:
            _compute_stage_postconditions(payload)
            return True
        except LifecycleValidationError:
            pass
    elif lifecycle.get("STATUS") in {"ACTIVATING", "ACTIVE"}:
        if lifecycle.get("ACTIVATION_OPERATION_ID") != payload[
            "activation_operation_id"
        ] or lifecycle.get("CONTAINER_KEY_FINGERPRINTS") != ",".join(fingerprints):
            raise LifecycleValidationError(
                "ACTIVATION_STATE_CONFLICT",
                "automatic rollback refused an ACTIVE lifecycle owned by another operation",
            )
    else:
        raise LifecycleValidationError(
            "ACTIVATION_ROLLBACK_STATE_UNKNOWN",
            "managed lifecycle is neither the bound STAGED, ACTIVATING, nor ACTIVE state",
        )
    raw_allocation_job_id = lifecycle.get("GPU_ALLOCATION_JOB_ID", "")
    raw_allocation_uuid = lifecycle.get("GPU_ALLOCATION_UUID", "")
    allocation_job_id = int(raw_allocation_job_id) if raw_allocation_job_id.isdigit() else None
    allocation_uuid = raw_allocation_uuid or None
    runtime_payload = {
        **payload,
        "gpu_allocation_job_id": allocation_job_id,
        "gpu_allocation_uuid": allocation_uuid,
    }
    inspected = containers_inspect({"name": str(payload["container_name"])})
    raw_container = inspected.get("container", {})
    container = raw_container if isinstance(raw_container, dict) else {}
    raw_state = container.get("state", {})
    state = raw_state if isinstance(raw_state, dict) else {}
    if not (
        inspected.get("status") == "OK"
        and str(container.get("name", "")).lstrip("/") == payload["container_name"]
        and container.get("owner") == payload["username"]
        and isinstance(state.get("Running"), bool)
    ):
        raise LifecycleValidationError(
            "ACTIVATION_ROLLBACK_CONTAINER_UNKNOWN",
            "development Container state could not be authoritatively resolved",
        )
    if state["Running"] is True:
        if isinstance(allocation_job_id, int) and isinstance(allocation_uuid, str):
            if _gpu_allocation_binding(payload, allocation_job_id) != allocation_uuid:
                raise LifecycleValidationError(
                    "ACTIVATION_ROLLBACK_CONTAINER_STOP_FAILED",
                    "GPU allocation binding changed before activation rollback",
                )
            stop_argv = [
                SCRIPT_ALLOWLIST["h100-container-gpu-runtime"],
                "stop",
                str(payload["username"]),
                str(allocation_job_id),
                allocation_uuid,
            ]
        else:
            stop_argv = [SCRIPT_ALLOWLIST["h100-container-stop"], str(payload["username"])]
        stopped = run_allowlisted_script(stop_argv, timeout=150)
        if not stopped.get("ok"):
            raise LifecycleValidationError(
                "ACTIVATION_ROLLBACK_CONTAINER_STOP_FAILED",
                "development Container could not be stopped during activation rollback",
            )
    if isinstance(allocation_job_id, int):
        _cancel_gpu_development_allocation(runtime_payload, allocation_job_id)
    _remove_activation_keys(payload, fingerprints)
    _write_activation_lifecycle_state(
        payload,
        status="STAGED",
        fingerprints=[],
        starts_at=None,
        expires_at=None,
    )
    _compute_stage_postconditions(payload)
    return False


def _execute_self_compute_activation(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    expected_key = f"compute-activate:{payload['activation_operation_id']}"
    if (
        request.requested_by != payload["owner_login"]
        or request.approved_by != payload["owner_login"]
        or request.idempotency_key != expected_key
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "ACTIVATION_OWNER_BINDING_REJECTED",
                "message": "activation request is not bound to its authenticated owner",
            },
            "rollback_status": "NOT_REQUIRED",
        }
    integrity = script_integrity()
    required_scripts = set(SELF_ACTIVATE_REQUIRED_SCRIPTS)
    if payload["development_profile"] == GPU_DEVELOPMENT_PROFILE:
        required_scripts.add("h100-container-gpu-runtime")
    failed = sorted(
        name for name in required_scripts if not integrity.get(name, {}).get("integrity_ok", False)
    )
    if failed:
        return {
            "status": "ERROR",
            "error": {
                "code": "SCRIPT_INTEGRITY_FAILED",
                "message": "fixed activation lifecycle integrity check failed",
                "scripts": failed,
            },
            "rollback_status": "NOT_REQUIRED",
        }
    mutated = False
    fingerprints: list[str] = list(payload["ssh_key_fingerprints"])
    try:
        _activation_runtime_binding(payload)
        records, fingerprints = _activation_records(payload)
        lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
        if lifecycle.get("STATUS") == "ACTIVE":
            active_lifecycle, starts_at, expires_at = _activation_active_lifecycle(
                payload, fingerprints
            )
            raw_job_id = active_lifecycle.get("GPU_ALLOCATION_JOB_ID", "")
            allocation_job_id = int(raw_job_id) if raw_job_id.isdigit() else None
            allocation_uuid = active_lifecycle.get("GPU_ALLOCATION_UUID") or None
            runtime_payload = {
                **payload,
                "gpu_allocation_job_id": allocation_job_id,
                "gpu_allocation_uuid": allocation_uuid,
            }
            _activation_container_security(
                runtime_payload,
                require_running=True,
                expected_fingerprints=fingerprints,
            )
            if isinstance(allocation_job_id, int) and (
                _gpu_allocation_binding(payload, allocation_job_id) != allocation_uuid
            ):
                raise LifecycleValidationError(
                    "GPU_ALLOCATION_POSTCONDITION_FAILED",
                    "active container GPU differs from its live Slurm allocation",
                )
            return _activation_result(
                request,
                payload,
                fingerprints,
                starts_at,
                expires_at,
                allocation_job_id,
                allocation_uuid,
                replay=True,
            )
        if lifecycle.get("STATUS") == "ACTIVATING":
            lifecycle = _activation_in_progress_lifecycle(payload, fingerprints)
            raw_job_id = lifecycle.get("GPU_ALLOCATION_JOB_ID", "")
            allocation_job_id = int(raw_job_id) if raw_job_id.isdigit() else None
            allocation_uuid = lifecycle.get("GPU_ALLOCATION_UUID") or None
            mutated = True
        else:
            _activation_staged_lifecycle(payload)
            _compute_stage_postconditions(payload)
            mutated = True
            _install_activation_keys(payload, records, fingerprints)
            _write_activation_lifecycle_state(
                payload,
                status="ACTIVATING",
                fingerprints=fingerprints,
                starts_at=None,
                expires_at=None,
            )
            allocation_job_id = None
            allocation_uuid = None
        starts_at = datetime.now(UTC)
        expires_at = starts_at + timedelta(seconds=STANDARD_COMPUTE_LEASE_SECONDS)
        runtime_payload = {
            **payload,
            "lease_expires_at": expires_at.isoformat(),
            "gpu_allocation_job_id": allocation_job_id,
            "gpu_allocation_uuid": allocation_uuid,
        }
        running = _activation_container_running(payload)
        _activation_container_security(
            runtime_payload,
            require_running=running,
            expected_fingerprints=fingerprints,
        )
        if payload["development_profile"] == GPU_DEVELOPMENT_PROFILE:
            if allocation_job_id is None:
                allocation_job_id, allocation_uuid = _submit_gpu_development_allocation(
                    runtime_payload
                )
                runtime_payload.update(
                    {
                        "gpu_allocation_job_id": allocation_job_id,
                        "gpu_allocation_uuid": allocation_uuid,
                    }
                )
                _write_activation_lifecycle_state(
                    payload,
                    status="ACTIVATING",
                    fingerprints=fingerprints,
                    starts_at=None,
                    expires_at=None,
                    gpu_allocation_job_id=allocation_job_id,
                    gpu_allocation_uuid=allocation_uuid,
                )
            elif _gpu_allocation_binding(payload, allocation_job_id) != allocation_uuid:
                raise LifecycleValidationError(
                    "GPU_ALLOCATION_POSTCONDITION_FAILED",
                    "activation GPU differs from its live Slurm allocation",
                )
        if not running:
            start_argv = (
                [
                    SCRIPT_ALLOWLIST["h100-container-gpu-runtime"],
                    "start",
                    str(payload["username"]),
                    str(allocation_job_id),
                    str(allocation_uuid),
                ]
                if payload["development_profile"] == GPU_DEVELOPMENT_PROFILE
                else [SCRIPT_ALLOWLIST["h100-container-start"], str(payload["username"])]
            )
            started = run_allowlisted_script(start_argv, timeout=150)
            if not started.get("ok"):
                raise LifecycleValidationError(
                    "CONTAINER_START_FAILED", "development Container failed to start"
                )
        _activation_container_security(
            runtime_payload,
            require_running=True,
            expected_fingerprints=fingerprints,
        )
        isolation = run_allowlisted_script(
            [SCRIPT_ALLOWLIST["h100-user-gpu-isolation"], "verify", str(payload["username"])],
            timeout=60,
        )
        if not isolation.get("ok"):
            raise LifecycleValidationError(
                "GPU_ISOLATION_FAILED", "per-UID GPU isolation verification failed"
            )
        _write_activation_lifecycle_state(
            payload,
            status="ACTIVE",
            fingerprints=fingerprints,
            starts_at=starts_at,
            expires_at=expires_at,
            gpu_allocation_job_id=allocation_job_id,
            gpu_allocation_uuid=allocation_uuid,
        )
        _activation_active_lifecycle(payload, fingerprints)
        return _activation_result(
            request,
            payload,
            fingerprints,
            starts_at,
            expires_at,
            allocation_job_id,
            allocation_uuid,
            replay=False,
        )
    except (LifecycleValidationError, OSError) as exc:
        rollback_status = "NOT_REQUIRED"
        if mutated:
            try:
                _rollback_self_activation(payload, fingerprints)
                rollback_status = "ROLLED_BACK"
            except LifecycleValidationError, OSError:
                rollback_status = "REQUIRES_MANUAL_REVIEW"
        return {
            "status": "ERROR",
            "error": {
                "code": getattr(exc, "code", "ACTIVATION_FAILED"),
                "message": str(exc)[:512],
            },
            "rollback_status": rollback_status,
        }


def _execute_self_compute_activation_rollback(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    expected_key = f"compute-activate-rollback:{payload['activation_operation_id']}"
    if (
        request.requested_by != payload["owner_login"]
        or request.approved_by != payload["owner_login"]
        or request.idempotency_key != expected_key
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "ACTIVATION_ROLLBACK_BINDING_REJECTED",
                "message": "activation rollback is not bound to its owner and operation",
            },
            "rollback_status": "REQUIRES_MANUAL_REVIEW",
        }
    integrity = script_integrity()
    if not all(
        integrity.get(name, {}).get("integrity_ok", False)
        for name in SELF_ACTIVATE_REQUIRED_SCRIPTS
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "SCRIPT_INTEGRITY_FAILED",
                "message": "fixed activation rollback integrity check failed",
            },
            "rollback_status": "REQUIRES_MANUAL_REVIEW",
        }
    try:
        _records, fingerprints = _activation_records(payload)
        replay = _rollback_self_activation(payload, fingerprints)
        return {
            "status": "SUCCEEDED",
            "handler": "compute.activate.self.rollback",
            "activation_operation_id": payload["activation_operation_id"],
            "managed_user_id": payload["managed_user_id"],
            "username": payload["username"],
            "container_state": "STOPPED",
            "host_authorized_keys": "ABSENT",
            "container_authorized_keys": "ABSENT",
            "lease_state": "NOT_STARTED",
            "rollback_status": "ROLLED_BACK",
            "idempotent_replay": replay,
        }
    except (LifecycleValidationError, OSError) as exc:
        return {
            "status": "ERROR",
            "error": {
                "code": getattr(exc, "code", "ACTIVATION_ROLLBACK_FAILED"),
                "message": str(exc)[:512],
            },
            "rollback_status": "REQUIRES_MANUAL_REVIEW",
        }


def _activation_fingerprints(
    key_records: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    host_records, container_records = _activation_target_records(key_records)
    return (
        [str(record["fingerprint_sha256"]) for record in host_records],
        [str(record["fingerprint_sha256"]) for record in container_records],
    )


def _server_public_key_fingerprint(path: Path) -> str:
    if path not in {HOST_ED25519_PUBLIC_KEY, CONTAINER_ED25519_PUBLIC_KEY}:
        raise LifecycleValidationError(
            "SSH_SERVER_FINGERPRINT_REJECTED", "SSH server public-key path is not approved"
        )
    try:
        before = path.lstat()
    except OSError as exc:
        raise LifecycleValidationError(
            "SSH_SERVER_FINGERPRINT_FAILED", "SSH server public key is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or before.st_nlink != 1
        or bool(before.st_mode & 0o022)
        or not 0 < before.st_size <= MAX_SSH_KEY_FILE_BYTES
    ):
        raise LifecycleValidationError(
            "SSH_SERVER_FINGERPRINT_FAILED", "SSH server public-key metadata is invalid"
        )
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise LifecycleValidationError(
                    "SSH_SERVER_FINGERPRINT_FAILED",
                    "SSH server public key changed during verification",
                )
            content = os.read(descriptor, MAX_SSH_KEY_FILE_BYTES + 1)
        finally:
            os.close(descriptor)
        public_key = content.decode("utf-8", errors="strict").rstrip("\n")
    except LifecycleValidationError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise LifecycleValidationError(
            "SSH_SERVER_FINGERPRINT_FAILED", "SSH server public key could not be read safely"
        ) from exc
    if "\n" in public_key or "\r" in public_key:
        raise LifecycleValidationError(
            "SSH_SERVER_FINGERPRINT_FAILED", "SSH server public-key file is not canonical"
        )
    validated = _validate_public_key_content(public_key)
    if validated["key_type"] != "ssh-ed25519":
        raise LifecycleValidationError(
            "SSH_SERVER_FINGERPRINT_FAILED", "approved SSH server identity key is not ED25519"
        )
    return str(validated["fingerprint_sha256"])


def _container_ssh_effective_policy(name: str) -> dict[str, Any]:
    if name != APPROVED_STAGE_PAYLOAD["container_name"]:
        raise LifecycleValidationError(
            "CONTAINER_SSH_POLICY_REJECTED", "container SSH policy target is not approved"
        )
    result = run_fixed(
        "docker",
        [
            "exec",
            name,
            "/usr/sbin/sshd",
            "-T",
            "-C",
            (
                f"user={PILOT_USERNAME},host={SSH_REPRESENTATIVE_HOST},"
                f"addr={SSH_REPRESENTATIVE_CLIENT_IP}"
            ),
        ],
        timeout=15,
    )
    if not result.get("ok"):
        raise LifecycleValidationError(
            "CONTAINER_SSH_POLICY_FAILED", "container sshd effective policy is unavailable"
        )
    effective = _parse_sshd_effective_config(str(result.get("stdout", "")))
    policy = {
        "pubkey_authentication": effective["pubkeyauthentication"] == "yes",
        "password_authentication": effective["passwordauthentication"] == "yes",
        "keyboard_interactive_authentication": effective["kbdinteractiveauthentication"] == "yes",
        "authentication_methods": effective["authenticationmethods"],
        "permit_root_login": effective["permitrootlogin"],
        "authorized_keys_file": effective["authorizedkeysfile"],
        "status": "PASSING",
        "source": "CONTAINER_SSHD_EFFECTIVE_CONFIG",
        "representative_user": PILOT_USERNAME,
        "representative_host": SSH_REPRESENTATIVE_HOST,
        "representative_client_address": SSH_REPRESENTATIVE_CLIENT_IP,
    }
    if not (
        policy["pubkey_authentication"] is True
        and policy["password_authentication"] is False
        and policy["keyboard_interactive_authentication"] is False
        and policy["authentication_methods"] == "publickey"
        and policy["permit_root_login"] == "no"
        and policy["authorized_keys_file"] == ".ssh/authorized_keys"
    ):
        raise LifecycleValidationError(
            "CONTAINER_SSH_POLICY_FAILED", "container SSH is not public-key-only"
        )
    return policy


def _approved_container_listener() -> dict[str, Any]:
    result = run_fixed(
        "ss", ["-H", "-lnt", f"sport = :{APPROVED_STAGE_PAYLOAD['ssh_port']}"], timeout=10
    )
    lines = [line.split() for line in str(result.get("stdout", "")).splitlines() if line.strip()]
    local_endpoints = [fields[3] for fields in lines if len(fields) >= 5]
    expected = f"{CONTAINER_PUBLISH_HOST}:{APPROVED_STAGE_PAYLOAD['ssh_port']}"
    if not result.get("ok") or local_endpoints != [expected]:
        raise LifecycleValidationError(
            "CONTAINER_SSH_LISTENER_FAILED", "container SSH listener differs from approval"
        )
    return {
        "address": PUBLIC_ACCESS_HOST,
        "port": APPROVED_STAGE_PAYLOAD["ssh_port"],
        "status": "LISTENING",
    }


def _host_ssh_server_status() -> dict[str, Any]:
    syntax = run_fixed("sshd", ["-t"], timeout=10)
    service = run_fixed("systemctl", ["is-active", "ssh.service"], timeout=10)
    listeners = run_fixed("ss", ["-H", "-lnt", "sport = :22"], timeout=10)
    if (
        not syntax.get("ok")
        or str(service.get("stdout", "")).strip() != "active"
        or not listeners.get("ok")
        or not str(listeners.get("stdout", "")).strip()
    ):
        raise LifecycleValidationError("HOST_SSH_SERVER_FAILED", "host SSH service is not ready")
    return {
        "service": "ssh.service",
        "service_state": "ACTIVE",
        "config_validation": "PASSED",
        "approved_address": MANAGEMENT_IP,
        "port": 22,
        "status": "READY_FOR_CLIENT_VALIDATION",
    }


def _activate_postcondition_summary(
    key_records: list[dict[str, Any]],
    management_before: dict[str, dict[str, str]],
    *,
    expected_slurm_state: str = "DRAIN",
) -> dict[str, Any]:
    if expected_slurm_state not in {"DRAIN", "IDLE"}:
        raise LifecycleValidationError(
            "ACTIVATE_SLURM_GATE_FAILED", "unsupported fixed Slurm state expectation"
        )
    lifecycle = _read_active_pilot_state(PILOT_USERNAME)
    host_fingerprints, container_fingerprints = _activation_fingerprints(key_records)
    expected_state = {
        "VERSION": "2",
        "STATUS": "ACTIVE",
        "USERNAME": PILOT_USERNAME,
        "UID": str(APPROVED_STAGE_PAYLOAD["uid"]),
        "GID": str(APPROVED_STAGE_PAYLOAD["gid"]),
        "PROJECT_ID": str(APPROVED_STAGE_PAYLOAD["project_id"]),
        "SSH_PORT": str(APPROVED_STAGE_PAYLOAD["ssh_port"]),
        "SLURM_ACCOUNT": APPROVED_STAGE_PAYLOAD["slurm_account"],
        "SLURM_QOS": APPROVED_STAGE_PAYLOAD["slurm_qos"],
        "SSH_KEY_STATE": "INSTALLED",
        "HOST_KEY_FINGERPRINTS": ",".join(host_fingerprints),
        "CONTAINER_KEY_FINGERPRINTS": ",".join(container_fingerprints),
    }
    if any(lifecycle.get(field) != value for field, value in expected_state.items()):
        raise LifecycleValidationError(
            "ACTIVATE_POSTCONDITION_FAILED", "ACTIVE lifecycle state differs from approval"
        )

    try:
        account = pwd.getpwnam(PILOT_USERNAME)
        private_group = grp.getgrnam(PILOT_USERNAME)
    except KeyError as exc:
        raise LifecycleValidationError(
            "ACTIVATE_POSTCONDITION_FAILED", "activated Linux identity is unavailable"
        ) from exc
    if (
        account.pw_uid != APPROVED_STAGE_PAYLOAD["uid"]
        or account.pw_gid != APPROVED_STAGE_PAYLOAD["gid"]
        or private_group.gr_gid != APPROVED_STAGE_PAYLOAD["gid"]
        or account.pw_dir != f"/home/{PILOT_USERNAME}"
        or account.pw_shell != "/bin/bash"
    ):
        raise LifecycleValidationError(
            "ACTIVATE_POSTCONDITION_FAILED", "activated Linux identity differs from approval"
        )
    groups = _group_names(PILOT_USERNAME, account.pw_gid)
    if set(groups) != {PILOT_USERNAME} or set(groups) & FORBIDDEN_PILOT_GROUPS:
        raise LifecycleValidationError(
            "ACTIVATE_POSTCONDITION_FAILED", "activated identity has a privileged group"
        )
    password = run_fixed("passwd", ["-S", PILOT_USERNAME], timeout=10)
    password_fields = str(password.get("stdout", "")).split()
    if not password.get("ok") or len(password_fields) < 2 or password_fields[1] != "L":
        raise LifecycleValidationError(
            "ACTIVATE_POSTCONDITION_FAILED", "activated Linux password is not locked"
        )

    observed_host = _installed_key_fingerprints(
        Path(account.pw_dir) / ".ssh" / "authorized_keys", account.pw_uid, account.pw_gid
    )
    observed_container = _installed_key_fingerprints(
        PILOT_DATA_ROOT / PILOT_USERNAME / "home/.ssh/authorized_keys",
        account.pw_uid,
        account.pw_gid,
    )
    if observed_host != host_fingerprints or observed_container != container_fingerprints:
        raise LifecycleValidationError(
            "ACTIVATE_KEY_POSTCONDITION_FAILED", "installed SSH fingerprints differ from approval"
        )

    host_ssh_policy = managed_host_ssh_policy(PILOT_USERNAME)
    management_after = {
        MANAGEMENT_USERNAME: _sshd_effective_config(MANAGEMENT_USERNAME),
        "codexops": _sshd_effective_config("codexops"),
    }
    for username, before in management_before.items():
        validate_ssh_policy_no_regression(username, before, management_after[username])

    isolation = run_allowlisted_script(
        [SCRIPT_ALLOWLIST["h100-user-gpu-isolation"], "verify", PILOT_USERNAME], timeout=60
    )
    if not isolation.get("ok"):
        raise LifecycleValidationError(
            "ACTIVATE_GPU_POLICY_FAILED", "per-UID GPU isolation verification failed"
        )
    quota = _verified_project_quota(
        APPROVED_STAGE_PAYLOAD["project_id"], APPROVED_STAGE_PAYLOAD["quota_gb"]
    )
    association = run_fixed(
        "sacctmgr",
        [
            "-n",
            "-P",
            "show",
            "assoc",
            "where",
            f"User={PILOT_USERNAME}",
            f"Account={APPROVED_STAGE_PAYLOAD['slurm_account']}",
            "format=User,Account,QOS,DefaultQOS",
        ],
        timeout=20,
    )
    if not association.get("ok") or not any(
        fields[:2] == [PILOT_USERNAME, APPROVED_STAGE_PAYLOAD["slurm_account"]]
        and APPROVED_STAGE_PAYLOAD["slurm_qos"] in fields[2:]
        for fields in (line.split("|") for line in str(association.get("stdout", "")).splitlines())
    ):
        raise LifecycleValidationError(
            "ACTIVATE_SLURM_ASSOCIATION_FAILED", "Slurm association differs from approval"
        )
    qos = run_fixed(
        "sacctmgr",
        [
            "-n",
            "-P",
            "show",
            "qos",
            APPROVED_STAGE_PAYLOAD["slurm_qos"],
            "format=Name,MaxTRESPerUser",
        ],
        timeout=20,
    )
    if not qos.get("ok") or not any(
        line.startswith(f"{APPROVED_STAGE_PAYLOAD['slurm_qos']}|") and "gres/gpu=1" in line
        for line in str(qos.get("stdout", "")).splitlines()
    ):
        raise LifecycleValidationError(
            "ACTIVATE_SLURM_LIMIT_FAILED", "Slurm max GPU limit differs from approval"
        )

    guard_start = run_fixed("systemctl", ["start", "h100-gpu-bypass-guard.service"], timeout=90)
    if not guard_start.get("ok"):
        raise LifecycleValidationError("ACTIVATE_GUARD_FAILED", "GPU bypass Guard service failed")
    guard = _verified_guard_metrics(APPROVED_STAGE_PAYLOAD["uid"])
    node = slurm_node()
    jobs = slurm_jobs()
    node_rows = node.get("nodes")
    single_node = (
        node_rows[0]
        if isinstance(node_rows, list) and len(node_rows) == 1 and isinstance(node_rows[0], dict)
        else None
    )
    observed_node_state = (
        str(single_node.get("state", "")).upper() if isinstance(single_node, dict) else ""
    )
    observed_reason = (
        str(single_node.get("reason") or "").strip() if isinstance(single_node, dict) else ""
    )
    state_matches = (
        "DRAIN" in observed_node_state
        if expected_slurm_state == "DRAIN"
        else observed_node_state == "IDLE"
        and observed_reason.casefold() in {"", "none", "(null)", "n/a"}
    )
    if (
        node.get("status") != "OK"
        or not isinstance(single_node, dict)
        or single_node.get("name") != "sagsh100server"
        or not state_matches
        or jobs.get("status") != "OK"
        or jobs.get("jobs")
    ):
        raise LifecycleValidationError(
            "ACTIVATE_SLURM_GATE_FAILED",
            f"Slurm is not exactly {expected_slurm_state} with an empty queue",
        )

    inspected = containers_inspect({"name": APPROVED_STAGE_PAYLOAD["container_name"]})
    container = inspected.get("container", {})
    state = container.get("state", {}) if isinstance(container, dict) else {}
    health = state.get("Health", {}) if isinstance(state, dict) else {}
    mounts = container.get("mounts", []) if isinstance(container, dict) else []
    expected_mounts = {
        (str(PILOT_DATA_ROOT / PILOT_USERNAME / "home"), f"/home/{PILOT_USERNAME}"),
        (str(workspace_path(int(APPROVED_STAGE_PAYLOAD["uid"]))), "/workspace"),
        (
            f"/srv/gpu-platform/container-data/{PILOT_USERNAME}/ssh-host-keys",
            "/etc/ssh/persistent",
        ),
    }
    observed_mounts = {
        (str(item.get("Source", "")), str(item.get("Destination", "")))
        for item in mounts
        if isinstance(item, dict) and item.get("Type") == "bind" and item.get("RW") is True
    }
    if not (
        inspected.get("status") == "OK"
        and isinstance(container, dict)
        and str(container.get("name", "")).lstrip("/") == APPROVED_STAGE_PAYLOAD["container_name"]
        and container.get("owner") == PILOT_USERNAME
        and container.get("cpu_limit") == float(APPROVED_STAGE_PAYLOAD["cpus"])
        and container.get("memory_limit_bytes") == APPROVED_STAGE_PAYLOAD["memory_gb"] * 1024**3
        and container.get("pids_limit") == APPROVED_STAGE_PAYLOAD["pids_limit"]
        and container.get("ssh_port") == str(APPROVED_STAGE_PAYLOAD["ssh_port"])
        and container.get("ssh_host_ip") == CONTAINER_PUBLISH_HOST
        and container.get("privileged") is False
        and container.get("network_mode") != "host"
        and container.get("pid_mode") != "host"
        and container.get("ipc_mode") != "host"
        and container.get("gpu") == "NONE"
        and not container.get("docker_socket_mounted")
        and isinstance(state, dict)
        and state.get("Running") is True
        and state.get("Status") == "running"
        and isinstance(health, dict)
        and health.get("Status") == "healthy"
        and len(mounts) == len(expected_mounts)
        and observed_mounts == expected_mounts
    ):
        raise LifecycleValidationError(
            "ACTIVATE_CONTAINER_POSTCONDITION_FAILED", "running container differs from approval"
        )
    container_ssh_policy = _container_ssh_effective_policy(APPROVED_STAGE_PAYLOAD["container_name"])
    listener = _approved_container_listener()
    host_server = _host_ssh_server_status()

    return {
        "username": PILOT_USERNAME,
        "uid": APPROVED_STAGE_PAYLOAD["uid"],
        "gid": APPROVED_STAGE_PAYLOAD["gid"],
        "onboarding_state": "ACTIVE",
        "shell": "/bin/bash",
        "password": "LOCKED",
        "host_access": "ENABLED",
        "ssh_key_state": "INSTALLED",
        "host_authorized_keys": "INSTALLED",
        "container_authorized_keys": "INSTALLED",
        "host_key_fingerprints": observed_host,
        "container_key_fingerprints": observed_container,
        "host_ssh_policy": host_ssh_policy,
        "container_ssh_policy": container_ssh_policy,
        "host_ssh_server": host_server,
        "container_ssh_server": {
            "service": "sshd",
            "service_state": "ACTIVE",
            "internal_port": 22,
            "bind": listener,
            "status": "READY_FOR_CLIENT_VALIDATION",
        },
        "host_server_fingerprint": _server_public_key_fingerprint(HOST_ED25519_PUBLIC_KEY),
        "container_server_fingerprint": _server_public_key_fingerprint(
            CONTAINER_ED25519_PUBLIC_KEY
        ),
        "management_ssh_policy": {
            MANAGEMENT_USERNAME: "UNCHANGED",
            "codexops": "UNCHANGED",
        },
        "gpu_policy": {
            "unit": f"user-{APPROVED_STAGE_PAYLOAD['uid']}.slice",
            "device_policy": "closed",
            "device_allow": [],
            "status": "PASSING",
            "out_of_job_gpu": "DENIED",
        },
        "quota": quota,
        "slurm": {
            "account": APPROVED_STAGE_PAYLOAD["slurm_account"],
            "qos": APPROVED_STAGE_PAYLOAD["slurm_qos"],
            "max_gpus": APPROVED_STAGE_PAYLOAD["max_gpus"],
            "node_state": expected_slurm_state,
            "queue": "EMPTY",
            "drain_reason": observed_reason or "NONE",
        },
        "container": {
            "name": APPROVED_STAGE_PAYLOAD["container_name"],
            "state": "RUNNING",
            "gpu": "NONE",
            "cpus": APPROVED_STAGE_PAYLOAD["cpus"],
            "memory_gb": APPROVED_STAGE_PAYLOAD["memory_gb"],
            "pids_limit": APPROVED_STAGE_PAYLOAD["pids_limit"],
            "ssh_address": PUBLIC_ACCESS_HOST,
            "ssh_port": APPROVED_STAGE_PAYLOAD["ssh_port"],
        },
        "guard": guard,
        "host_ssh_client_validation": "PENDING",
        "container_ssh_client_validation": "PENDING",
    }


def _portal3e_final_payload_matches(payload: dict[str, Any]) -> bool:
    return payload == {
        "managed_user_id": PORTAL3E_FINAL_MANAGED_USER_ID,
        "approved_ssh_key_record_ids": [PORTAL3E_FINAL_KEY_RECORD_ID],
        "expected_state": "STAGED",
        "approval_reference": PORTAL3E_FINAL_APPROVAL_REFERENCE,
        "dry_run_operation_id": PORTAL3E_FINAL_DRY_RUN_OPERATION_ID,
    }


def _portal3f_active_preflight(expected_slurm_state: str = "DRAIN") -> dict[str, Any]:
    """Re-read every ACTIVE identity boundary before a Portal-3F decision."""
    management_before = {
        MANAGEMENT_USERNAME: _sshd_effective_config(MANAGEMENT_USERNAME),
        "codexops": _sshd_effective_config("codexops"),
    }
    active = _activate_postcondition_summary(
        [{"scope": "BOTH", "fingerprint_sha256": PORTAL3E_FINAL_KEY_FINGERPRINT}],
        management_before,
        expected_slurm_state=expected_slurm_state,
    )
    gpus = gpu_list()
    health = gpu_health()
    failed = systemd_failed()
    gpu_rows = gpus.get("gpus", [])
    dcgm_rows = health.get("per_gpu", [])
    if not (
        gpus.get("status") == "OK"
        and gpus.get("count") == 4
        and isinstance(gpu_rows, list)
        and len(gpu_rows) == 4
        and all(
            isinstance(row, dict) and str(row.get("mig.mode.current", "")).lower() == "disabled"
            for row in gpu_rows
        )
        and health.get("status") == "OK"
        and isinstance(dcgm_rows, list)
        and len(dcgm_rows) == 4
        and all(
            isinstance(row, dict) and str(row.get("dcgm_status", "")).casefold() == "pass"
            for row in dcgm_rows
        )
        and isinstance(health.get("kernel_errors"), dict)
        and health["kernel_errors"].get("status") == "CLEAR"
        and failed.get("status") == "OK"
        and failed.get("count") == 0
    ):
        raise LifecycleValidationError(
            "PORTAL3F_HEALTH_PREFLIGHT_FAILED",
            "GPU/DCGM/kernel/systemd health differs from the approved Pilot baseline",
        )
    return {
        "username": active["username"],
        "uid": active["uid"],
        "gid": active["gid"],
        "onboarding_state": active["onboarding_state"],
        "shell": active["shell"],
        "password": active["password"],
        "ssh_key_state": active["ssh_key_state"],
        "host_authorized_keys": active["host_authorized_keys"],
        "container_authorized_keys": active["container_authorized_keys"],
        "host_key_fingerprints": active["host_key_fingerprints"],
        "container_key_fingerprints": active["container_key_fingerprints"],
        "host_ssh_policy": active["host_ssh_policy"],
        "container_ssh_policy": active["container_ssh_policy"],
        "host_ssh_server": active["host_ssh_server"],
        "container_ssh_server": active["container_ssh_server"],
        "management_ssh_policy": active["management_ssh_policy"],
        "gpu_policy": active["gpu_policy"],
        "quota": active["quota"],
        "slurm": active["slurm"],
        "container": active["container"],
        "guard": active["guard"],
        "gpu_health": {
            "count": 4,
            "mig": "DISABLED",
            "dcgm": "4/4 PASS",
            "kernel_errors": "CLEAR",
        },
        "systemd_failed_units": 0,
    }


def build_origin_pilot_acceptance_argv(request_id: str) -> list[str]:
    try:
        canonical = str(uuid.UUID(request_id))
    except ValueError as exc:
        raise LifecycleValidationError(
            "PORTAL3F_REQUEST_ID_REJECTED", "Portal-3F Worker request ID is invalid"
        ) from exc
    if canonical != request_id:
        raise LifecycleValidationError(
            "PORTAL3F_REQUEST_ID_REJECTED", "Portal-3F Worker request ID is not canonical"
        )
    return [SCRIPT_ALLOWLIST["h100-origin-pilot-acceptance"], "--execute", canonical]


def _portal3f_script_result(stdout: str, request_id: str) -> dict[str, Any]:
    accepted_fields = {
        "PORTAL3F_RESULT",
        "CPU_JOB_ID",
        "GPU_JOB_ID",
        "ALLOCATED_GPU_UUID",
        "OUT_OF_JOB_GPU_OPEN",
        "OUT_OF_JOB_CUDA_CONTEXT",
        "IN_JOB_ALLOCATED_GPU",
        "IN_JOB_UNALLOCATED_GPUS",
        "IN_JOB_CUDA_CONTEXT",
        "FINAL_NODE_STATE",
        "WORKER_LOG_DIR",
    }
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in accepted_fields:
            continue
        if key in values:
            raise LifecycleValidationError(
                "PORTAL3F_RESULT_REJECTED", "Portal-3F script returned duplicate fields"
            )
        values[key] = value
    expected_log_dir = f"/srv/gpu-platform/platform/logs/portal3f-worker-{request_id}"
    if not (
        values.get("PORTAL3F_RESULT") == "PASSED"
        and re.fullmatch(r"[1-9][0-9]*", values.get("CPU_JOB_ID", ""))
        and re.fullmatch(r"[1-9][0-9]*", values.get("GPU_JOB_ID", ""))
        and values.get("CPU_JOB_ID") != values.get("GPU_JOB_ID")
        and re.fullmatch(r"GPU-[0-9a-f-]{36}", values.get("ALLOCATED_GPU_UUID", ""))
        and values.get("OUT_OF_JOB_GPU_OPEN") == "DENIED"
        and values.get("OUT_OF_JOB_CUDA_CONTEXT") == "DENIED"
        and values.get("IN_JOB_ALLOCATED_GPU") == "ALLOWED"
        and values.get("IN_JOB_UNALLOCATED_GPUS") == "DENIED"
        and values.get("IN_JOB_CUDA_CONTEXT") == "PASSED"
        and values.get("FINAL_NODE_STATE") == "DRAIN"
        and values.get("WORKER_LOG_DIR") == expected_log_dir
    ):
        raise LifecycleValidationError(
            "PORTAL3F_RESULT_REJECTED", "Portal-3F script result is incomplete"
        )
    return {
        "cpu_job_id": int(values["CPU_JOB_ID"]),
        "gpu_job_id": int(values["GPU_JOB_ID"]),
        "allocated_gpu_uuid": values["ALLOCATED_GPU_UUID"],
        "out_of_job_gpu_open": "DENIED",
        "out_of_job_cuda_context": "DENIED",
        "in_job_allocated_gpu": "ALLOWED",
        "in_job_unallocated_gpus": "DENIED",
        "in_job_cuda_context": "PASSED",
        "final_node_state": "DRAIN",
        "worker_log_dir": expected_log_dir,
    }


def _portal3f_drain_recovery_status() -> str:
    node = slurm_node()
    jobs = slurm_jobs()
    if (
        node.get("status") == "OK"
        and node.get("nodes")
        and all("DRAIN" in str(item.get("state", "")).upper() for item in node["nodes"])
        and jobs.get("status") == "OK"
        and not jobs.get("jobs")
    ):
        return "DRAIN_RESTORED"
    return "REQUIRES_MANUAL_REVIEW"


def _portal3f_client_validation_plan(payload: dict[str, Any]) -> dict[str, Any]:
    preflight = _portal3f_active_preflight()
    return {
        "status": "DRY_RUN",
        "handler": "user.ssh_client_validation.record",
        "execution_enabled": False,
        "validated_username": payload["username"],
        "confirmation_source": payload["confirmation_source"],
        "host_client_validation": "PASS",
        "container_client_validation": "PASS",
        "server_preflight": preflight,
        "private_key_handling": "NOT_ACCESSED",
        "slurm_execution": "NOT_PERFORMED",
    }


def _execute_portal3f_client_validation(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    if (
        request.requested_by != MANAGEMENT_USERNAME
        or request.approved_by != MANAGEMENT_USERNAME
        or request.idempotency_key != PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY
        or payload != APPROVED_CLIENT_VALIDATION_PAYLOAD
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "PORTAL3F_CLIENT_VALIDATION_BINDING_REJECTED",
                "message": "client validation is not bound to the fixed Portal-3F approval",
            },
        }
    try:
        preflight = _portal3f_active_preflight()
    except LifecycleValidationError as exc:
        return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}
    return {
        "status": "SUCCEEDED",
        "handler": "user.ssh_client_validation.record",
        "execution_enabled": True,
        "username": PILOT_USERNAME,
        "host_client_validation": "PASS",
        "container_client_validation": "PASS",
        "confirmation_source": payload["confirmation_source"],
        "private_key_handling": "NOT_ACCESSED",
        "server_preflight": preflight,
    }


def _portal3f_pilot_acceptance_plan(payload: dict[str, Any]) -> dict[str, Any]:
    integrity = script_integrity()
    failed_scripts = sorted(
        name
        for name in PORTAL3F_REQUIRED_SCRIPTS
        if not integrity.get(name, {}).get("integrity_ok", False)
    )
    if failed_scripts:
        raise LifecycleValidationError(
            "SCRIPT_INTEGRITY_FAILED",
            f"Portal-3F required script integrity failed: {','.join(failed_scripts)}",
        )
    preflight = _portal3f_active_preflight()
    return {
        "status": "DRY_RUN",
        "handler": "user.pilot.acceptance",
        "execution_enabled": False,
        "validated_username": payload["username"],
        "node_name": payload["node_name"],
        "partition": payload["partition"],
        "account": payload["account"],
        "qos": payload["qos"],
        "max_gpus": payload["max_gpus"],
        "image_ref": payload["image_ref"],
        "tests": [
            "CPU_JOB",
            "SINGLE_GPU_PYXIS_ENROOT",
            "IN_JOB_ALLOCATED_GPU_ALLOW",
            "IN_JOB_UNALLOCATED_GPU_DENY",
            "IN_JOB_CUDA_CONTEXT",
            "OUT_OF_JOB_GPU_OPEN_DENY_CONCURRENT",
            "OUT_OF_JOB_CUDA_CONTEXT_DENY_CONCURRENT",
        ],
        "final_node_state": "DRAIN",
        "preflight": preflight,
    }


def _execute_portal3f_pilot_acceptance(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    if (
        request.requested_by != MANAGEMENT_USERNAME
        or request.approved_by != MANAGEMENT_USERNAME
        or request.idempotency_key != PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY
        or payload != APPROVED_PILOT_ACCEPTANCE_PAYLOAD
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "PORTAL3F_ACCEPTANCE_BINDING_REJECTED",
                "message": "Pilot acceptance is not bound to the fixed Portal-3F approval",
            },
        }
    try:
        plan = _portal3f_pilot_acceptance_plan(payload)
        execution = run_allowlisted_script(
            build_origin_pilot_acceptance_argv(request.request_id), timeout=1500
        )
        if not execution.get("ok"):
            return {
                "status": "ERROR",
                "error": {
                    "code": str(execution.get("error_code", "PORTAL3F_EXECUTION_FAILED"))[:64],
                    "message": "fixed Portal-3F acceptance tooling failed",
                    "exit_code": execution.get("exit_code"),
                },
                "rollback_status": _portal3f_drain_recovery_status(),
            }
        acceptance = _portal3f_script_result(str(execution.get("stdout", "")), request.request_id)
        postflight = _portal3f_active_preflight()
        return {
            "status": "SUCCEEDED",
            "handler": "user.pilot.acceptance",
            "execution_enabled": True,
            "username": PILOT_USERNAME,
            "approval_reference": payload["approval_reference"],
            "client_validation": {"host": "PASS", "container": "PASS"},
            "acceptance": acceptance,
            "preflight": plan["preflight"],
            "postflight": postflight,
            "rollback_status": "NOT_REQUIRED",
        }
    except LifecycleValidationError as exc:
        return {
            "status": "ERROR",
            "error": {"code": exc.code, "message": str(exc)},
            "rollback_status": _portal3f_drain_recovery_status(),
        }


def _portal3g_node_snapshot(expected_state: str) -> dict[str, Any] | None:
    node = slurm_node()
    jobs = slurm_jobs()
    rows = node.get("nodes")
    if (
        node.get("status") != "OK"
        or not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
        or rows[0].get("name") != "sagsh100server"
        or jobs.get("status") != "OK"
        or jobs.get("jobs")
    ):
        return None
    state = str(rows[0].get("state", "")).upper()
    reason = str(rows[0].get("reason") or "").strip()
    if expected_state == "IDLE":
        if state != "IDLE" or reason.casefold() not in {"", "none", "(null)", "n/a"}:
            return None
    elif expected_state == "DRAIN":
        if "DRAIN" not in state:
            return None
    else:
        return None
    return {
        "name": "sagsh100server",
        "state": expected_state,
        "queue": "EMPTY",
        "reason": reason or "NONE",
        "jobs_submitted": 0,
    }


def _portal3g_wait_for_node(
    expected_state: str, timeout_seconds: int = 20
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = _portal3g_node_snapshot(expected_state)
        if snapshot is not None:
            return snapshot
        if time.monotonic() >= deadline:
            return None
        time.sleep(1)


def _portal3g_safety_drain() -> str:
    drained = run_fixed(
        "scontrol",
        [
            "update",
            "NodeName=sagsh100server",
            "State=DRAIN",
            "Reason=production pilot safety gate failed",
        ],
        timeout=20,
    )
    if not drained.get("ok"):
        return "REQUIRES_MANUAL_REVIEW"
    return (
        "DRAIN_RESTORED"
        if _portal3g_wait_for_node("DRAIN", timeout_seconds=20) is not None
        else "REQUIRES_MANUAL_REVIEW"
    )


def _portal3g_production_pilot_plan(payload: dict[str, Any]) -> dict[str, Any]:
    preflight = _portal3f_active_preflight("DRAIN")
    return {
        "status": "DRY_RUN",
        "handler": "slurm.production_pilot.start",
        "execution_enabled": False,
        "validated_username": payload["username"],
        "node_name": payload["node_name"],
        "action": "RESUME",
        "production_pilot_scope": {
            "mode": "SINGLE_NODE",
            "managed_users": ["origin-pilot"],
            "max_gpus": 1,
        },
        "client_validation_operation_id": payload["client_validation_operation_id"],
        "pilot_acceptance_operation_id": payload["pilot_acceptance_operation_id"],
        "expected_node_state": "DRAIN",
        "target_node_state": "IDLE",
        "jobs_submitted": 0,
        "preflight": preflight,
        "failure_action": "DRAIN",
    }


def _execute_portal3g_production_pilot(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    if (
        request.requested_by != MANAGEMENT_USERNAME
        or request.approved_by != MANAGEMENT_USERNAME
        or request.idempotency_key != PORTAL3G_PRODUCTION_PILOT_IDEMPOTENCY_KEY
        or payload != APPROVED_PRODUCTION_PILOT_PAYLOAD
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "PORTAL3G_PRODUCTION_PILOT_BINDING_REJECTED",
                "message": "Production Pilot start is not bound to the fixed approval",
            },
            "rollback_status": "NOT_REQUIRED_NODE_REMAINS_DRAINED",
        }
    try:
        plan = _portal3g_production_pilot_plan(payload)
    except LifecycleValidationError as exc:
        return {
            "status": "ERROR",
            "error": {"code": exc.code, "message": str(exc)},
            "rollback_status": "NOT_REQUIRED_NODE_REMAINS_DRAINED",
        }

    resumed = run_fixed(
        "scontrol",
        ["update", "NodeName=sagsh100server", "State=RESUME"],
        timeout=20,
    )
    if not resumed.get("ok"):
        return {
            "status": "ERROR",
            "error": {
                "code": "PORTAL3G_RESUME_FAILED",
                "message": "fixed Slurm RESUME handler failed",
            },
            "rollback_status": _portal3g_safety_drain(),
        }
    node = _portal3g_wait_for_node("IDLE", timeout_seconds=20)
    if node is None:
        return {
            "status": "ERROR",
            "error": {
                "code": "PORTAL3G_IDLE_NOT_REACHED",
                "message": "Slurm did not reach exact IDLE with an empty queue",
            },
            "rollback_status": _portal3g_safety_drain(),
        }
    try:
        postflight = _portal3f_active_preflight("IDLE")
    except LifecycleValidationError as exc:
        return {
            "status": "ERROR",
            "error": {"code": exc.code, "message": str(exc)},
            "rollback_status": _portal3g_safety_drain(),
        }
    return {
        "status": "SUCCEEDED",
        "handler": "slurm.production_pilot.start",
        "execution_enabled": True,
        "username": PILOT_USERNAME,
        "node_name": "sagsh100server",
        "action": "RESUME",
        "previous_node_state": "DRAIN",
        "node": node,
        "scheduler": "AVAILABLE",
        "production_pilot_state": "ACTIVE",
        "production_pilot_scope": plan["production_pilot_scope"],
        "client_validation_operation_id": payload["client_validation_operation_id"],
        "pilot_acceptance_operation_id": payload["pilot_acceptance_operation_id"],
        "approval_reference": payload["approval_reference"],
        "jobs_submitted": 0,
        "preflight": plan["preflight"],
        "postflight": postflight,
        "rollback_status": "NOT_REQUIRED",
    }


def _execute_portal3g_safety_drain(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    if (
        request.requested_by != MANAGEMENT_USERNAME
        or request.approved_by != MANAGEMENT_USERNAME
        or request.idempotency_key != PORTAL3G_DRAIN_IDEMPOTENCY_KEY
        or payload != {"node_name": "sagsh100server"}
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "PORTAL3G_SAFETY_DRAIN_BINDING_REJECTED",
                "message": "safety DRAIN is not bound to the fixed Portal-3G rollback",
            },
        }
    rollback_status = _portal3g_safety_drain()
    return {
        "status": "SUCCEEDED" if rollback_status == "DRAIN_RESTORED" else "ERROR",
        "handler": "slurm.drain",
        "execution_enabled": True,
        "node_name": "sagsh100server",
        "reason": "production pilot safety gate failed",
        "rollback_status": rollback_status,
        "jobs_submitted": 0,
    }


def _execute_origin_pilot_activate_rollback(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    if (
        request.requested_by != MANAGEMENT_USERNAME
        or request.approved_by != MANAGEMENT_USERNAME
        or request.idempotency_key != PORTAL3E_FINAL_ROLLBACK_IDEMPOTENCY_KEY
        or not _portal3e_final_payload_matches(payload)
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "ACTIVATE_ROLLBACK_BINDING_REJECTED",
                "message": "Activate rollback is not bound to Portal-3E-FINAL",
            },
        }
    integrity = script_integrity()
    if not integrity.get("h100-user-create", {}).get("integrity_ok", False):
        return {
            "status": "ERROR",
            "error": {
                "code": "SCRIPT_INTEGRITY_FAILED",
                "message": "fixed lifecycle script integrity check failed",
            },
        }
    try:
        summary = _stage_postcondition_summary(APPROVED_STAGE_PAYLOAD)
        return {
            "status": "SUCCEEDED",
            "handler": "user.activate.rollback",
            "rollback_status": "ROLLED_BACK",
            "idempotent_replay": True,
            "stage": summary,
        }
    except LifecycleValidationError:
        pass
    execution = run_allowlisted_script(
        build_user_activate_rollback_argv(PILOT_USERNAME), timeout=180
    )
    if not execution.get("ok"):
        return {
            "status": "ERROR",
            "error": {
                "code": str(execution.get("error_code", "ACTIVATE_ROLLBACK_FAILED"))[:64],
                "message": "controlled Activate rollback script failed",
            },
            "rollback_status": "REQUIRES_MANUAL_REVIEW",
        }
    try:
        summary = _stage_postcondition_summary(APPROVED_STAGE_PAYLOAD)
    except LifecycleValidationError as exc:
        return {
            "status": "ERROR",
            "error": {"code": exc.code, "message": str(exc)},
            "rollback_status": "REQUIRES_MANUAL_REVIEW",
        }
    return {
        "status": "SUCCEEDED",
        "handler": "user.activate.rollback",
        "rollback_status": "ROLLED_BACK",
        "idempotent_replay": False,
        "stage": summary,
    }


def _execute_origin_pilot_activate(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    if (
        request.requested_by != MANAGEMENT_USERNAME
        or request.approved_by != MANAGEMENT_USERNAME
        or request.idempotency_key != PORTAL3E_FINAL_IDEMPOTENCY_KEY
        or not _portal3e_final_payload_matches(payload)
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "ACTIVATE_APPROVAL_BINDING_REJECTED",
                "message": "real Activate is not bound to Portal-3E-FINAL approval",
            },
        }
    integrity = script_integrity()
    failed_scripts = sorted(
        name
        for name in ACTIVATE_REQUIRED_SCRIPTS
        if not integrity.get(name, {}).get("integrity_ok", False)
    )
    if failed_scripts:
        return {
            "status": "ERROR",
            "error": {
                "code": "SCRIPT_INTEGRITY_FAILED",
                "message": "fixed lifecycle script integrity check failed",
                "scripts": failed_scripts,
            },
        }
    try:
        preflight = _user_activate_dry_run(payload)
        key_records = list(preflight["approved_ssh_keys"])
        if (
            len(key_records) != 1
            or key_records[0].get("record_id") != PORTAL3E_FINAL_KEY_RECORD_ID
            or key_records[0].get("fingerprint_sha256") != PORTAL3E_FINAL_KEY_FINGERPRINT
            or key_records[0].get("key_type") != "ssh-ed25519"
            or key_records[0].get("scope") != "BOTH"
        ):
            raise LifecycleValidationError(
                "ACTIVATE_APPROVED_KEY_CHANGED", "approved Portal-3E-FINAL key record changed"
            )
        management_before = {
            MANAGEMENT_USERNAME: _sshd_effective_config(MANAGEMENT_USERNAME),
            "codexops": _sshd_effective_config("codexops"),
        }
        with activation_key_bundles(request.request_id, key_records) as (
            host_key_file,
            container_key_file,
        ):
            execution = run_allowlisted_script(
                build_user_activate_argv(PILOT_USERNAME, host_key_file, container_key_file),
                timeout=ACTIVATE_EXECUTION_TIMEOUT_SECONDS,
            )
        if not execution.get("ok"):
            try:
                _stage_postcondition_summary(APPROVED_STAGE_PAYLOAD)
                rollback_status = "ROLLED_BACK"
            except LifecycleValidationError:
                rollback_status = "REQUIRES_MANUAL_REVIEW"
            return {
                "status": "ERROR",
                "error": {
                    "code": str(execution.get("error_code", "ACTIVATE_EXECUTION_FAILED"))[:64],
                    "message": "transactional Activate script failed",
                    "exit_code": execution.get("exit_code"),
                },
                "rollback_status": rollback_status,
            }
        try:
            activated = _activate_postcondition_summary(key_records, management_before)
        except LifecycleValidationError as exc:
            rollback_execution = run_allowlisted_script(
                build_user_activate_rollback_argv(PILOT_USERNAME), timeout=180
            )
            rollback_status = "REQUIRES_MANUAL_REVIEW"
            if rollback_execution.get("ok"):
                try:
                    _stage_postcondition_summary(APPROVED_STAGE_PAYLOAD)
                    rollback_status = "ROLLED_BACK"
                except LifecycleValidationError:
                    pass
            return {
                "status": "ERROR",
                "error": {"code": exc.code, "message": str(exc)},
                "rollback_status": rollback_status,
            }
        return {
            "status": "SUCCEEDED",
            "handler": "user.activate",
            "idempotent_replay": False,
            "execution_enabled": True,
            "approved_ssh_keys": key_records,
            "activate": activated,
        }
    except LifecycleValidationError as exc:
        return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}


def _user_activate_dry_run(payload: dict[str, Any]) -> dict[str, Any]:
    # Key files are validated before consulting or mutating lifecycle state.
    key_records = validate_approved_ssh_key_records(payload["approved_ssh_key_record_ids"])
    if any(record["managed_user_id"] != payload["managed_user_id"] for record in key_records):
        raise LifecycleValidationError(
            "PUBLIC_KEY_RECORD_OWNER_MISMATCH",
            "approved SSH key record belongs to another managed identity",
        )
    stage_summary = _stage_postcondition_summary(APPROVED_STAGE_PAYLOAD)
    host_ssh_policy = managed_host_ssh_policy(stage_summary["username"])
    host_records, container_records = _activation_target_records(key_records)
    host_plan = [
        {
            "record_id": record["record_id"],
            "fingerprint_sha256": record["fingerprint_sha256"],
        }
        for record in host_records
    ]
    container_plan = [
        {
            "record_id": record["record_id"],
            "fingerprint_sha256": record["fingerprint_sha256"],
        }
        for record in container_records
    ]
    return {
        "status": "DRY_RUN",
        "handler": "user.activate",
        "activate_status": "READY",
        "execution_enabled": False,
        "expected_state": "STAGED",
        "managed_user_id": payload["managed_user_id"],
        "approved_ssh_keys": key_records,
        "validated_username": stage_summary["username"],
        "validated_uid": stage_summary["uid"],
        "public_key_validation": "PASSED",
        "authorized_keys_install": "DEFERRED_UNTIL_REAL_ACTIVATE",
        "activate_cli_contract": "TARGET_SCOPED_ROOT_CONTROLLED_BUNDLES",
        "host_authorized_keys_install": "PLANNED",
        "container_authorized_keys_install": "PLANNED",
        "host_authorized_keys_plan": host_plan,
        "container_authorized_keys_plan": container_plan,
        "host_authorized_keys_current": stage_summary["host_authorized_keys"],
        "container_authorized_keys_current": stage_summary["container_authorized_keys"],
        "shell_current": stage_summary["shell"],
        "password_current": stage_summary["password"],
        "gpu_isolation": "PASS",
        "host_ssh_policy": host_ssh_policy,
        "guard": stage_summary["guard"],
        "quota": stage_summary["quota"],
        "slurm": stage_summary["slurm"],
        "container": stage_summary["container"],
        "expected_rollback": [
            "恢复 /usr/sbin/nologin",
            "禁用或回滚 authorized_keys",
            "停止用户容器",
            "保留 GPU policy、quota 与用户数据",
            "保持 Slurm DRAIN",
        ],
    }


def _managed_account(payload: dict[str, Any]) -> pwd.struct_passwd:
    username = str(payload["username"])
    try:
        account = pwd.getpwnam(username)
    except KeyError as exc:
        raise LifecycleValidationError(
            "MANAGED_IDENTITY_NOT_FOUND", "managed Unix identity does not exist"
        ) from exc
    if (
        account.pw_uid != int(payload["uid"])
        or account.pw_gid != int(payload["gid"])
        or account.pw_shell not in {"/bin/bash", "/usr/sbin/nologin"}
    ):
        raise LifecycleValidationError(
            "MANAGED_IDENTITY_CHANGED", "managed Unix identity differs from its approved binding"
        )
    password = run_fixed("passwd", ["-S", username], timeout=10)
    fields = str(password.get("stdout", "")).split()
    if not password.get("ok") or len(fields) < 2 or fields[1] != "L":
        raise LifecycleValidationError(
            "LINUX_PASSWORD_NOT_LOCKED", "managed Unix password must remain locked"
        )
    forbidden = FORBIDDEN_PILOT_GROUPS.intersection(_group_names(username, account.pw_gid))
    if forbidden:
        raise LifecycleValidationError(
            "PRIVILEGED_GROUP_REJECTED", "managed Unix identity has a privileged group"
        )
    return account


def _managed_slurm_security_preflight(payload: dict[str, Any]) -> None:
    account = _managed_account(payload)
    if account.pw_shell != "/usr/sbin/nologin":
        raise LifecycleValidationError(
            "HOST_ACCESS_POLICY_REJECTED",
            "self-service jobs require managed-user host login to remain disabled",
        )
    username = str(payload["username"])
    slurm_account = str(payload["slurm_account"])
    slurm_qos = str(payload["slurm_qos"])
    integrity = script_integrity()
    isolation_tool = "h100-user-gpu-isolation"
    if not integrity.get(isolation_tool, {}).get("integrity_ok", False):
        raise LifecycleValidationError(
            "SCRIPT_INTEGRITY_FAILED", "GPU isolation verifier integrity failed"
        )
    isolation = run_allowlisted_script(
        [SCRIPT_ALLOWLIST[isolation_tool], "verify", username], timeout=60
    )
    if not isolation.get("ok"):
        raise LifecycleValidationError(
            "GPU_ISOLATION_FAILED", "per-user GPU isolation verification failed"
        )
    association = run_fixed(
        "sacctmgr",
        [
            "-n",
            "-P",
            "show",
            "assoc",
            "where",
            f"User={username}",
            f"Account={slurm_account}",
            "format=User,Account,QOS,DefaultQOS",
        ],
        timeout=20,
    )
    if not association.get("ok") or not any(
        len(values) >= 4
        and values[:2] == [username, slurm_account]
        and (slurm_qos in values[2].split(",") or values[3] == slurm_qos)
        for values in (line.split("|") for line in str(association.get("stdout", "")).splitlines())
    ):
        raise LifecycleValidationError(
            "SLURM_ASSOCIATION_REJECTED", "Slurm association differs from the owned context"
        )
    qos = run_fixed(
        "sacctmgr",
        ["-n", "-P", "show", "qos", slurm_qos, "format=Name,MaxTRESPerUser"],
        timeout=20,
    )
    if not qos.get("ok") or not any(
        line.startswith(f"{slurm_qos}|") and "gres/gpu=1" in line
        for line in str(qos.get("stdout", "")).splitlines()
    ):
        raise LifecycleValidationError("GPU_LIMIT_REJECTED", "Slurm GPU limit is not one")


def _managed_relative_parts(relative: str) -> tuple[str, ...]:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise LifecycleValidationError(
            "SYMLINK_ESCAPE_REJECTED", "requested user path escapes the owned workspace"
        )
    return path.parts


def _workspace_binding_for_payload(
    payload: dict[str, Any], *, require_alias: bool
) -> WorkspaceBinding:
    try:
        binding = workspace_binding(
            str(payload["username"]),
            int(payload["uid"]),
            int(payload["gid"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleValidationError(
            "WORKSPACE_BINDING_REJECTED", "managed workspace coordinates are invalid"
        ) from exc
    if payload.get("workspace_path") != str(binding.canonical_workspace) or payload.get(
        "quota_root"
    ) != str(binding.quota_root):
        raise LifecycleValidationError(
            "WORKSPACE_BINDING_REJECTED", "workspace is not derived from the managed owner"
        )
    try:
        quota_metadata = Path(binding.quota_root).lstat()
        backing_metadata = Path(binding.backing_workspace).lstat()
    except OSError as exc:
        raise LifecycleValidationError(
            "WORKSPACE_BINDING_REJECTED", "authoritative workspace backing is unavailable"
        ) from exc
    for metadata in (quota_metadata, backing_metadata):
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != binding.uid
            or metadata.st_gid != binding.gid
            or stat.S_IMODE(metadata.st_mode) & 0o007
        ):
            raise LifecycleValidationError(
                "WORKSPACE_BINDING_REJECTED",
                "authoritative workspace ownership or mode is invalid",
            )
    if require_alias:
        try:
            canonical_metadata = Path(binding.canonical_workspace).lstat()
        except OSError as exc:
            raise LifecycleValidationError(
                "WORKSPACE_BINDING_REJECTED", "canonical workspace alias is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(canonical_metadata.st_mode)
            or stat.S_ISLNK(canonical_metadata.st_mode)
            or canonical_metadata.st_uid != binding.uid
            or canonical_metadata.st_gid != binding.gid
            or stat.S_IMODE(canonical_metadata.st_mode) & 0o007
            or (canonical_metadata.st_dev, canonical_metadata.st_ino)
            != (backing_metadata.st_dev, backing_metadata.st_ino)
        ):
            raise LifecycleValidationError(
                "WORKSPACE_BINDING_REJECTED",
                "canonical workspace is not the owner-bound backing alias",
            )
    return binding


def _validate_owned_descriptor(
    descriptor: int, *, directory: bool, uid: int, gid: int
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    expected_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected_type or metadata.st_uid != uid or metadata.st_gid != gid:
        raise LifecycleValidationError(
            "USER_PATH_OWNERSHIP_REJECTED", "requested user path metadata is invalid"
        )
    return metadata


@contextmanager
def _open_managed_user_path(
    root: Path, relative: str, *, directory: bool, uid: int, gid: int
) -> Iterator[tuple[Path, int, os.stat_result]]:
    parts = _managed_relative_parts(relative)
    descriptors: list[int] = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | nofollow)
        descriptors.append(descriptor)
        _validate_owned_descriptor(descriptor, directory=True, uid=uid, gid=gid)
        metadata = os.fstat(descriptor)
        for index, part in enumerate(parts):
            is_directory = index < len(parts) - 1 or directory
            flags = os.O_RDONLY | os.O_CLOEXEC | nofollow
            if is_directory:
                flags |= os.O_DIRECTORY
            descriptor = os.open(part, flags, dir_fd=descriptors[-1])
            descriptors.append(descriptor)
            metadata = _validate_owned_descriptor(
                descriptor, directory=is_directory, uid=uid, gid=gid
            )
        yield root.joinpath(*parts), descriptors[-1], metadata
    except LifecycleValidationError:
        raise
    except OSError as exc:
        code = (
            "SYMLINK_ESCAPE_REJECTED"
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}
            else "USER_PATH_NOT_FOUND"
        )
        raise LifecycleValidationError(code, "requested user path cannot be opened safely") from exc
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)


@contextmanager
def _open_workspace_directory(
    root: Path, relative: str, *, uid: int, gid: int
) -> Iterator[tuple[Path, int, os.stat_result]]:
    parts = _managed_relative_parts(relative)
    descriptors: list[int] = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | nofollow)
        descriptors.append(descriptor)
        _validate_owned_descriptor(descriptor, directory=True, uid=uid, gid=gid)
        metadata = os.fstat(descriptor)
        for part in parts:
            descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | nofollow,
                dir_fd=descriptors[-1],
            )
            descriptors.append(descriptor)
            metadata = _validate_owned_descriptor(descriptor, directory=True, uid=uid, gid=gid)
        yield root.joinpath(*parts), descriptors[-1], metadata
    except LifecycleValidationError:
        raise
    except OSError as exc:
        code = (
            "SYMLINK_ESCAPE_REJECTED"
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}
            else "WORKSPACE_DIRECTORY_REJECTED"
        )
        raise LifecycleValidationError(code, "workspace path cannot be opened safely") from exc
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)


def _managed_user_path(root: Path, relative: str, *, directory: bool, uid: int, gid: int) -> Path:
    with _open_managed_user_path(root, relative, directory=directory, uid=uid, gid=gid) as (
        path,
        _descriptor,
        _metadata,
    ):
        return path


def _read_regular_descriptor(descriptor: int, metadata: os.stat_result) -> bytes:
    if metadata.st_size <= 0 or metadata.st_size > 1024 * 1024:
        raise LifecycleValidationError("JOB_SCRIPT_REJECTED", "job script size is invalid")
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = bytearray()
    while len(content) <= 1024 * 1024:
        chunk = os.read(descriptor, min(64 * 1024, 1024 * 1024 + 1 - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    if len(content) != metadata.st_size or b"\x00" in content:
        raise LifecycleValidationError("JOB_SCRIPT_REJECTED", "job script content is invalid")
    return bytes(content)


def _ensure_managed_owned_directory(root: Path, relative: str, uid: int, gid: int) -> Path:
    parts = _managed_relative_parts(relative)
    descriptors: list[int] = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | nofollow)
        descriptors.append(descriptor)
        _validate_owned_descriptor(descriptor, directory=True, uid=uid, gid=gid)
        for index, part in enumerate(parts):
            created = False
            try:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | nofollow,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | nofollow,
                    dir_fd=descriptor,
                )
                os.fchown(next_descriptor, uid, gid)
                created = True
            descriptors.append(next_descriptor)
            descriptor = next_descriptor
            _validate_owned_descriptor(descriptor, directory=True, uid=uid, gid=gid)
            if created or index > 0:
                os.fchmod(descriptor, 0o700)
        return root.joinpath(*parts)
    except LifecycleValidationError:
        raise
    except OSError as exc:
        raise LifecycleValidationError(
            "JOB_STAGING_REJECTED", "job staging directory cannot be opened safely"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)


def _stage_user_job_script(payload: dict[str, Any], content: bytes) -> tuple[Path, int]:
    uid = int(payload["uid"])
    gid = int(payload["gid"])
    root = Path(str(payload["workspace_path"]))
    script_relative = ".portal/job-scripts"
    _ensure_managed_owned_directory(root, script_relative, uid, gid)
    _ensure_managed_owned_directory(root, ".portal/jobs", uid, gid)
    _ensure_managed_owned_directory(root, "outputs", uid, gid)
    filename = f"{payload['portal_job_id']}.sh"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    with _open_managed_user_path(root, script_relative, directory=True, uid=uid, gid=gid) as (
        script_dir,
        directory_descriptor,
        _metadata,
    ):
        created = False
        try:
            descriptor = os.open(
                filename,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | nofollow,
                0o700,
                dir_fd=directory_descriptor,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_CLOEXEC | nofollow,
                dir_fd=directory_descriptor,
            )
        try:
            if created:
                os.fchown(descriptor, uid, gid)
                os.fchmod(descriptor, 0o700)
                written = 0
                while written < len(content):
                    count = os.write(descriptor, content[written:])
                    if count <= 0:
                        raise LifecycleValidationError(
                            "JOB_STAGING_REJECTED", "staged job script write made no progress"
                        )
                    written += count
                os.fsync(descriptor)
            metadata = _validate_owned_descriptor(descriptor, directory=False, uid=uid, gid=gid)
            if created:
                if metadata.st_size != len(content):
                    raise LifecycleValidationError(
                        "JOB_STAGING_REJECTED", "staged job script is incomplete"
                    )
            elif _read_regular_descriptor(descriptor, metadata) != content:
                raise LifecycleValidationError(
                    "JOB_STAGING_CONFLICT", "staged job script differs from idempotent request"
                )
            return script_dir / filename, descriptor
        except Exception:
            os.close(descriptor)
            raise


def _slurm_time(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    prefix = f"{days}-" if days else ""
    return f"{prefix}{hours:02d}:{minutes:02d}:{seconds:02d}"


def _slurm_job_owner(job_id: int) -> str | None:
    live = run_fixed("scontrol", ["show", "job", str(job_id), "-o"], timeout=15)
    if live.get("ok"):
        match = re.search(r"(?:^|\s)UserId=([^\s(]+)", str(live.get("stdout", "")))
        if match:
            return match.group(1)
    history = run_fixed(
        "sacct",
        ["-X", "-n", "-P", "-j", str(job_id), "-o", "User"],
        timeout=20,
    )
    if history.get("ok"):
        users = [line.strip("|") for line in str(history.get("stdout", "")).splitlines() if line]
        if users:
            return users[0]
    return None


def _run_as_managed_user(
    payload: dict[str, Any],
    command: list[str],
    timeout: float,
    *,
    pass_fds: tuple[int, ...] = (),
) -> dict[str, Any]:
    setpriv = BINARIES["setpriv"]
    if not os.path.exists(setpriv) or any("\x00" in item for item in command):
        return {"ok": False, "error_code": "SETUID_EXECUTION_REJECTED", "stdout": "", "stderr": ""}
    username = str(payload["username"])
    workspace = str(payload["workspace_path"])
    enroot_root = f"{workspace}/.portal/enroot"
    managed_env = {
        **FIXED_ENV,
        "HOME": f"/home/{username}",
        "USER": username,
        "LOGNAME": username,
        "SHELL": "/usr/sbin/nologin",
        "WORKSPACE": workspace,
        "ENROOT_CACHE_PATH": f"{enroot_root}/cache",
        "ENROOT_CONFIG_PATH": f"{enroot_root}/config",
        "ENROOT_DATA_PATH": f"{enroot_root}/data",
        "ENROOT_RUNTIME_PATH": f"{enroot_root}/runtime",
    }
    try:
        completed = subprocess.run(
            [
                setpriv,
                f"--reuid={payload['uid']}",
                f"--regid={payload['gid']}",
                "--clear-groups",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "--bounding-set=-all",
                "--",
                *command,
            ],
            cwd="/",
            env=managed_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
            text=True,
            pass_fds=pass_fds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "error_code": "SETUID_EXECUTION_FAILED",
            "stdout": "",
            "stderr": str(exc)[:512],
        }
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": _truncate(completed.stdout),
        "stderr": _truncate(completed.stderr),
    }


def _managed_sbatch_argv(
    payload: dict[str, Any],
    *,
    workdir: Path,
    stdout: Path,
    stderr: Path,
    staged_descriptor: int,
) -> list[str]:
    deadline = (
        datetime.fromisoformat(str(payload["lease_deadline_at"]).replace("Z", "+00:00"))
        .astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "")
    )
    argv = [
        BINARIES["sbatch"],
        "--parsable",
        f"--account={payload['slurm_account']}",
        f"--qos={payload['slurm_qos']}",
        f"--job-name={payload['name']}",
        f"--cpus-per-task={payload['cpus']}",
        f"--mem={payload['memory_mb']}M",
        f"--time={_slurm_time(int(payload['time_limit_seconds']))}",
        f"--deadline={deadline}",
        "--export=ALL",
        f"--chdir={workdir}",
        f"--output={stdout}",
        f"--error={stderr}",
    ]
    if int(payload["gpu_count"]) == 1:
        argv.append("--gres=gpu:h100:1")
    if payload.get("image_ref"):
        owned_root = Path(str(payload["workspace_path"]))
        argv.extend(
            [
                f"--container-image={payload['image_ref']}",
                "--no-container-mount-home",
                f"--container-mounts={owned_root}:{owned_root},{owned_root}:/workspace",
            ]
        )
    argv.append(f"/proc/self/fd/{staged_descriptor}")
    return argv


def _execute_self_job_submit(request: WorkerRequest, payload: dict[str, Any]) -> dict[str, Any]:
    staged_descriptor: int | None = None
    try:
        _managed_slurm_security_preflight(payload)
        uid = int(payload["uid"])
        gid = int(payload["gid"])
        root = Path(str(payload["workspace_path"]))
        if root != Path(workspace_path(uid)):
            raise LifecycleValidationError(
                "WORKSPACE_BINDING_REJECTED", "workspace is not derived from the managed UID"
            )
        content = str(payload["script_content"]).encode("utf-8")
        workdir = _managed_user_path(
            root,
            str(payload["workdir_relative_path"]),
            directory=True,
            uid=uid,
            gid=gid,
        )
        _staged, staged_descriptor = _stage_user_job_script(payload, content)
        stdout = root / str(payload["stdout_relative_path"])
        stderr = root / str(payload["stderr_relative_path"])
        output_parent = _ensure_managed_owned_directory(root, "outputs", uid, gid)
        if stdout.parent != output_parent or stderr.parent != output_parent:
            raise LifecycleValidationError(
                "JOB_OUTPUT_REJECTED", "job output directories are inconsistent"
            )
        argv = _managed_sbatch_argv(
            payload,
            workdir=workdir,
            stdout=stdout,
            stderr=stderr,
            staged_descriptor=staged_descriptor,
        )
        submitted = _run_as_managed_user(payload, argv, timeout=30, pass_fds=(staged_descriptor,))
        if not submitted.get("ok"):
            return {
                "status": "ERROR",
                "error": {
                    "code": "SBATCH_FAILED",
                    "message": str(submitted.get("stderr", "Slurm submission failed"))[:512],
                },
            }
        match = re.fullmatch(r"(\d+)(?:;[A-Za-z0-9_.-]+)?\s*", str(submitted.get("stdout", "")))
        if match is None:
            raise LifecycleValidationError(
                "SBATCH_RESPONSE_REJECTED", "sbatch returned an invalid job ID"
            )
        job_id = int(match.group(1))
        owner = _slurm_job_owner(job_id)
        if owner != payload["username"]:
            run_fixed("scancel", [str(job_id)], timeout=15)
            raise LifecycleValidationError(
                "JOB_OWNER_POSTCONDITION_FAILED", "submitted Slurm job owner is incorrect"
            )
        return {
            "status": "SUCCEEDED",
            "handler": "self.job.submit",
            "request_id": request.request_id,
            "slurm_job_id": job_id,
            "slurm_user": owner,
            "uid": uid,
            "gid": gid,
            "gpu_count": payload["gpu_count"],
            "lease_deadline_at": payload["lease_deadline_at"],
            "shell": False,
        }
    except LifecycleValidationError as exc:
        return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}
    finally:
        if staged_descriptor is not None:
            with suppress(OSError):
                os.close(staged_descriptor)


def _execute_self_job_cancel(request: WorkerRequest, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        _managed_account(payload)
        job_id = int(payload["slurm_job_id"])
        if _slurm_job_owner(job_id) != payload["username"]:
            raise LifecycleValidationError(
                "JOB_OWNERSHIP_REJECTED", "Slurm job does not belong to the managed user"
            )
        cancelled = _run_as_managed_user(payload, [BINARIES["scancel"], str(job_id)], timeout=20)
        if not cancelled.get("ok"):
            raise LifecycleValidationError("JOB_CANCEL_FAILED", "Slurm job cancellation failed")
        return {
            "status": "SUCCEEDED",
            "handler": "self.job.cancel",
            "request_id": request.request_id,
            "slurm_job_id": job_id,
            "slurm_user": payload["username"],
            "job_state": "CANCELLED",
        }
    except LifecycleValidationError as exc:
        return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}


def _read_job_log(root: Path, relative: str, uid: int, gid: int) -> str:
    try:
        with _open_managed_user_path(root, relative, directory=False, uid=uid, gid=gid) as (
            _path,
            descriptor,
            metadata,
        ):
            start = metadata.st_size - MAX_OUTPUT if metadata.st_size > MAX_OUTPUT else 0
            os.lseek(descriptor, start, os.SEEK_SET)
            content = os.read(descriptor, MAX_OUTPUT)
    except LifecycleValidationError as exc:
        if exc.code == "USER_PATH_NOT_FOUND":
            return ""
        raise
    return content.decode("utf-8", errors="replace")


def _self_job_logs(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        _managed_account(payload)
        uid = int(payload["uid"])
        gid = int(payload["gid"])
        root = Path(str(payload["workspace_path"]))
        if root != Path(workspace_path(uid)):
            raise LifecycleValidationError(
                "WORKSPACE_BINDING_REJECTED", "workspace is not derived from the managed UID"
            )
        return {
            "status": "OK",
            "handler": "self.job.logs.read",
            "stdout": _read_job_log(root, str(payload["stdout_relative_path"]), uid, gid),
            "stderr": _read_job_log(root, str(payload["stderr_relative_path"]), uid, gid),
        }
    except LifecycleValidationError as exc:
        return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}


def _self_job_status(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        _managed_account(payload)
        job_id = int(payload["slurm_job_id"])
        if _slurm_job_owner(job_id) != payload["username"]:
            raise LifecycleValidationError(
                "JOB_OWNERSHIP_REJECTED", "Slurm job does not belong to the managed user"
            )
        live = run_fixed("scontrol", ["show", "job", str(job_id), "-o"], timeout=15)
        if live.get("ok"):
            line = str(live.get("stdout", ""))
            state_match = re.search(r"(?:^|\s)JobState=([^\s]+)", line)
            state = state_match.group(1) if state_match else "UNKNOWN"
            exit_code = None
        else:
            history = run_fixed(
                "sacct",
                [
                    "-X",
                    "-n",
                    "-P",
                    "-j",
                    str(job_id),
                    "-o",
                    "JobIDRaw,User,State,ExitCode,Elapsed,AllocTRES,StdOut,StdErr",
                ],
                timeout=20,
            )
            if not history.get("ok"):
                raise LifecycleValidationError(
                    "JOB_STATUS_FAILED", "Slurm job status is unavailable"
                )
            rows = [line.split("|") for line in str(history.get("stdout", "")).splitlines()]
            row = next(
                (
                    values
                    for values in rows
                    if len(values) >= 8
                    and values[0] == str(job_id)
                    and values[1] == payload["username"]
                ),
                None,
            )
            if row is None:
                raise LifecycleValidationError(
                    "JOB_STATUS_FAILED", "Slurm job status is unavailable"
                )
            state = row[2]
            exit_code = row[3]
        return {
            "status": "OK",
            "handler": "self.job.status.read",
            "slurm_job_id": job_id,
            "slurm_user": payload["username"],
            "job_state": state,
            "exit_code": exit_code,
        }
    except LifecycleValidationError as exc:
        return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}


def _self_storage(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        account = _managed_account(payload)
        root = Path(workspace_path(int(payload["uid"])))
        resolved = root.resolve(strict=True)
        metadata = resolved.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != account.pw_uid
            or metadata.st_gid != account.pw_gid
            or stat.S_IMODE(metadata.st_mode) & 0o007
        ):
            raise LifecycleValidationError(
                "STORAGE_OWNERSHIP_REJECTED", "user storage ownership or mode is invalid"
            )
        usage = run_fixed("du", ["-s", "-B1", str(resolved)], timeout=60)
        match = re.fullmatch(r"(\d+)\s+.+\n?", str(usage.get("stdout", "")))
        if not usage.get("ok") or match is None:
            raise LifecycleValidationError("STORAGE_USAGE_FAILED", "storage usage is unavailable")
        return {
            "status": "OK",
            "handler": "self.storage.read",
            "username": payload["username"],
            "used_bytes": int(match.group(1)),
            "private": True,
        }
    except (LifecycleValidationError, OSError) as exc:
        return {
            "status": "ERROR",
            "error": {
                "code": getattr(exc, "code", "STORAGE_READ_FAILED"),
                "message": str(exc),
            },
        }


def _self_workspace_check(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        _managed_account(payload)
        binding = _workspace_binding_for_payload(payload, require_alias=True)
        root = Path(binding.canonical_workspace)
        root_metadata = root.lstat()
        writable = bool(
            root_metadata.st_mode & stat.S_IWUSR and root_metadata.st_mode & stat.S_IXUSR
        )
        if not writable:
            raise LifecycleValidationError(
                "WORKSPACE_PERMISSION_REJECTED", "workspace is not owner-writable"
            )
        for relative in WORKSPACE_REQUIRED_DIRECTORIES:
            with _open_workspace_directory(
                root,
                relative.as_posix(),
                uid=binding.uid,
                gid=binding.gid,
            ) as (_path, _descriptor, metadata):
                if stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise LifecycleValidationError(
                        "WORKSPACE_DIRECTORY_REJECTED",
                        "required workspace directory mode is invalid",
                    )

        mount_target = run_fixed(
            "findmnt",
            [
                "--noheadings",
                "--output",
                "TARGET",
                "--mountpoint",
                str(binding.canonical_workspace),
            ],
            timeout=10,
        )
        mount_options = run_fixed(
            "findmnt",
            [
                "--noheadings",
                "--output",
                "OPTIONS",
                "--mountpoint",
                str(binding.canonical_workspace),
            ],
            timeout=10,
        )
        options = {
            option for option in str(mount_options.get("stdout", "")).strip().split(",") if option
        }
        if (
            not mount_target.get("ok")
            or str(mount_target.get("stdout", "")).strip() != str(binding.canonical_workspace)
            or not mount_options.get("ok")
            or not {"rw", "nosuid", "nodev"} <= options
        ):
            raise LifecycleValidationError(
                "WORKSPACE_MOUNT_REJECTED", "canonical workspace bind mount is invalid"
            )

        project_id = int(payload["project_id"])
        quota_bytes = int(payload["quota_bytes"])
        if (
            quota_bytes != WORKSPACE_QUOTA_BYTES
            or f"{project_id}:{binding.quota_root}" not in _safe_file_lines(PROJECTS_FILE)
            or f"h100_{binding.username}:{project_id}" not in _safe_file_lines(PROJID_FILE)
        ):
            raise LifecycleValidationError(
                "WORKSPACE_QUOTA_REJECTED", "workspace XFS project mapping is invalid"
            )
        try:
            quota = _verified_project_quota(project_id, quota_bytes // 1024**3)
        except LifecycleValidationError as exc:
            raise LifecycleValidationError(
                "WORKSPACE_QUOTA_REJECTED", "workspace XFS quota is not enforced"
            ) from exc
        if quota.get("enforcement") != "ON":
            raise LifecycleValidationError(
                "WORKSPACE_QUOTA_REJECTED", "workspace XFS quota is not enforced"
            )
        return {
            "status": "OK",
            "handler": "self.workspace.check",
            "username": binding.username,
            "uid": binding.uid,
            "gid": binding.gid,
            "ownership_verified": True,
            "writable": True,
            "same_inode": True,
            "quota_mapping_valid": True,
            "quota_enforced": True,
            "required_directories_ready": True,
            "mount_status": "PASS",
            "permission_status": "PASS",
            "storage_status": "PASS",
        }
    except (LifecycleValidationError, OSError) as exc:
        return {
            "status": "ERROR",
            "error": {
                "code": getattr(exc, "code", "WORKSPACE_CHECK_FAILED"),
                "message": str(exc),
            },
        }


def _gpu_allocation_binding(payload: dict[str, Any], job_id: int) -> str:
    """Return the single physical GPU UUID held by a running Slurm allocation."""

    detail = run_fixed("scontrol", ["show", "job", "-dd", "-o", str(job_id)], timeout=15)
    line = str(detail.get("stdout", ""))
    owner = re.search(r"(?:^|\s)UserId=([^\s(]+)", line)
    state = re.search(r"(?:^|\s)JobState=([^\s]+)", line)
    account = re.search(r"(?:^|\s)Account=([^\s]+)", line)
    qos = re.search(r"(?:^|\s)QOS=([^\s]+)", line)
    partition = re.search(r"(?:^|\s)Partition=([^\s]+)", line)
    comment = re.search(r"(?:^|\s)Comment=([^\s]+)", line)
    indexes = {int(value) for value in re.findall(r"IDX:(\d+)", line)}
    h100_request = bool(
        re.search(r"(?:^|\s)Gres=gpu:h100:1(?:\s|$)", line)
        or re.search(r"(?:^|\s)TresPerNode=gres/gpu:h100:1(?:\s|$)", line)
    )
    comment_is_bound = False
    if comment is not None:
        lease_prefix = f"h100-gpu-dev:{payload['managed_user_id']}:"
        lease_suffix = comment.group(1).removeprefix(lease_prefix)
        with suppress(ValueError):
            comment_is_bound = (
                comment.group(1).startswith(lease_prefix)
                and str(uuid.UUID(lease_suffix)) == lease_suffix
            )
    if (
        not detail.get("ok")
        or owner is None
        or owner.group(1) != payload["username"]
        or state is None
        or state.group(1) != "RUNNING"
        or account is None
        or account.group(1) != payload["slurm_account"]
        or qos is None
        or qos.group(1) != payload["slurm_qos"]
        or partition is None
        or partition.group(1) != "gpu-dev"
        or not comment_is_bound
        or not h100_request
        or len(indexes) != 1
    ):
        raise LifecycleValidationError(
            "GPU_ALLOCATION_NOT_RUNNING",
            "Slurm does not prove one running owner-bound GPU allocation",
        )
    mapping = run_fixed(
        "nvidia-smi", ["--query-gpu=index,uuid", "--format=csv,noheader,nounits"], timeout=15
    )
    rows: dict[int, str] = {}
    if mapping.get("ok"):
        for raw in str(mapping.get("stdout", "")).splitlines():
            index, separator, gpu_uuid = raw.partition(",")
            if separator and index.strip().isdigit():
                rows[int(index.strip())] = gpu_uuid.strip()
    resolved_gpu_uuid = rows.get(next(iter(indexes)))
    if (
        resolved_gpu_uuid is None
        or re.fullmatch(r"GPU-[0-9a-fA-F-]{32,40}", resolved_gpu_uuid) is None
    ):
        raise LifecycleValidationError(
            "GPU_ALLOCATION_IDENTITY_REJECTED",
            "allocated Slurm GPU could not be mapped to one physical UUID",
        )
    return resolved_gpu_uuid


def _gpu_development_partition_preflight() -> None:
    result = run_fixed("scontrol", ["show", "partition", "gpu-dev", "-o"], timeout=15)
    line = str(result.get("stdout", ""))
    configuration = run_fixed("scontrol", ["show", "config"], timeout=15)
    configuration_text = str(configuration.get("stdout", ""))
    integrity = script_integrity()
    epilog_integrity = integrity.get("h100-gpu-development-epilog", {})
    common_integrity = integrity.get("h100-platform-common", {})
    if (
        not result.get("ok")
        or re.search(r"(?:^|\s)PartitionName=gpu-dev(?:\s|$)", line) is None
        or re.search(r"(?:^|\s)Default=NO(?:\s|$)", line) is None
        or re.search(r"(?:^|\s)State=UP(?:\s|$)", line) is None
        or re.search(r"(?:^|\s)MaxTime=(?:INFINITE|UNLIMITED)(?:\s|$)", line) is None
        or not configuration.get("ok")
        or re.search(
            r"(?:^|\n)Epilog\s*=\s*/usr/local/sbin/h100-gpu-development-epilog(?:\s|$)",
            configuration_text,
        )
        is None
        or epilog_integrity.get("integrity_ok") is not True
        or common_integrity.get("integrity_ok") is not True
    ):
        raise LifecycleValidationError(
            "GPU_DEVELOPMENT_PARTITION_REJECTED",
            "the non-default GPU development safety-lock partition is unavailable",
        )


def _submit_gpu_development_allocation(payload: dict[str, Any]) -> tuple[int, str]:
    _gpu_development_partition_preflight()
    workspace = Path(str(payload["workspace_path"]))
    argv = [
        BINARIES["sbatch"],
        "--parsable",
        f"--account={payload['slurm_account']}",
        f"--qos={payload['slurm_qos']}",
        "--partition=gpu-dev",
        f"--job-name=portal-gpu-dev-{payload['uid']}",
        "--cpus-per-task=8",
        "--mem=32768M",
        "--gres=gpu:h100:1",
        f"--chdir={workspace}",
        "--export=ALL",
        f"--comment=h100-gpu-dev:{payload['managed_user_id']}:{payload['lease_id']}",
        f"--output={workspace}/outputs/.gpu-development-%j.out",
        f"--error={workspace}/outputs/.gpu-development-%j.err",
        "--wrap=/usr/bin/sleep infinity",
    ]
    submitted = _run_as_managed_user(payload, argv, timeout=30)
    match = re.fullmatch(r"(\d+)(?:;[A-Za-z0-9_.-]+)?\s*", str(submitted.get("stdout", "")))
    if not submitted.get("ok") or match is None:
        raise LifecycleValidationError(
            "GPU_ALLOCATION_SUBMIT_FAILED", "Slurm GPU development allocation failed"
        )
    job_id = int(match.group(1))
    try:
        for _attempt in range(120):
            try:
                return job_id, _gpu_allocation_binding(payload, job_id)
            except LifecycleValidationError as exc:
                if exc.code != "GPU_ALLOCATION_NOT_RUNNING":
                    raise
            time.sleep(0.5)
        raise LifecycleValidationError(
            "GPU_ALLOCATION_UNAVAILABLE", "one GPU did not become available before timeout"
        )
    except LifecycleValidationError:
        _run_as_managed_user(payload, [BINARIES["scancel"], str(job_id)], timeout=20)
        raise


GPU_ALLOCATION_TERMINAL_STATES = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "REVOKED",
        "SPECIAL_EXIT",
        "STOPPED",
        "TIMEOUT",
    }
)


def _gpu_allocation_terminal_binding(payload: dict[str, Any], job_id: int) -> None:
    """Prove that one exact owner-bound GPU allocation reached a terminal state."""

    accounting = run_fixed(
        "sacct",
        [
            "-n",
            "-X",
            "-P",
            "-j",
            str(job_id),
            "--format=JobIDRaw,User,Account,QOS,Partition,State,ReqTRES,AllocTRES,Comment",
        ],
        timeout=15,
    )
    rows: list[list[str]] = []
    if accounting.get("ok"):
        for raw_line in str(accounting.get("stdout", "")).splitlines():
            fields = raw_line.rstrip("\n").split("|")
            if len(fields) == 10 and fields[-1] == "" and fields[0] == str(job_id):
                rows.append(fields[:-1])
    if len(rows) != 1:
        raise LifecycleValidationError(
            "GPU_ALLOCATION_TERMINAL_UNPROVEN",
            "Slurm accounting does not contain one exact GPU allocation record",
        )
    (
        _job_id,
        owner,
        account,
        qos,
        partition,
        raw_state,
        requested_tres,
        allocated_tres,
        comment,
    ) = rows[0]
    lease_prefix = f"h100-gpu-dev:{payload['managed_user_id']}:"
    lease_suffix = comment.removeprefix(lease_prefix)
    comment_is_bound = False
    with suppress(ValueError):
        comment_is_bound = (
            comment.startswith(lease_prefix) and str(uuid.UUID(lease_suffix)) == lease_suffix
        )
    typed_gpu = re.compile(r"(?:^|,)gres/gpu:h100=1(?:,|$)")
    if (
        owner != payload["username"]
        or account != payload["slurm_account"]
        or qos != payload["slurm_qos"]
        or partition != "gpu-dev"
        or not comment_is_bound
        or typed_gpu.search(requested_tres) is None
        or typed_gpu.search(allocated_tres) is None
    ):
        raise LifecycleValidationError(
            "GPU_ALLOCATION_OWNERSHIP_REJECTED",
            "terminal GPU allocation is not bound to this owner, account, QOS, and profile",
        )
    state = raw_state.partition(" ")[0].rstrip("+").upper()
    if state not in GPU_ALLOCATION_TERMINAL_STATES:
        raise LifecycleValidationError(
            "GPU_ALLOCATION_STILL_ACTIVE",
            "owner-bound GPU allocation has not reached a terminal state",
        )


def _cancel_gpu_development_allocation(payload: dict[str, Any], job_id: int) -> None:
    try:
        _gpu_allocation_binding(payload, job_id)
    except LifecycleValidationError as exc:
        if exc.code != "GPU_ALLOCATION_NOT_RUNNING":
            raise LifecycleValidationError(
                "GPU_ALLOCATION_OWNERSHIP_REJECTED",
                "GPU allocation is not bound to this owner, lease, account, and QOS",
            ) from exc
        _gpu_allocation_terminal_binding(payload, job_id)
        return
    cancelled = _run_as_managed_user(payload, [BINARIES["scancel"], str(job_id)], timeout=20)
    if not cancelled.get("ok"):
        raise LifecycleValidationError(
            "GPU_ALLOCATION_CANCEL_FAILED", "GPU allocation could not be revoked"
        )
    for _attempt in range(40):
        try:
            _gpu_allocation_terminal_binding(payload, job_id)
        except LifecycleValidationError as exc:
            if exc.code != "GPU_ALLOCATION_STILL_ACTIVE":
                raise LifecycleValidationError(
                    "GPU_ALLOCATION_CANCEL_UNPROVEN",
                    "GPU allocation revocation could not be proven",
                ) from exc
            time.sleep(0.25)
        else:
            return
    raise LifecycleValidationError(
        "GPU_ALLOCATION_CANCEL_UNPROVEN",
        "GPU allocation remained active after cancellation",
    )


def _managed_container_security(
    payload: dict[str, Any], *, require_running: bool | None
) -> dict[str, Any]:
    account = _managed_account(payload)
    workspace = Path(str(payload["workspace_path"]))
    backing_workspace = PILOT_DATA_ROOT / str(payload["username"]) / "workspace"
    try:
        workspace_metadata = workspace.lstat()
    except OSError as exc:
        raise LifecycleValidationError(
            "WORKSPACE_BINDING_REJECTED", "canonical workspace is unavailable"
        ) from exc
    if (
        workspace != Path(workspace_path(account.pw_uid))
        or not stat.S_ISDIR(workspace_metadata.st_mode)
        or stat.S_ISLNK(workspace_metadata.st_mode)
        or workspace_metadata.st_uid != account.pw_uid
        or workspace_metadata.st_gid != account.pw_gid
        or stat.S_IMODE(workspace_metadata.st_mode) != 0o700
    ):
        raise LifecycleValidationError(
            "WORKSPACE_BINDING_REJECTED", "canonical workspace ownership or mode is invalid"
        )
    inspected = containers_inspect({"name": str(payload["name"])})
    raw_container = inspected.get("container", {})
    container: dict[str, Any] = raw_container if isinstance(raw_container, dict) else {}
    state = container.get("state", {})
    mounts = container.get("mounts", [])
    forbidden_destinations = {"/", "/root", "/etc", "/var/run", "/var/run/docker.sock"}
    forbidden_sources = {
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/etc/munge",
        "/run/munge",
        "/var/run/munge",
    }
    profile_is_gpu = payload["development_profile"] == GPU_DEVELOPMENT_PROFILE
    allocation_job_id = payload.get("gpu_allocation_job_id")
    allocation_uuid = payload.get("gpu_allocation_uuid")
    running = bool(state.get("Running"))
    device_requests = container.get("device_requests")
    safe_environment = container.get("safe_environment", {})
    safe_labels = container.get("safe_labels", {})
    gpu_runtime_valid = (
        isinstance(device_requests, list)
        and len(device_requests) == 1
        and isinstance(device_requests[0], dict)
        and device_requests[0].get("Driver") == "nvidia"
        and device_requests[0].get("DeviceIDs") == [allocation_uuid]
        and device_requests[0].get("Capabilities") == [["gpu"]]
        and device_requests[0].get("Options") in (None, {})
        and container.get("runtime") == "nvidia"
        and safe_environment
        == {"CUDA_VISIBLE_DEVICES": "0", "NVIDIA_VISIBLE_DEVICES": allocation_uuid}
        and safe_labels.get("h100.dev.gpu-allocation-job") == str(allocation_job_id)
        and safe_labels.get("h100.dev.gpu-uuid") == allocation_uuid
    )
    approved_workspace_mounts = {
        (str(payload["workspace_path"]), "/workspace"),
        (str(backing_workspace), "/workspace"),
    }
    observed_workspace_mounts = {
        (str(item.get("Source", "")), str(item.get("Destination", "")))
        for item in mounts
        if isinstance(item, dict)
        and item.get("Type") == "bind"
        and item.get("RW") is True
        and item.get("Destination") == "/workspace"
    }
    legacy_workspace_mount = (str(backing_workspace), "/workspace") in (observed_workspace_mounts)
    legacy_workspace_safe = True
    if legacy_workspace_mount:
        try:
            backing_metadata = backing_workspace.lstat()
        except OSError:
            legacy_workspace_safe = False
        else:
            legacy_workspace_safe = bool(
                stat.S_ISDIR(backing_metadata.st_mode)
                and not stat.S_ISLNK(backing_metadata.st_mode)
                and backing_metadata.st_uid == account.pw_uid
                and backing_metadata.st_gid == account.pw_gid
                and stat.S_IMODE(backing_metadata.st_mode) == 0o700
                and (workspace_metadata.st_dev, workspace_metadata.st_ino)
                == (backing_metadata.st_dev, backing_metadata.st_ino)
            )
    allowed_rw_mounts = {
        (
            str(PILOT_DATA_ROOT / str(payload["username"]) / "home"),
            f"/home/{payload['username']}",
        ),
        (
            str(PILOT_DATA_ROOT / str(payload["username"]) / "shared"),
            "/shared",
        ),
        (
            f"/srv/gpu-platform/container-data/{payload['username']}/ssh-host-keys",
            "/etc/ssh/persistent",
        ),
    } | observed_workspace_mounts
    observed_rw_mounts = {
        (str(item.get("Source", "")), str(item.get("Destination", "")))
        for item in mounts
        if isinstance(item, dict) and item.get("Type") == "bind" and item.get("RW") is True
    }
    if not (
        inspected.get("status") == "OK"
        and container.get("owner") == payload["username"]
        and safe_labels.get("h100.dev.uid") == str(payload["uid"])
        and safe_labels.get("h100.dev.gid") == str(payload["gid"])
        and container.get("privileged") is False
        and container.get("network_mode") != "host"
        and container.get("pid_mode") != "host"
        and container.get("ipc_mode") != "host"
        and not container.get("devices")
        and not container.get("device_cgroup_rules")
        and not container.get("cap_add")
        and (
            gpu_runtime_valid and container.get("gpu") == "REQUESTED"
            if profile_is_gpu and allocation_job_id is not None and running
            else container.get("gpu") == "NONE"
            and not device_requests
            and not safe_environment
            and container.get("runtime") != "nvidia"
        )
        and not container.get("docker_socket_mounted")
        and len(observed_workspace_mounts) == 1
        and observed_workspace_mounts <= approved_workspace_mounts
        and legacy_workspace_safe
        and observed_rw_mounts == allowed_rw_mounts
        and len(mounts) == len(allowed_rw_mounts)
        and not any(
            str(item.get("Destination", "")) in forbidden_destinations
            or str(item.get("Source", "")) in forbidden_sources
            or "munge" in str(item.get("Destination", "")).casefold()
            for item in mounts
            if isinstance(item, dict)
        )
    ):
        raise LifecycleValidationError(
            "CONTAINER_SECURITY_REJECTED", "managed container security contract changed"
        )
    if require_running is not None and running is not require_running:
        raise LifecycleValidationError(
            "CONTAINER_STATE_REJECTED", "managed container state differs from request"
        )
    return container


def _execute_managed_container_lifecycle(
    request: WorkerRequest, payload: dict[str, Any]
) -> dict[str, Any]:
    new_allocation = False
    start_invoked = False
    allocation_lifecycle_bound = False
    allocation_job_id = payload.get("gpu_allocation_job_id")
    allocation_uuid = payload.get("gpu_allocation_uuid")
    try:
        if request.requested_by != payload["username"]:
            raise LifecycleValidationError(
                "RESOURCE_OWNERSHIP_REJECTED",
                "user-level operation actor does not own the target resource",
            )
        action = request.operation_type.rsplit(".", 1)[-1]
        if action in {"start", "restart"} and payload.get("lease_id") is None:
            raise LifecycleValidationError(
                "CONTAINER_OPERATION_DENIED_LEASE_INACTIVE", "active lease binding is required"
            )
        before = _managed_container_security(
            payload, require_running=False if action == "start" else None
        )
        required_scripts = {"h100-container-stop"}
        if action in {"start", "restart"}:
            required_scripts.add("h100-container-start")
        if payload["development_profile"] == GPU_DEVELOPMENT_PROFILE:
            required_scripts.add("h100-container-gpu-runtime")
        integrity = script_integrity()
        if not all(integrity.get(name, {}).get("integrity_ok", False) for name in required_scripts):
            raise LifecycleValidationError(
                "SCRIPT_INTEGRITY_FAILED", "container lifecycle script integrity failed"
            )
        profile_is_gpu = payload["development_profile"] == GPU_DEVELOPMENT_PROFILE
        if action in {"stop", "restart"} and bool(before.get("state", {}).get("Running")):
            if profile_is_gpu:
                if not isinstance(allocation_job_id, int) or not isinstance(allocation_uuid, str):
                    raise LifecycleValidationError(
                        "GPU_ALLOCATION_BINDING_REJECTED",
                        "running GPU container has no allocation binding",
                    )
                _gpu_allocation_binding(payload, allocation_job_id)
                stopped = run_allowlisted_script(
                    [
                        SCRIPT_ALLOWLIST["h100-container-gpu-runtime"],
                        "stop",
                        str(payload["username"]),
                        str(allocation_job_id),
                        allocation_uuid,
                    ],
                    timeout=150,
                )
                if stopped.get("ok"):
                    _cancel_gpu_development_allocation(payload, allocation_job_id)
                    stopped_job_id = allocation_job_id
                    stopped_gpu_uuid = allocation_uuid
                    allocation_job_id = None
                    allocation_uuid = None
                    _clear_active_gpu_lifecycle_binding(
                        payload,
                        expected_job_id=stopped_job_id,
                        expected_gpu_uuid=stopped_gpu_uuid,
                    )
            else:
                stopped = run_allowlisted_script(
                    [SCRIPT_ALLOWLIST["h100-container-stop"], str(payload["username"])],
                    timeout=90,
                )
            if not stopped.get("ok"):
                raise LifecycleValidationError("CONTAINER_STOP_FAILED", "container stop failed")
        elif (
            action in {"stop", "restart"} and profile_is_gpu and isinstance(allocation_job_id, int)
        ):
            assert isinstance(allocation_uuid, str)
            _cancel_gpu_development_allocation(payload, allocation_job_id)
            stopped_job_id = allocation_job_id
            stopped_gpu_uuid = allocation_uuid
            allocation_job_id = None
            allocation_uuid = None
            _clear_active_gpu_lifecycle_binding(
                payload,
                expected_job_id=stopped_job_id,
                expected_gpu_uuid=stopped_gpu_uuid,
            )
        if action in {"start", "restart"}:
            if profile_is_gpu:
                allocation_payload = {
                    **payload,
                    "gpu_allocation_job_id": None,
                    "gpu_allocation_uuid": None,
                }
                allocation_job_id, allocation_uuid = _submit_gpu_development_allocation(
                    allocation_payload
                )
                new_allocation = True
                _bind_active_container_lifecycle(
                    payload,
                    gpu_allocation_job_id=allocation_job_id,
                    gpu_allocation_uuid=allocation_uuid,
                )
                allocation_lifecycle_bound = True
                start_invoked = True
                started = run_allowlisted_script(
                    [
                        SCRIPT_ALLOWLIST["h100-container-gpu-runtime"],
                        "start",
                        str(payload["username"]),
                        str(allocation_job_id),
                        allocation_uuid,
                    ],
                    timeout=150,
                )
                if not started.get("ok"):
                    # The fixed wrapper can fail after Docker has created or started
                    # the container.  Keep the allocation bound until the exception
                    # rollback has first removed that possibly live container.
                    pass
            else:
                _bind_active_container_lifecycle(
                    payload,
                    gpu_allocation_job_id=None,
                    gpu_allocation_uuid=None,
                )
                started = run_allowlisted_script(
                    [SCRIPT_ALLOWLIST["h100-container-start"], str(payload["username"])],
                    timeout=150,
                )
            if not started.get("ok"):
                raise LifecycleValidationError("CONTAINER_START_FAILED", "container start failed")
        expected_running = action != "stop"
        postflight_payload = {
            **payload,
            "gpu_allocation_job_id": allocation_job_id,
            "gpu_allocation_uuid": allocation_uuid,
        }
        _managed_container_security(postflight_payload, require_running=expected_running)
        if (
            profile_is_gpu
            and expected_running
            and isinstance(allocation_job_id, int)
            and _gpu_allocation_binding(postflight_payload, allocation_job_id) != allocation_uuid
        ):
            raise LifecycleValidationError(
                "GPU_ALLOCATION_POSTCONDITION_FAILED",
                "container GPU differs from the live Slurm allocation",
            )
        return {
            "status": "SUCCEEDED",
            "handler": request.operation_type,
            "request_id": request.request_id,
            "name": payload["name"],
            "username": payload["username"],
            "container_state": "RUNNING" if expected_running else "STOPPED",
            "container_gpu": payload["expected_gpu"],
            "gpu_allocation_job_id": allocation_job_id,
            "gpu_allocation_uuid": allocation_uuid,
        }
    except LifecycleValidationError as exc:
        rollback_failed = False
        if new_allocation and isinstance(allocation_job_id, int):
            failed_job_id = allocation_job_id
            failed_gpu_uuid = allocation_uuid
            if start_invoked and isinstance(allocation_uuid, str):
                stopped = run_allowlisted_script(
                    [
                        SCRIPT_ALLOWLIST["h100-container-gpu-runtime"],
                        "stop",
                        str(payload["username"]),
                        str(allocation_job_id),
                        allocation_uuid,
                    ],
                    timeout=150,
                )
                rollback_failed = not bool(stopped.get("ok"))
            if not rollback_failed:
                try:
                    _cancel_gpu_development_allocation(payload, allocation_job_id)
                except LifecycleValidationError:
                    rollback_failed = True
            if not rollback_failed:
                allocation_job_id = None
                allocation_uuid = None
                if allocation_lifecycle_bound and isinstance(failed_gpu_uuid, str):
                    try:
                        _clear_active_gpu_lifecycle_binding(
                            payload,
                            expected_job_id=failed_job_id,
                            expected_gpu_uuid=failed_gpu_uuid,
                        )
                    except LifecycleValidationError:
                        rollback_failed = True
        if rollback_failed:
            return {
                "status": "ERROR",
                "gpu_allocation_state_known": False,
                "error": {
                    "code": "GPU_ALLOCATION_ROLLBACK_FAILED",
                    "message": "GPU container start failed and allocation cleanup is incomplete",
                    "cause": exc.code,
                },
            }
        return {
            "status": "ERROR",
            "gpu_allocation_state_known": True,
            "gpu_allocation_job_id": allocation_job_id,
            "gpu_allocation_uuid": allocation_uuid,
            "error": {"code": exc.code, "message": str(exc)},
        }


def _execute_resource_restore(request: WorkerRequest, payload: dict[str, Any]) -> dict[str, Any]:
    active = PILOT_DATA_ROOT / str(payload["username"]) / "home/.ssh/authorized_keys"
    suspended = active.with_name("authorized_keys.portal-recycle")
    start_invoked = False
    prior_lifecycle: dict[str, str] | None = None
    allocation_job_id: int | None = None
    allocation_uuid: str | None = None
    try:
        if request.operation_type == "self.resource.restore" and (
            request.requested_by != payload["username"]
            or request.approved_by != payload["username"]
        ):
            raise LifecycleValidationError(
                "RESOURCE_OWNERSHIP_REJECTED",
                "self-service restore actor does not own the target resource",
            )
        account = _managed_account(payload)
        if account.pw_shell != "/usr/sbin/nologin":
            raise LifecycleValidationError(
                "HOST_ACCESS_POLICY_REJECTED", "restore requires host login to remain disabled"
            )
        host_keys = MANAGED_HOME_ROOT / str(payload["username"]) / ".ssh/authorized_keys"
        if host_keys.exists():
            raise LifecycleValidationError(
                "HOST_ACCESS_POLICY_REJECTED", "restore must not reinstall host authorized_keys"
            )
        if active.exists() and suspended.exists():
            raise LifecycleValidationError(
                "CONTAINER_KEY_RESTORE_FAILED", "active and suspended SSH authorization conflict"
            )
        key_path = active if active.exists() else suspended
        if not key_path.exists():
            raise LifecycleValidationError(
                "CONTAINER_KEY_RESTORE_FAILED", "suspended container public key is unavailable"
            )
        fingerprints = _installed_key_fingerprints(
            key_path, int(payload["uid"]), int(payload["gid"])
        )
        if sorted(fingerprints) != payload["expected_key_fingerprints"]:
            raise LifecycleValidationError(
                "CONTAINER_KEY_BINDING_REJECTED",
                "container SSH authorization differs from the approved Portal key records",
            )
        lifecycle_payload = {
            **payload,
            "name": payload["container_name"],
        }
        inspected = containers_inspect({"name": str(payload["container_name"])})
        raw_container = inspected.get("container", {})
        observed_container = raw_container if isinstance(raw_container, dict) else {}
        observed_state = observed_container.get("state", {})
        observed_running = bool(
            isinstance(observed_state, dict) and observed_state.get("Running") is True
        )
        if observed_running and payload["development_profile"] == GPU_DEVELOPMENT_PROFILE:
            labels = observed_container.get("safe_labels", {})
            raw_job_id = labels.get("h100.dev.gpu-allocation-job")
            raw_uuid = labels.get("h100.dev.gpu-uuid")
            if (
                not isinstance(raw_job_id, str)
                or not raw_job_id.isdigit()
                or not isinstance(raw_uuid, str)
                or re.fullmatch(r"GPU-[0-9a-fA-F-]{32,40}", raw_uuid) is None
            ):
                raise LifecycleValidationError(
                    "GPU_ALLOCATION_BINDING_REJECTED",
                    "running restored GPU container lacks an allocation binding",
                )
            allocation_job_id = int(raw_job_id)
            allocation_uuid = raw_uuid
            lifecycle_payload.update(
                {
                    "gpu_allocation_job_id": allocation_job_id,
                    "gpu_allocation_uuid": allocation_uuid,
                }
            )
        container = _managed_container_security(lifecycle_payload, require_running=None)
        running = bool(container.get("state", {}).get("Running"))
        if running:
            if not active.exists():
                raise LifecycleValidationError(
                    "CONTAINER_KEY_RESTORE_FAILED",
                    "running restored container does not have active SSH authorization",
                )
            _validate_active_restored_lifecycle(
                payload,
                fingerprints,
                gpu_allocation_job_id=allocation_job_id,
                gpu_allocation_uuid=allocation_uuid,
            )
            if isinstance(allocation_job_id, int) and (
                _gpu_allocation_binding(lifecycle_payload, allocation_job_id) != allocation_uuid
            ):
                raise LifecycleValidationError(
                    "GPU_ALLOCATION_POSTCONDITION_FAILED",
                    "restored container GPU differs from its live Slurm allocation",
                )
            return {
                "status": "SUCCEEDED",
                "handler": request.operation_type,
                "request_id": request.request_id,
                "container_state": "RUNNING",
                "container_gpu": payload["expected_gpu"],
                "gpu_allocation_job_id": allocation_job_id,
                "gpu_allocation_uuid": allocation_uuid,
                "host_access": "DISABLED",
                "container_key_state": "INSTALLED",
                "container_key_fingerprints": fingerprints,
                "idempotent_replay": True,
            }
        if suspended.exists():
            os.replace(suspended, active)
        integrity = script_integrity()
        start_script = (
            "h100-container-gpu-runtime"
            if payload["development_profile"] == GPU_DEVELOPMENT_PROFILE
            else "h100-container-start"
        )
        if not integrity.get(start_script, {}).get("integrity_ok", False):
            raise LifecycleValidationError(
                "SCRIPT_INTEGRITY_FAILED", "container start script integrity failed"
            )
        if payload["development_profile"] == GPU_DEVELOPMENT_PROFILE:
            allocation_job_id, allocation_uuid = _submit_gpu_development_allocation(payload)
            prior_lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
            _activate_restored_lifecycle(
                payload,
                fingerprints,
                gpu_allocation_job_id=allocation_job_id,
                gpu_allocation_uuid=allocation_uuid,
            )
            # A failed start can still leave a partially created runtime, so
            # rollback must prove it removed before releasing the allocation.
            start_invoked = True
            started = run_allowlisted_script(
                [
                    SCRIPT_ALLOWLIST["h100-container-gpu-runtime"],
                    "start",
                    str(payload["username"]),
                    str(allocation_job_id),
                    allocation_uuid,
                ],
                timeout=150,
            )
            lifecycle_payload.update(
                {
                    "gpu_allocation_job_id": allocation_job_id,
                    "gpu_allocation_uuid": allocation_uuid,
                }
            )
        else:
            prior_lifecycle = _read_managed_lifecycle_state(str(payload["username"]))
            _activate_restored_lifecycle(
                payload,
                fingerprints,
                gpu_allocation_job_id=None,
                gpu_allocation_uuid=None,
            )
            start_invoked = True
            started = run_allowlisted_script(
                [SCRIPT_ALLOWLIST["h100-container-start"], str(payload["username"])],
                timeout=150,
            )
        if not started.get("ok"):
            raise LifecycleValidationError(
                "CONTAINER_START_FAILED", "restored container failed to start"
            )
        _managed_container_security(lifecycle_payload, require_running=True)
        if isinstance(allocation_job_id, int) and (
            _gpu_allocation_binding(lifecycle_payload, allocation_job_id) != allocation_uuid
        ):
            raise LifecycleValidationError(
                "GPU_ALLOCATION_POSTCONDITION_FAILED",
                "restored container GPU differs from its live Slurm allocation",
            )
        return {
            "status": "SUCCEEDED",
            "handler": request.operation_type,
            "request_id": request.request_id,
            "container_state": "RUNNING",
            "container_gpu": payload["expected_gpu"],
            "gpu_allocation_job_id": allocation_job_id,
            "gpu_allocation_uuid": allocation_uuid,
            "host_access": "DISABLED",
            "container_key_state": "INSTALLED",
            "container_key_fingerprints": fingerprints,
            "idempotent_replay": False,
        }
    except (LifecycleValidationError, OSError) as raw_exc:
        exc = (
            raw_exc
            if isinstance(raw_exc, LifecycleValidationError)
            else LifecycleValidationError(
                "CONTAINER_KEY_RESTORE_FAILED", "container SSH authorization could not be restored"
            )
        )
        rollback_errors: list[str] = []
        allocation_state_known = True
        if start_invoked:
            integrity = script_integrity()
            stop_script = (
                "h100-container-gpu-runtime"
                if isinstance(allocation_job_id, int) and isinstance(allocation_uuid, str)
                else "h100-container-stop"
            )
            container_removal_proven = False
            if not integrity.get(stop_script, {}).get("integrity_ok", False):
                rollback_errors.append("container stop script integrity failed")
            else:
                stop_argv = (
                    [
                        SCRIPT_ALLOWLIST["h100-container-gpu-runtime"],
                        "stop",
                        str(payload["username"]),
                        str(allocation_job_id),
                        str(allocation_uuid),
                    ]
                    if stop_script == "h100-container-gpu-runtime"
                    else [SCRIPT_ALLOWLIST["h100-container-stop"], str(payload["username"])]
                )
                stopped = run_allowlisted_script(stop_argv, timeout=150)
                if not stopped.get("ok"):
                    rollback_errors.append("restored container could not be stopped")
                else:
                    container_removal_proven = True
            if isinstance(allocation_job_id, int) and container_removal_proven:
                try:
                    _cancel_gpu_development_allocation(payload, allocation_job_id)
                except LifecycleValidationError:
                    allocation_state_known = False
                    rollback_errors.append("restored GPU allocation could not be cancelled")
                else:
                    allocation_job_id = None
                    allocation_uuid = None
        elif isinstance(allocation_job_id, int):
            try:
                _cancel_gpu_development_allocation(payload, allocation_job_id)
            except LifecycleValidationError:
                allocation_state_known = False
                rollback_errors.append("unused restored GPU allocation could not be cancelled")
            else:
                allocation_job_id = None
                allocation_uuid = None
        if active.exists():
            try:
                if suspended.exists():
                    raise OSError("suspended authorization target already exists")
                os.replace(active, suspended)
            except OSError:
                rollback_errors.append("container SSH authorization could not be suspended")
        if prior_lifecycle is not None:
            try:
                _restore_prior_lifecycle(payload, prior_lifecycle)
            except LifecycleValidationError:
                rollback_errors.append("managed lifecycle Lease could not be rolled back")
        if rollback_errors:
            return {
                "status": "ERROR",
                "gpu_allocation_state_known": allocation_state_known,
                "gpu_allocation_job_id": allocation_job_id,
                "gpu_allocation_uuid": allocation_uuid,
                "error": {
                    "code": "RESTORE_ROLLBACK_FAILED",
                    "message": "restore failed and its safety rollback is incomplete",
                    "cause": exc.code,
                },
            }
        return {
            "status": "ERROR",
            "gpu_allocation_state_known": True,
            "gpu_allocation_job_id": None,
            "gpu_allocation_uuid": None,
            "error": {"code": exc.code, "message": str(exc), "rollback_status": "ROLLED_BACK"},
        }


def _active_user_slurm_jobs(username: str) -> list[tuple[int, str]]:
    result = run_fixed("squeue", ["-h", "-u", username, "-o", "%A|%T"], timeout=20)
    if not result.get("ok"):
        raise LifecycleValidationError(
            "SLURM_JOB_QUERY_FAILED", "user Slurm jobs could not be read"
        )
    jobs: list[tuple[int, str]] = []
    for line in str(result.get("stdout", "")).splitlines():
        raw_id, separator, state = line.partition("|")
        if not separator or not raw_id.isdigit():
            raise LifecycleValidationError("SLURM_JOB_QUERY_FAILED", "Slurm job output is invalid")
        jobs.append((int(raw_id), state.strip().upper()))
    return jobs


def _execute_resource_recycle(request: WorkerRequest, payload: dict[str, Any]) -> dict[str, Any]:
    restore_rollback = request.operation_type == "resource.restore.rollback"
    result_lease_id = payload["recycle_lease_id"] if restore_rollback else payload["lease_id"]
    fingerprints: list[str] = []
    cancelled_pending_ids: list[int] = []
    cancelled_running_ids: list[int] = []
    key_suspended = False
    gpu_allocation_revoked = False
    failed_step = "RECYCLE_PREFLIGHT"
    try:
        if restore_rollback and (
            request.requested_by != payload["username"]
            or request.approved_by != payload["username"]
        ):
            raise LifecycleValidationError(
                "RESOURCE_OWNERSHIP_REJECTED",
                "restore rollback actor does not own the target resource",
            )
        failed_step = "ACCOUNT_SECURITY_GATE"
        account = _managed_account(payload)
        if account.pw_shell != "/usr/sbin/nologin":
            raise LifecycleValidationError(
                "HOST_ACCESS_POLICY_REJECTED", "lease recycle requires host shell to be disabled"
            )
        host_keys = MANAGED_HOME_ROOT / str(payload["username"]) / ".ssh/authorized_keys"
        if host_keys.exists():
            raise LifecycleValidationError(
                "HOST_ACCESS_POLICY_REJECTED", "lease recycle found active host authorized_keys"
            )
        active = PILOT_DATA_ROOT / str(payload["username"]) / "home/.ssh/authorized_keys"
        suspended = active.with_name("authorized_keys.portal-recycle")
        if active.exists() and suspended.exists():
            raise LifecycleValidationError(
                "CONTAINER_KEY_SUSPEND_CONFLICT", "suspended key target already exists"
            )
        key_path = active if active.exists() else suspended
        if not key_path.exists():
            raise LifecycleValidationError(
                "CONTAINER_KEY_SUSPEND_FAILED", "container authorized_keys is unavailable"
            )
        fingerprints = _installed_key_fingerprints(
            key_path, int(payload["uid"]), int(payload["gid"])
        )
        if sorted(fingerprints) != payload["expected_key_fingerprints"]:
            raise LifecycleValidationError(
                "CONTAINER_KEY_BINDING_REJECTED",
                "container SSH authorization differs from the approved Portal key records",
            )
        failed_step = "CONTAINER_KEY_SUSPEND"
        if active.exists():
            try:
                os.replace(active, suspended)
            except OSError as exc:
                raise LifecycleValidationError(
                    "CONTAINER_KEY_SUSPEND_FAILED",
                    "container SSH authorization could not be suspended",
                ) from exc
        key_suspended = suspended.exists() and not active.exists()
        if not key_suspended:
            raise LifecycleValidationError(
                "CONTAINER_KEY_SUSPEND_FAILED",
                "container SSH authorization suspension is incomplete",
            )
        lifecycle_payload = {
            **payload,
            "name": payload["container_name"],
        }
        failed_step = "CONTAINER_SECURITY_PREFLIGHT"
        container = _managed_container_security(lifecycle_payload, require_running=None)
        running = bool(container.get("state", {}).get("Running"))
        allocation_job_id = payload.get("gpu_allocation_job_id")
        allocation_uuid = payload.get("gpu_allocation_uuid")
        if running:
            failed_step = "CONTAINER_STOP"
            is_gpu = payload["development_profile"] == GPU_DEVELOPMENT_PROFILE
            script_name = "h100-container-gpu-runtime" if is_gpu else "h100-container-stop"
            integrity = script_integrity()
            if not integrity.get(script_name, {}).get("integrity_ok", False):
                raise LifecycleValidationError(
                    "SCRIPT_INTEGRITY_FAILED", "container stop script integrity failed"
                )
            if is_gpu:
                if not isinstance(allocation_job_id, int) or not isinstance(allocation_uuid, str):
                    raise LifecycleValidationError(
                        "GPU_ALLOCATION_BINDING_REJECTED",
                        "running GPU container has no allocation binding",
                    )
                if _gpu_allocation_binding(lifecycle_payload, allocation_job_id) != allocation_uuid:
                    raise LifecycleValidationError(
                        "GPU_ALLOCATION_POSTCONDITION_FAILED",
                        "container GPU differs from its live Slurm allocation",
                    )
                stop_argv = [
                    SCRIPT_ALLOWLIST[script_name],
                    "stop",
                    str(payload["username"]),
                    str(allocation_job_id),
                    allocation_uuid,
                ]
            else:
                stop_argv = [SCRIPT_ALLOWLIST[script_name], str(payload["username"])]
            stopped = run_allowlisted_script(stop_argv, timeout=150)
            if not stopped.get("ok"):
                raise LifecycleValidationError("CONTAINER_STOP_FAILED", "container stop failed")
        if isinstance(allocation_job_id, int):
            failed_step = "GPU_ALLOCATION_CANCEL"
            _cancel_gpu_development_allocation(payload, allocation_job_id)
            allocation_job_id = None
            allocation_uuid = None
            gpu_allocation_revoked = True
        lifecycle_payload.update({"gpu_allocation_job_id": None, "gpu_allocation_uuid": None})
        failed_step = "CONTAINER_STOP_POSTCONDITION"
        _managed_container_security(lifecycle_payload, require_running=False)

        failed_step = "SLURM_JOB_DISCOVERY"
        jobs = _active_user_slurm_jobs(str(payload["username"]))
        pending_ids = [job_id for job_id, state in jobs if state.startswith("PEND")]
        running_ids = [job_id for job_id, state in jobs if not state.startswith("PEND")]
        if pending_ids:
            failed_step = "PENDING_JOB_CANCEL"
            cancelled = run_fixed("scancel", [*(str(item) for item in pending_ids)], timeout=30)
            if not cancelled.get("ok"):
                raise LifecycleValidationError(
                    "PENDING_JOB_CANCEL_FAILED", "pending jobs could not be cancelled"
                )
            cancelled_pending_ids = pending_ids
        if running_ids:
            failed_step = "RUNNING_JOB_TERM"
            signalled = run_fixed(
                "scancel",
                ["--signal=TERM", "--full", *(str(item) for item in running_ids)],
                timeout=30,
            )
            if not signalled.get("ok"):
                raise LifecycleValidationError(
                    "RUNNING_JOB_SIGNAL_FAILED", "running jobs could not be signalled"
                )
            time.sleep(2)
            failed_step = "RUNNING_JOB_CANCEL"
            cancelled = run_fixed("scancel", [*(str(item) for item in running_ids)], timeout=30)
            if not cancelled.get("ok"):
                raise LifecycleValidationError(
                    "RUNNING_JOB_CANCEL_FAILED", "running jobs could not be cancelled"
                )
            cancelled_running_ids = running_ids
        failed_step = "RECYCLE_POSTCONDITION"
        _managed_container_security(lifecycle_payload, require_running=False)
        if (
            active.exists()
            or not suspended.exists()
            or _active_user_slurm_jobs(str(payload["username"]))
        ):
            raise LifecycleValidationError(
                "RECYCLE_POSTCONDITION_FAILED", "lease recycle postconditions are incomplete"
            )
        failed_step = "LIFECYCLE_STATE_RECYCLE"
        _mark_recycled_lifecycle(payload, fingerprints, restore_rollback=restore_rollback)
        return {
            "status": "SUCCEEDED",
            "handler": request.operation_type,
            "request_id": request.request_id,
            "lease_id": result_lease_id,
            "cancelled_pending_job_ids": cancelled_pending_ids,
            "cancelled_running_job_ids": cancelled_running_ids,
            "container_state": "STOPPED",
            "container_gpu": payload["expected_gpu"],
            "gpu_allocation_job_id": None,
            "gpu_allocation_uuid": None,
            "container_key_state": "SUSPENDED_BY_RECYCLE",
            "container_key_fingerprints": fingerprints,
            "new_access": "DENIED",
            "host_access": "DISABLED",
            "data_preserved": True,
            "container_definition_preserved": True,
            "gpu_allocation_revoked": True,
            "auto_permanent_delete": False,
        }
    except LifecycleValidationError as exc:
        return {
            "status": "ERROR",
            "handler": request.operation_type,
            "request_id": request.request_id,
            "lease_id": result_lease_id,
            "error": {"code": exc.code, "message": str(exc)},
            "first_failed_step": failed_step,
            "cleanup_retryable": key_suspended,
            "container_key_state": ("SUSPENDED_BY_RECYCLE" if key_suspended else "NOT_VERIFIED"),
            "container_key_fingerprints": fingerprints,
            "new_access": "DENIED" if key_suspended else "NOT_VERIFIED",
            "cancelled_pending_job_ids": cancelled_pending_ids,
            "cancelled_running_job_ids": cancelled_running_ids,
            "data_preserved": True,
            "container_definition_preserved": True,
            "gpu_allocation_revoked": gpu_allocation_revoked,
            "gpu_allocation_job_id": (
                None if gpu_allocation_revoked else payload.get("gpu_allocation_job_id")
            ),
            "gpu_allocation_uuid": (
                None if gpu_allocation_revoked else payload.get("gpu_allocation_uuid")
            ),
            "auto_permanent_delete": False,
        }


def _copy_root_only(source: Path, destination: Path) -> str:
    content = source.read_bytes()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chown(destination.parent, 0, 0)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, 0, 0)
    finally:
        os.close(descriptor)
    return hashlib.sha256(content).hexdigest()


def _rollback_host_access_revoke(
    *, username: str, host_keys: Path, removed: Path, original_shell: str
) -> None:
    current = pwd.getpwnam(username)
    if current.pw_shell != original_shell:
        restored_shell = run_fixed("usermod", ["-s", original_shell, username], timeout=20)
        if not restored_shell.get("ok"):
            raise OSError("managed host shell rollback failed")
    if removed.exists():
        if host_keys.exists():
            raise OSError("host authorized_keys rollback target is occupied")
        os.replace(removed, host_keys)
    refreshed = pwd.getpwnam(username)
    if refreshed.pw_shell != original_shell or not host_keys.exists():
        raise OSError("host access rollback postcondition failed")


def _execute_host_access_revoke(request: WorkerRequest, payload: dict[str, Any]) -> dict[str, Any]:
    host_keys = MANAGED_HOME_ROOT / str(payload["username"]) / ".ssh/authorized_keys"
    removed = host_keys.with_name(f".authorized_keys.portal4a-{request.request_id}")
    original_shell: str | None = None
    migration_started = False
    try:
        account = _managed_account(payload)
        original_shell = account.pw_shell
        if account.pw_shell not in {"/bin/bash", "/usr/sbin/nologin"}:
            raise LifecycleValidationError(
                "HOST_ACCESS_STATE_REJECTED", "managed host shell is outside migration states"
            )
        management_before = {
            MANAGEMENT_USERNAME: _sshd_effective_config(MANAGEMENT_USERNAME),
            "codexops": _sshd_effective_config("codexops"),
        }
        container_keys = PILOT_DATA_ROOT / str(payload["username"]) / "home/.ssh/authorized_keys"
        container_fingerprints = _installed_key_fingerprints(
            container_keys, int(payload["uid"]), int(payload["gid"])
        )
        if container_fingerprints != [payload["key_fingerprint"]]:
            raise LifecycleValidationError(
                "CONTAINER_KEY_CHANGED", "container public-key fingerprint differs from approval"
            )
        already_revoked = account.pw_shell == "/usr/sbin/nologin" and not host_keys.exists()
        backup_path: Path | None = None
        backup_sha256: str | None = None
        if not already_revoked:
            host_fingerprints = _installed_key_fingerprints(
                host_keys, int(payload["uid"]), int(payload["gid"])
            )
            if host_fingerprints != [payload["key_fingerprint"]]:
                raise LifecycleValidationError(
                    "HOST_KEY_CHANGED", "host public-key fingerprint differs from approval"
                )
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            backup_path = (
                PLATFORM_BACKUP_ROOT
                / f"portal4a-host-access-{stamp}-{request.request_id[:8]}"
                / "authorized_keys"
            )
            backup_sha256 = _copy_root_only(host_keys, backup_path)
            os.replace(host_keys, removed)
            migration_started = True
            changed = run_fixed(
                "usermod", ["-s", "/usr/sbin/nologin", str(payload["username"])], timeout=20
            )
            if not changed.get("ok"):
                raise LifecycleValidationError(
                    "HOST_SHELL_REVOKE_FAILED", "managed host shell could not be disabled"
                )
        refreshed = pwd.getpwnam(str(payload["username"]))
        if refreshed.pw_shell != "/usr/sbin/nologin" or host_keys.exists():
            raise LifecycleValidationError(
                "HOST_ACCESS_POSTCONDITION_FAILED", "host access revocation is incomplete"
            )
        if _installed_key_fingerprints(
            container_keys, int(payload["uid"]), int(payload["gid"])
        ) != [payload["key_fingerprint"]]:
            raise LifecycleValidationError(
                "CONTAINER_KEY_CHANGED", "container public key changed during host revocation"
            )
        for username, before in management_before.items():
            validate_ssh_policy_no_regression(username, before, _sshd_effective_config(username))
        _managed_account(payload)
        if migration_started:
            removed.unlink()
        return {
            "status": "SUCCEEDED",
            "handler": "host_access.revoke_managed_user",
            "request_id": request.request_id,
            "username": payload["username"],
            "shell": "/usr/sbin/nologin",
            "password": "LOCKED",
            "host_authorized_keys": "ABSENT",
            "host_access": "DISABLED_BY_PLATFORM_POLICY",
            "container_authorized_keys": "INSTALLED",
            "container_key_fingerprint": payload["key_fingerprint"],
            "key_scope": "CONTAINER",
            "backup_path": str(backup_path) if backup_path else None,
            "backup_sha256": backup_sha256,
            "idempotent_replay": already_revoked,
            "origin_al_policy": "UNCHANGED",
            "codexops_policy": "UNCHANGED",
        }
    except (LifecycleValidationError, OSError) as exc:
        rollback_status = "NOT_REQUIRED"
        rollback_error: OSError | None = None
        if migration_started and original_shell is not None:
            try:
                _rollback_host_access_revoke(
                    username=str(payload["username"]),
                    host_keys=host_keys,
                    removed=removed,
                    original_shell=original_shell,
                )
                rollback_status = "ROLLED_BACK"
            except OSError as rollback_exc:
                rollback_status = "ROLLBACK_FAILED"
                rollback_error = rollback_exc
        return {
            "status": "ERROR",
            "error": {
                "code": "HOST_ACCESS_ROLLBACK_FAILED"
                if rollback_error is not None
                else getattr(exc, "code", "HOST_ACCESS_REVOKE_FAILED"),
                "message": str(rollback_error or exc),
                "rollback_status": rollback_status,
            },
        }


def dry_run_plan(request: WorkerRequest, payload: dict[str, Any]) -> dict[str, Any]:
    if request.operation_type == "compute.provision.plan":
        return _compute_provision_plan(payload)
    if request.operation_type == "compute.provision.dry_run":
        return _compute_provision_dry_run(payload)
    if request.operation_type == "compute.provision.retry_verify":
        return _compute_retry_verification(payload)
    if request.operation_type == "compute.provision.stage":
        return {
            "status": "ERROR",
            "error": {
                "code": "COMPUTE_STAGE_DRY_RUN_REJECTED",
                "message": "Stage consumes a separately recorded compute.provision.dry_run",
            },
        }
    if request.operation_type in {"compute.activate.self", "compute.activate.self.rollback"}:
        return {
            "status": "ERROR",
            "error": {
                "code": "ACTIVATION_DRY_RUN_REJECTED",
                "message": "owner activation preflight is internal to the one-click operation",
            },
        }
    if request.operation_type == "user.ssh_client_validation.record":
        return _portal3f_client_validation_plan(payload)
    if request.operation_type == "user.pilot.acceptance":
        return _portal3f_pilot_acceptance_plan(payload)
    if request.operation_type == "slurm.production_pilot.start":
        return _portal3g_production_pilot_plan(payload)
    if request.operation_type in {"ssh_key.prepare", "ssh_key.discard"}:
        return {
            "status": "DRY_RUN",
            "handler": request.operation_type,
            "execution_enabled": False,
            "record_id": payload["record_id"],
            "controlled_root": str(SSH_KEY_STAGING_ROOT),
            "authorized_keys_install": "NOT_PERFORMED",
        }
    integrity = script_integrity() if request.operation_type in KNOWN_WRITES else {}
    failed_scripts = sorted(
        name for name, status in integrity.items() if not status.get("integrity_ok", False)
    )
    result: dict[str, Any] = {
        "status": "DRY_RUN",
        "handler": request.operation_type,
        "safe_parameters": payload,
        "expected_backups": ["精确目标配置文件（原子替换前）"],
        "expected_validation": ["重新读取目标状态", "验证 owner/mode/hash", "验证幂等键"],
        "expected_rollback": ["仅恢复本工具创建的精确文件", "保持 Slurm DRAIN", "保留用户数据"],
        "execution_enabled": False,
        "script_integrity": integrity,
    }
    if failed_scripts:
        result["status"] = "ERROR"
        result["error"] = {
            "code": "SCRIPT_INTEGRITY_FAILED",
            "message": "固定管理脚本完整性校验失败",
            "scripts": failed_scripts,
        }
    if request.operation_type == "user.plan":
        return _user_plan(payload["username"])
    if request.operation_type == "user.stage" and set(payload) != {"username"}:
        return _user_stage_dry_run(payload)
    if request.operation_type == "user.activate":
        if failed_scripts:
            return result
        return _user_activate_dry_run(payload)
    if request.operation_type == "user.activate.rollback":
        return {
            "status": "ERROR",
            "error": {
                "code": "ACTIVATE_ROLLBACK_DRY_RUN_REJECTED",
                "message": "internal rollback is available only for a failed real Activate",
            },
        }
    return result


def handle(request: WorkerRequest) -> dict[str, Any]:
    try:
        payload = validate_payload(
            request.operation_type,
            request.payload,
            allow_legacy_stage=(
                request.operation_type == "user.stage" and set(request.payload) == {"username"}
            ),
        )
    except ValueError as exc:
        rejected = {
            "status": "ERROR",
            "error": {"code": getattr(exc, "code", "PAYLOAD_REJECTED"), "message": str(exc)},
        }
        if request.operation_type == "compute.provision.stage":
            rejected.update(
                {
                    "side_effect_classification": "NO_SIDE_EFFECT",
                    "rollback_status": "NOT_REQUIRED",
                    "last_successful_step": "NONE",
                    "first_failed_step": "WORKER_PAYLOAD_CONTRACT",
                    "failed_handler": "compute.provision.stage",
                    "retained_resources": [],
                }
            )
        return rejected
    if request.operation_type.startswith("self.") and request.requested_by != payload.get(
        "username"
    ):
        return {
            "status": "ERROR",
            "error": {
                "code": "RESOURCE_OWNERSHIP_REJECTED",
                "message": "user-level operation actor does not own the target resource",
            },
        }
    if request.operation_type in KNOWN_WRITES:
        if not request.dry_run:
            if request.operation_type == "compute.activate.self":
                return _execute_self_compute_activation(request, payload)
            if request.operation_type == "compute.activate.self.rollback":
                return _execute_self_compute_activation_rollback(request, payload)
            if request.operation_type == "compute.provision.stage":
                return _execute_compute_provision_stage(request, payload)
            if request.operation_type == "self.job.submit":
                return _execute_self_job_submit(request, payload)
            if request.operation_type == "self.job.cancel":
                return _execute_self_job_cancel(request, payload)
            if (
                request.operation_type in {"container.start", "container.stop", "container.restart"}
                and "uid" in payload
            ):
                return _execute_managed_container_lifecycle(request, payload)
            if request.operation_type in {"resource.restore", "self.resource.restore"}:
                return _execute_resource_restore(request, payload)
            if request.operation_type in {
                "lease.expire",
                "resource.recycle",
                "resource.restore.rollback",
            }:
                return _execute_resource_recycle(request, payload)
            if request.operation_type == "host_access.revoke_managed_user":
                return _execute_host_access_revoke(request, payload)
            if request.operation_type == "user.stage":
                return _execute_origin_pilot_stage(request, payload)
            if request.operation_type == "user.activate":
                return _execute_origin_pilot_activate(request, payload)
            if request.operation_type == "user.activate.rollback":
                return _execute_origin_pilot_activate_rollback(request, payload)
            if request.operation_type == "user.ssh_client_validation.record":
                return _execute_portal3f_client_validation(request, payload)
            if request.operation_type == "user.pilot.acceptance":
                return _execute_portal3f_pilot_acceptance(request, payload)
            if request.operation_type == "slurm.production_pilot.start":
                return _execute_portal3g_production_pilot(request, payload)
            if request.operation_type == "slurm.drain":
                return _execute_portal3g_safety_drain(request, payload)
            if request.operation_type == "ssh_key.prepare":
                return _prepare_ssh_key_record(request, payload)
            if request.operation_type == "ssh_key.discard":
                return _discard_ssh_key_record(request, payload)
            if request.operation_type == "container.start" and set(payload) != {"name"}:
                return _execute_managed_container_start(request, payload)
            return {
                "status": "ERROR",
                "error": {
                    "code": "WRITE_EXECUTION_DISABLED",
                    "message": "This write operation has no approved controlled execution path",
                },
            }
        try:
            return dry_run_plan(request, payload)
        except LifecycleValidationError as exc:
            return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}
    try:
        if request.operation_type == "compute.provision.retry_verify":
            if not request.dry_run:
                return {
                    "status": "ERROR",
                    "error": {
                        "code": "COMPUTE_RETRY_VERIFY_EXECUTION_REJECTED",
                        "message": "retry verification is read-only",
                    },
                }
            return _compute_retry_verification(payload)
        if request.operation_type == "platform.health.read":
            return platform_health()
        if request.operation_type == "gpu.list":
            return gpu_list()
        if request.operation_type == "gpu.health.read":
            return gpu_health()
        if request.operation_type == "slurm.node.read":
            return slurm_node()
        if request.operation_type == "slurm.jobs.read":
            return slurm_jobs()
        if request.operation_type == "slurm.accounts.read":
            return slurm_accounts()
        if request.operation_type == "slurm.history.read":
            return slurm_history()
        if request.operation_type == "containers.list":
            return containers_list()
        if request.operation_type == "containers.inspect":
            return containers_inspect(payload)
        if request.operation_type == "storage.summary.read":
            return storage_summary()
        if request.operation_type == "quotas.list":
            return quotas_list()
        if request.operation_type == "systemd.failed.read":
            return systemd_failed()
        if request.operation_type == "monitoring.alerts.read":
            return monitoring_alerts()
        if request.operation_type == "monitoring.summary.read":
            return monitoring_summary()
        if request.operation_type == "registry.status.read":
            return registry_status()
        if request.operation_type == "images.list":
            return images_list()
        if request.operation_type == "gpu_isolation.status.read":
            return gpu_isolation_status()
        if request.operation_type == "ssh.policy.read":
            return ssh_policy_status()
        if request.operation_type == "self.job.logs.read":
            return _self_job_logs(payload)
        if request.operation_type == "self.job.status.read":
            return _self_job_status(payload)
        if request.operation_type == "self.storage.read":
            return _self_storage(payload)
        if request.operation_type == "self.workspace.check":
            return _self_workspace_check(payload)
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "error": {"code": "ADAPTER_EXCEPTION", "message": str(exc)[:255]},
        }
    return {"status": "ERROR", "error": {"code": "UNKNOWN_OPERATION", "message": "未知操作"}}
