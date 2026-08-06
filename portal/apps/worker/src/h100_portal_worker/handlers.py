import csv
import hashlib
import json
import os
import re
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from h100_portal_worker.schemas import KNOWN_WRITES, WorkerRequest, validate_payload

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
    "hostname": "/usr/bin/hostname",
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
    return result


def handle(request: WorkerRequest) -> dict[str, Any]:
    try:
        payload = validate_payload(request.operation_type, request.payload)
    except ValueError as exc:
        return {"status": "ERROR", "error": {"code": "PAYLOAD_REJECTED", "message": str(exc)}}
    if request.operation_type in KNOWN_WRITES:
        if not request.dry_run:
            return {
                "status": "ERROR",
                "error": {
                    "code": "WRITE_EXECUTION_DISABLED",
                    "message": "Portal-0/1 仅允许 dry-run",
                },
            }
        return dry_run_plan(request, payload)
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
