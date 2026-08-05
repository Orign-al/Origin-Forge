#!/usr/bin/env python3
"""Map NVIDIA UUIDs to Linux minors and probe exact device open access.

The probe deliberately never assumes that logical GPU index 0 is
/dev/nvidia0.  NVIDIA procfs is the source for UUID-to-minor correlation.
"""

from __future__ import annotations

import argparse
import csv
import errno
import glob
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


DENIED_ERRNOS = {errno.EACCES, errno.EPERM}
OPEN_MODES = {
    "O_RDONLY": os.O_RDONLY,
    "O_WRONLY": os.O_WRONLY,
    "O_RDWR": os.O_RDWR,
}


def clean(value: str | None) -> str:
    return (value or "").strip()


def parse_proc_gpu_map() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    pattern = "/proc/driver/nvidia/gpus/*/information"
    for info_path in sorted(glob.glob(pattern)):
        fields: dict[str, str] = {}
        with open(info_path, encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                if ":" not in raw_line:
                    continue
                key, value = raw_line.split(":", 1)
                fields[key.strip()] = value.strip()

        uuid = clean(fields.get("GPU UUID"))
        pci_bus = clean(fields.get("Bus Location"))
        minor_text = clean(fields.get("Device Minor"))
        if not uuid or not minor_text:
            continue
        try:
            minor = int(minor_text, 10)
        except ValueError:
            continue

        device_path = f"/dev/nvidia{minor}"
        major = None
        device_minor = None
        mode = None
        try:
            device_stat = os.stat(device_path)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISCHR(device_stat.st_mode):
                major = os.major(device_stat.st_rdev)
                device_minor = os.minor(device_stat.st_rdev)
                mode = oct(stat.S_IMODE(device_stat.st_mode))

        records.append(
            {
                "uuid": uuid,
                "pci_bus_id": pci_bus,
                "linux_minor": minor,
                "device_path": device_path,
                "major": major,
                "minor": device_minor,
                "mode": mode,
                "name": clean(fields.get("Model")),
                "proc_information": info_path,
            }
        )
    return records


def query_visible_gpus() -> tuple[int, list[dict[str, Any]], str]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,uuid,pci.bus_id,name,memory.total",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, [], str(exc)

    visible: list[dict[str, Any]] = []
    if completed.returncode == 0:
        for line in completed.stdout.splitlines():
            columns = [column.strip() for column in line.split(",", 4)]
            if len(columns) != 5:
                continue
            index_text, uuid, pci_bus, name, memory_total = columns
            try:
                index = int(index_text, 10)
            except ValueError:
                index = None
            visible.append(
                {
                    "nvml_index": index,
                    "uuid": uuid,
                    "pci_bus_id": pci_bus,
                    "name": name,
                    "memory_total_mb": memory_total,
                }
            )

    stderr = completed.stderr.strip()
    return completed.returncode, visible, stderr


def read_cgroup() -> str:
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()
    except OSError as exc:
        return f"ERROR errno={exc.errno} {exc.strerror}"


def per_gpu_paths() -> list[str]:
    paths = []
    for path in glob.glob("/dev/nvidia[0-9]*"):
        if re.fullmatch(r"/dev/nvidia[0-9]+", path):
            paths.append(path)
    return sorted(paths, key=lambda item: int(item.removeprefix("/dev/nvidia")))


def open_one(path: str, flags: int) -> dict[str, Any]:
    actual_flags = flags | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, actual_flags)
    except OSError as exc:
        result = "DENIED" if exc.errno in DENIED_ERRNOS else "ERROR"
        return {
            "result": result,
            "errno": exc.errno,
            "error": exc.strerror,
        }
    else:
        os.close(descriptor)
        return {"result": "OPENED", "errno": 0, "error": "OK"}


