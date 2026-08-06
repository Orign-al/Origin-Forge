import csv
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from h100_portal_worker.schemas import (
    KNOWN_WRITES,
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
    "hostname": "/usr/bin/hostname",
    "ss": "/usr/bin/ss",
    "ssh-keygen": "/usr/bin/ssh-keygen",
    "squeue-fallback": "/usr/bin/squeue",
}
SCRIPT_ALLOWLIST = {
    "h100-user-create": "/usr/local/sbin/h100-user-create",
    "h100-user-gpu-isolation": "/usr/local/sbin/h100-user-gpu-isolation",
    "h100-container-create": "/usr/local/sbin/h100-container-create",
    "h100-container-start": "/usr/local/sbin/h100-container-start",
    "h100-container-stop": "/usr/local/sbin/h100-container-stop",
    "h100-container-rebuild": "/usr/local/sbin/h100-container-rebuild",
    "h100-container-delete": "/usr/local/sbin/h100-container-delete",
    "h100-container-status": "/usr/local/sbin/h100-container-status",
    "h100-quota-show": "/usr/local/sbin/h100-quota-show",
    "h100-gpu-bypass-guard": "/usr/local/sbin/h100-gpu-bypass-guard",
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
GPU_DROPIN_NAME = "50-h100-gpu-isolation.conf"
GPU_DROPIN_CONTENT = "[Slice]\nDevicePolicy=closed\n"
GPU_REGISTRY = Path("/etc/h100-platform/gpu-isolated-users")
PILOT_STATE_ROOT = Path("/etc/h100-platform/users")
PILOT_DATA_ROOT = Path("/srv/gpu-platform/users")
PILOT_COMPOSE_ROOT = Path("/srv/gpu-platform/platform/config/dev-containers")
PILOT_SCAN_ROOTS = (
    Path("/home"),
    Path("/srv/gpu-platform"),
    Path("/var/lib/slurm"),
    Path("/var/spool/slurmctld"),
    Path("/var/spool/slurmd"),
)
SSH_KEY_STAGING_ROOT = Path("/var/lib/h100-portal/ssh-key-staging")
MAX_SSH_KEY_FILE_BYTES = 16 * 1024
MAX_APPROVED_SSH_KEYS = 5


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
    labels_value = config.get("Labels")
    labels: dict[str, Any] = labels_value if isinstance(labels_value, dict) else {}
    safe_labels = {
        key: str(labels[key])[:255]
        for key in ("h100.dev.user", "h100.base.digest", "org.opencontainers.image.version")
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
            "privileged": host_config.get("Privileged"),
            "network_mode": host_config.get("NetworkMode"),
            "pid_mode": host_config.get("PidMode"),
            "ipc_mode": host_config.get("IpcMode"),
            "mounts": mounts,
            "device_requests": device_requests,
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
        "grafana": grafana | {"url": "http://10.10.10.2:3000"},
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
    for name, path_string in SCRIPT_ALLOWLIST.items():
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


def _read_approved_ssh_key_record(record_id: str) -> dict[str, Any]:
    """Read one UUID-named, root-controlled key without following links."""
    try:
        root_stat = SSH_KEY_STAGING_ROOT.lstat()
    except OSError as exc:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH key staging directory is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or stat.S_ISLNK(root_stat.st_mode)
        or root_stat.st_uid != 0
        or root_stat.st_gid != 0
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH key staging directory metadata is invalid"
        )
    path = SSH_KEY_STAGING_ROOT / f"{record_id}.pub"
    try:
        before = path.lstat()
    except OSError as exc:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key record is unavailable"
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
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key file metadata is invalid"
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
                    "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key file changed during open"
                )
            content = os.read(descriptor, MAX_SSH_KEY_FILE_BYTES + 1)
            if os.read(descriptor, 1):
                content += b"x"
        finally:
            os.close(descriptor)
    except LifecycleValidationError:
        raise
    except OSError as exc:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key file could not be read safely"
        ) from exc
    if not 0 < len(content) <= MAX_SSH_KEY_FILE_BYTES:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key file has an invalid size"
        )
    try:
        decoded = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "approved SSH key is not valid UTF-8"
        ) from exc
    upper = decoded.upper()
    if "PRIVATE KEY" in upper or "-----BEGIN" in upper or "-----END" in upper:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "private-key material is forbidden"
        )
    lines = [line for line in decoded.splitlines() if line.strip()]
    if len(lines) != 1 or lines[0] != lines[0].strip() or "\r" in lines[0]:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "one canonical public key is required per record"
        )
    parts = lines[0].split(maxsplit=2)
    if len(parts) < 2 or parts[0] not in {"ssh-ed25519", "sk-ssh-ed25519@openssh.com"}:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH key type is not approved"
        )
    if re.fullmatch(r"[A-Za-z0-9+/]+={0,3}", parts[1]) is None:
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH public-key payload is malformed"
        )
    if len(parts) == 3 and any(ord(character) < 32 for character in parts[2]):
        raise LifecycleValidationError(
            "PUBLIC_KEY_VALIDATION_FAILED", "SSH key comment contains control characters"
        )
    fingerprint = _ssh_key_fingerprint(lines[0])
    return {
        "record_id": record_id,
        "key_type": parts[0],
        "fingerprint": fingerprint,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def validate_approved_ssh_key_records(record_ids: list[str]) -> list[dict[str, Any]]:
    if not 1 <= len(record_ids) <= MAX_APPROVED_SSH_KEYS:
        raise LifecycleValidationError(
            "PUBLIC_KEY_REQUIRED_FOR_ACTIVATION", "approved SSH key records are required"
        )
    results = [_read_approved_ssh_key_record(record_id) for record_id in record_ids]
    fingerprints = [str(item["fingerprint"]) for item in results]
    if len(set(fingerprints)) != len(fingerprints):
        raise LifecycleValidationError("PUBLIC_KEY_DUPLICATE", "duplicate SSH key fingerprint")
    return results


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


def build_user_activate_argv(username: str, controlled_key_file: Path) -> list[str]:
    """Build Activate argv only for a Worker-owned UUID staging path."""
    if (
        controlled_key_file.parent != SSH_KEY_STAGING_ROOT
        or re.fullmatch(r"[0-9A-Fa-f-]{36}\.pub", controlled_key_file.name) is None
    ):
        raise LifecycleValidationError(
            "ARBITRARY_PATH_REJECTED", "Activate key file is outside the controlled staging root"
        )
    return [
        SCRIPT_ALLOWLIST["h100-user-create"],
        "--activate",
        username,
        "--public-key-file",
        str(controlled_key_file),
        "--confirm-activate",
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


def _candidate_uid_gid() -> tuple[int | None, list[dict[str, str]], dict[str, str]]:
    used_uids, used_gids = _used_identity_numbers()
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


def _used_project_ids() -> set[int]:
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
    return used


def _candidate_project_id() -> tuple[int | None, dict[str, str]]:
    used = _used_project_ids()
    for value in range(PROJECT_ID_FIRST, PROJECT_ID_MAX + 1):
        if value not in used:
            return value, {
                "range": f"{PROJECT_ID_MIN}-{PROJECT_ID_MAX}",
                "reservation": "PROPOSED — NOT RESERVED",
                "source": "/etc/projects,/etc/projid,platform state",
            }
    return None, {
        "range": f"{PROJECT_ID_MIN}-{PROJECT_ID_MAX}",
        "reservation": "NOT_AVAILABLE",
        "source": "/etc/projects,/etc/projid,platform state",
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


def _candidate_ssh_port() -> tuple[int | None, dict[str, str]]:
    used, readable = _used_ssh_ports()
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
                "bind_address": MANAGEMENT_IP,
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
                "detail": f"候选端口={ssh_port}，未来绑定 {MANAGEMENT_IP}，只提出不开放"
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

    data_path = f"/srv/gpu-platform/users/{PILOT_USERNAME}"
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
            "network_bind": MANAGEMENT_IP,
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


def _user_activate_dry_run(payload: dict[str, Any]) -> dict[str, Any]:
    # Key files are validated before consulting or mutating lifecycle state.
    key_records = validate_approved_ssh_key_records(payload["approved_ssh_key_record_ids"])
    staged = _staged_origin_pilot_state()
    return {
        "status": "DRY_RUN",
        "handler": "user.activate",
        "activate_status": "READY",
        "execution_enabled": False,
        "expected_state": "STAGED",
        "managed_user_id": payload["managed_user_id"],
        "approved_ssh_keys": key_records,
        "validated_username": staged["USERNAME"],
        "validated_uid": staged.get("UID"),
        "public_key_validation": "PASSED",
        "authorized_keys_install": "DEFERRED_UNTIL_REAL_ACTIVATE",
        "expected_rollback": [
            "恢复 /usr/sbin/nologin",
            "禁用或回滚 authorized_keys",
            "停止用户容器",
            "保留 GPU policy、quota 与用户数据",
            "保持 Slurm DRAIN",
        ],
    }


def dry_run_plan(request: WorkerRequest, payload: dict[str, Any]) -> dict[str, Any]:
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
        return {
            "status": "ERROR",
            "error": {"code": getattr(exc, "code", "PAYLOAD_REJECTED"), "message": str(exc)},
        }
    if request.operation_type in KNOWN_WRITES:
        if not request.dry_run:
            return {
                "status": "ERROR",
                "error": {
                    "code": "WRITE_EXECUTION_DISABLED",
                    "message": "Portal-3B-R 生命周期 Gate 仅允许 dry-run",
                },
            }
        try:
            return dry_run_plan(request, payload)
        except LifecycleValidationError as exc:
            return {"status": "ERROR", "error": {"code": exc.code, "message": str(exc)}}
    try:
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
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "error": {"code": "ADAPTER_EXCEPTION", "message": str(exc)[:255]},
        }
    return {"status": "ERROR", "error": {"code": "UNKNOWN_OPERATION", "message": "未知操作"}}
