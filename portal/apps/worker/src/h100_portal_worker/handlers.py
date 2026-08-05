import csv
import hashlib
import json
import os
import re
import stat
import subprocess
import urllib.error
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
    parsed = _json_command(
        "sacctmgr", ["-n", "-P", "show", "account", "format=Account,Description"], timeout=20
    )
    if parsed["status"] == "OK":
        return parsed
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
    return {"status": "OK", "accounts": accounts}


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
                        "Labels",
                    )
                }
            )
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
    return {
        "status": "OK",
        "container": {
            "name": item.get("Name"),
            "id": item.get("Id"),
            "created": item.get("Created"),
            "state": item.get("State"),
            "image": config.get("Image"),
            "labels": config.get("Labels"),
            "privileged": host_config.get("Privileged"),
            "network_mode": host_config.get("NetworkMode"),
            "pid_mode": host_config.get("PidMode"),
            "ipc_mode": host_config.get("IpcMode"),
            "mounts": [
                {key: mount.get(key) for key in ("Type", "Source", "Destination", "RW")}
                for mount in item.get("Mounts", [])
                if isinstance(mount, dict)
            ],
            "device_requests": host_config.get("DeviceRequests"),
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
    return {"status": "OK" if mounts else "UNKNOWN", "mounts": mounts, "volume_groups": vgs}


def quotas_list() -> dict[str, Any]:
    result = run_fixed("xfs_quota", ["-x", "-c", "report -p -b -n"], timeout=20)
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    return {"status": "OK", "report": result["stdout"][:MAX_OUTPUT]}


def systemd_failed() -> dict[str, Any]:
    result = run_fixed("systemctl", ["--failed", "--no-legend", "--plain"], timeout=20)
    if not result.get("ok"):
        return {"status": "UNKNOWN", "error": result}
    failed = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
    return {"status": "OK", "failed_units": failed, "count": len(failed)}


def monitoring_alerts() -> dict[str, Any]:
    url = "http://127.0.0.1:9090/api/v1/alerts"
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            payload = json.loads(response.read(MAX_OUTPUT))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {
            "status": "UNKNOWN",
            "error": {"error_code": "PROMETHEUS_UNAVAILABLE", "detail": str(exc)[:128]},
        }
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return {"status": "UNKNOWN", "error": {"error_code": "PROMETHEUS_SCHEMA_ERROR"}}
    alerts = payload.get("data", {}).get("alerts", [])
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


def registry_status() -> dict[str, Any]:
    return {
        "status": "OK",
        "registries": [
            {"name": "Local", "status": "AVAILABLE", "detail": "本地受控镜像源"},
            {"name": "NGC", "status": "AVAILABLE", "detail": "按批准凭据使用"},
            {"name": "GHCR", "status": "AVAILABLE", "detail": "按批准凭据使用"},
            {"name": "Quay", "status": "AVAILABLE", "detail": "按批准凭据使用"},
            {
                "name": "Docker Hub",
                "status": "DEFERRED",
                "detail": "需要替代 Registry；不作为运行时依赖",
            },
        ],
    }


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
    diag = run_fixed("dcgmi", ["diag", "-r", "1"], timeout=90)
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
    status = (
        "OK" if discovery.get("ok") and diag.get("ok") and kernel_status == "CLEAR" else "PARTIAL"
    )
    return {
        "status": status,
        "discovery": {"ok": discovery.get("ok"), "output": discovery.get("stdout", "")[-4096:]},
        "diag": {"ok": diag.get("ok"), "output": diag.get("stdout", "")[-8192:]},
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
        if request.operation_type == "registry.status.read":
            return registry_status()
        if request.operation_type == "gpu_isolation.status.read":
            return gpu_isolation_status()
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "error": {"code": "ADAPTER_EXCEPTION", "message": str(exc)[:255]},
        }
    return {"status": "ERROR", "error": {"code": "UNKNOWN_OPERATION", "message": "未知操作"}}