def write_host_maps(
    csv_path: str | None,
    markdown_path: str | None,
    proc_records: list[dict[str, Any]],
    visible_records: list[dict[str, Any]],
) -> None:
    by_uuid = {record["uuid"]: record for record in proc_records}
    rows: list[dict[str, Any]] = []
    for visible in visible_records:
        mapped = by_uuid.get(visible["uuid"], {})
        rows.append(
            {
                "nvml_index": visible.get("nvml_index"),
                "uuid": visible.get("uuid"),
                "pci_bus_id": visible.get("pci_bus_id"),
                "linux_minor": mapped.get("linux_minor"),
                "device_path": mapped.get("device_path"),
                "major": mapped.get("major"),
                "minor": mapped.get("minor"),
                "name": visible.get("name") or mapped.get("name"),
                "memory_total_mb": visible.get("memory_total_mb"),
            }
        )

    fieldnames = [
        "nvml_index",
        "uuid",
        "pci_bus_id",
        "linux_minor",
        "device_path",
        "major",
        "minor",
        "name",
        "memory_total_mb",
    ]
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=fieldnames,
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)

    if markdown_path:
        with open(markdown_path, "w", encoding="utf-8") as handle:
            handle.write("# Host GPU device map\n\n")
            handle.write(
                "Minor numbers are correlated through "
                "`/proc/driver/nvidia/gpus/*/information`; this driver does "
                "not expose `minor_number` through `nvidia-smi --query-gpu`.\n\n"
            )
            handle.write(
                "| NVML index | UUID | PCI Bus ID | Linux minor | Device path | "
                "Major | Minor | Name | Memory MiB |\n"
            )
            handle.write("|---:|---|---|---:|---|---:|---:|---|---:|\n")
            for row in rows:
                handle.write(
                    "| {nvml_index} | `{uuid}` | `{pci_bus_id}` | "
                    "{linux_minor} | `{device_path}` | {major} | {minor} | "
                    "{name} | {memory_total_mb} |\n".format(**row)
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-allocated-count", type=int)
    parser.add_argument("--assert-slurm-isolation", action="store_true")
    parser.add_argument("--assert-out-of-job-denied", action="store_true")
    parser.add_argument("--host-map-csv")
    parser.add_argument("--host-map-md")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    environment_names = [
        "SLURM_JOB_ID",
        "SLURM_JOB_GPUS",
        "SLURM_STEP_GPUS",
        "CUDA_VISIBLE_DEVICES",
        "CUDA_DEVICE_ORDER",
        "NVIDIA_VISIBLE_DEVICES",
    ]
    print(f"PID={os.getpid()}")
    print(f"UID={os.getuid()} GID={os.getgid()}")
    print("CGROUP_BEGIN")
    print(read_cgroup())
    print("CGROUP_END")
    for name in environment_names:
        print(f"ENV {name}={os.environ.get(name, '')}")

    proc_records = parse_proc_gpu_map()
    smi_rc, visible_records, smi_stderr = query_visible_gpus()
    print(f"NVIDIA_SMI_RC={smi_rc}")
    if smi_stderr:
        print(f"NVIDIA_SMI_STDERR={smi_stderr}")

    proc_by_uuid = {record["uuid"]: record for record in proc_records}
    for record in proc_records:
        print(
            "HOST_GPU "
            f"uuid={record['uuid']} pci_bus_id={record['pci_bus_id']} "
            f"linux_minor={record['linux_minor']} "
            f"device_path={record['device_path']} major={record['major']} "
            f"minor={record['minor']}"
        )

    visible_uuids: set[str] = set()
    for visible in visible_records:
        visible_uuids.add(visible["uuid"])
        mapped = proc_by_uuid.get(visible["uuid"], {})
        print(
            "VISIBLE_GPU "
            f"nvml_index={visible.get('nvml_index')} uuid={visible['uuid']} "
            f"pci_bus_id={visible['pci_bus_id']} "
            f"linux_minor={mapped.get('linux_minor')} "
            f"device_path={mapped.get('device_path')}"
        )

    write_host_maps(
        args.host_map_csv,
        args.host_map_md,
        proc_records,
        visible_records,
    )

    in_slurm_job = bool(os.environ.get("SLURM_JOB_ID"))
    allocated_minors = {
        record["linux_minor"]
        for record in proc_records
        if in_slurm_job and record["uuid"] in visible_uuids
    }
    print(
        "ALLOCATED_IDENTITY "
        f"in_slurm_job={int(in_slurm_job)} "
        f"visible_uuid_count={len(visible_uuids)} "
        f"allocated_minors={','.join(str(item) for item in sorted(allocated_minors))}"
    )

    test_paths: list[tuple[str, str]] = []
    for path in per_gpu_paths():
        minor = int(path.removeprefix("/dev/nvidia"))
        role = "ALLOCATED" if minor in allocated_minors else "UNALLOCATED"
        test_paths.append((path, role))

    for path in [
        "/dev/nvidiactl",
        "/dev/nvidia-uvm",
        "/dev/nvidia-uvm-tools",
        "/dev/nvidia-modeset",
    ]:
        if os.path.exists(path):
            test_paths.append((path, "SHARED"))
    for path in sorted(glob.glob("/dev/nvidia-caps/*")):
        if os.path.exists(path):
            test_paths.append((path, "SHARED_CAPS"))

    results: dict[tuple[str, str], dict[str, Any]] = {}
    for path, role in test_paths:
        for mode_name, flags in OPEN_MODES.items():
            result = open_one(path, flags)
            results[(path, mode_name)] = result
            print(
                "DEVICE_OPEN "
                f"path={path} role={role} flags={mode_name} "
                f"result={result['result']} errno={result['errno']} "
                f"error={result['error']}"
            )

    failures: list[str] = []
    if args.expect_allocated_count is not None:
        if len(allocated_minors) != args.expect_allocated_count:
            failures.append(
                "allocated minor count "
                f"{len(allocated_minors)} != {args.expect_allocated_count}"
            )

    if args.assert_slurm_isolation:
        if not in_slurm_job:
            failures.append("SLURM_JOB_ID is absent")
        for path in per_gpu_paths():
            minor = int(path.removeprefix("/dev/nvidia"))
            result = results[(path, "O_RDWR")]
            if minor in allocated_minors:
                if result["result"] != "OPENED":
                    failures.append(f"allocated {path} O_RDWR was not opened")
            elif result["result"] != "DENIED":
                failures.append(f"unallocated {path} O_RDWR was not denied")

    if args.assert_out_of_job_denied:
        if in_slurm_job:
            failures.append("unexpected SLURM_JOB_ID for out-of-job assertion")
        for path in per_gpu_paths():
            result = results[(path, "O_RDWR")]
            if result["result"] != "DENIED":
                failures.append(f"out-of-job {path} O_RDWR was not denied")

    if failures:
        for failure in failures:
            print(f"ASSERTION_FAILURE={failure}")
        print("GPU_OPEN_PROBE_RESULT=FAILED")
        return 1

    print("GPU_OPEN_PROBE_RESULT=PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
