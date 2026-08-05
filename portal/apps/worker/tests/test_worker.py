import json
import subprocess
import uuid

import pytest
from h100_portal_worker import handlers
from h100_portal_worker.handlers import handle, run_fixed
from h100_portal_worker.protocol import ProtocolError, decode_frame, encode_frame
from h100_portal_worker.schemas import WorkerRequest
from pydantic import ValidationError


def request(operation: str, payload: dict | None = None, dry_run: bool = False) -> WorkerRequest:  # type: ignore[type-arg]
    return WorkerRequest(
        protocol_version=1,
        request_id=str(uuid.uuid4()),
        operation_type=operation,
        payload=payload or {},
        requested_by="origin-al",
        approved_by=None,
        idempotency_key="worker-test-0001",
        dry_run=dry_run,
    )


def test_protocol_round_trip_and_size_limit() -> None:
    framed = encode_frame({"status": "OK", "value": 1})
    assert decode_frame(framed)["value"] == 1
    with pytest.raises(ProtocolError):
        encode_frame({"payload": "x" * (70 * 1024)})


def test_worker_rejects_unknown_operation_and_extra_command() -> None:
    with pytest.raises(ValidationError):
        request("command.execute", {"command": "id"})
    with pytest.raises(ValidationError):
        WorkerRequest.model_validate(
            {
                "protocol_version": 1,
                "request_id": str(uuid.uuid4()),
                "operation_type": "gpu.list",
                "payload": {},
                "requested_by": "origin-al",
                "idempotency_key": "worker-test-0002",
                "dry_run": False,
                "command": "id",
            }
        )


def test_worker_rejects_arbitrary_path_and_non_dry_write() -> None:
    rejected = handle(request("containers.inspect", {"name": "../../etc/shadow"}))
    assert rejected["error"]["code"] == "PAYLOAD_REJECTED"
    write = handle(request("user.stage", {"username": "example-user"}, dry_run=False))
    assert write["error"]["code"] == "WRITE_EXECUTION_DISABLED"


def test_dry_run_returns_plan_not_command(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {"h100-user-create": {"hash_matches": True, "integrity_ok": True}},
    )
    result = handle(request("user.stage", {"username": "example-user"}, dry_run=True))
    assert result["status"] == "DRY_RUN"
    assert result["handler"] == "user.stage"
    assert result["execution_enabled"] is False
    assert "command" not in json.dumps(result).casefold()


def test_dry_run_fails_closed_when_script_integrity_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {"h100-user-create": {"hash_configured": False, "integrity_ok": False}},
    )
    result = handle(request("user.stage", {"username": "example-user"}, dry_run=True))
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "SCRIPT_INTEGRITY_FAILED"
    assert result["error"]["scripts"] == ["h100-user-create"]


def test_fixed_command_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def timeout(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd=["fixed"], timeout=1)

    monkeypatch.setattr(subprocess, "run", timeout)
    result = run_fixed("df", ["/"])
    assert result["error_code"] == "COMMAND_TIMEOUT"


def test_gpu_uuid_minor_mapping_is_joined_by_physical_identity(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    output = (
        "0, GPU-c837, 00000000:21:00.0, NVIDIA H100 PCIe, 81559, 0, 0, 31, 69, 0, Disabled, 5, 16\n"
    )
    monkeypatch.setattr(
        handlers,
        "run_fixed",
        lambda *_args, **_kwargs: {"ok": True, "stdout": output, "stderr": "", "exit_code": 0},
    )
    monkeypatch.setattr(
        handlers,
        "_driver_gpu_minor_map",
        lambda: {"0000:21:00.0": {"minor_number": "1", "uuid": "GPU-c837"}},
    )
    result = handlers.gpu_list()
    assert result["status"] == "OK"
    assert result["gpus"][0]["index"] == "0"
    assert result["gpus"][0]["minor_number"] == "1"
    assert result["gpus"][0]["device_path"] == "/dev/nvidia1"
    assert result["gpus"][0]["minor_source"] == "nvidia-driver-procfs"


def test_gpu_uuid_minor_mapping_mismatch_is_unknown(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    output = (
        "0, GPU-c837, 00000000:21:00.0, NVIDIA H100 PCIe, 81559, 0, 0, 31, 69, 0, Disabled, 5, 16\n"
    )
    monkeypatch.setattr(
        handlers,
        "run_fixed",
        lambda *_args, **_kwargs: {"ok": True, "stdout": output, "stderr": "", "exit_code": 0},
    )
    monkeypatch.setattr(
        handlers,
        "_driver_gpu_minor_map",
        lambda: {"0000:21:00.0": {"minor_number": "1", "uuid": "GPU-different"}},
    )
    result = handlers.gpu_list()
    assert result["status"] == "UNKNOWN"
    assert result["error"]["error_code"] == "GPU_MINOR_MAPPING_INCOMPLETE"
    assert result["gpus"][0]["minor_number"] is None


def test_gpu_health_distinguishes_clear_kernel_log_from_detected_error(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    def healthy(binary, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        if binary == "journalctl":
            return {
                "ok": False,
                "stdout": "-- No entries --\n",
                "stderr": "",
                "exit_code": 1,
            }
        return {"ok": True, "stdout": "Pass\n", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(handlers, "run_fixed", healthy)
    clear = handlers.gpu_health()
    assert clear["status"] == "OK"
    assert clear["kernel_errors"] == {"status": "CLEAR", "count": 0, "query_ok": True}

    def unhealthy(binary, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        output = "kernel: NVRM: Xid (PCI:0000:21:00): 79\n" if binary == "journalctl" else "Pass\n"
        return {"ok": True, "stdout": output, "stderr": "", "exit_code": 0}

    monkeypatch.setattr(handlers, "run_fixed", unhealthy)
    detected = handlers.gpu_health()
    assert detected["status"] == "PARTIAL"
    assert detected["kernel_errors"] == {
        "status": "DETECTED",
        "count": 1,
        "query_ok": True,
    }

    def inaccessible(binary, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        if binary == "journalctl":
            return {
                "ok": False,
                "stdout": "",
                "stderr": "Failed to open journal",
                "exit_code": 1,
            }
        return {"ok": True, "stdout": "Pass\n", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(handlers, "run_fixed", inaccessible)
    unknown = handlers.gpu_health()
    assert unknown["status"] == "PARTIAL"
    assert unknown["kernel_errors"] == {
        "status": "UNKNOWN",
        "count": 0,
        "query_ok": False,
    }


def test_adapter_error_remains_unknown(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        handlers,
        "run_fixed",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error_code": "COMMAND_TIMEOUT",
            "stdout": "",
            "stderr": "",
        },
    )
    result = handlers.gpu_list()
    assert result["status"] == "UNKNOWN"
    assert "count" not in result


def test_slurm_node_json_is_normalized(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "nodes": [
            {
                "name": "sagsh100server",
                "state": ["IDLE", "DRAIN"],
                "reason": "validation complete",
                "cpus": 256,
                "alloc_cpus": 0,
                "real_memory": 486377,
                "alloc_memory": 0,
                "gres": "gpu:h100:4(S:0-1)",
                "gres_used": "gpu:h100:0(IDX:N/A)",
                "tres": "cpu=256,mem=486377M,gres/gpu=4",
                "tres_used": "",
                "partitions": ["notebook", "train"],
            }
        ]
    }
    monkeypatch.setattr(
        handlers,
        "run_fixed",
        lambda *_args, **_kwargs: {
            "ok": True,
            "stdout": json.dumps(payload),
            "stderr": "",
            "exit_code": 0,
        },
    )
    result = handlers.slurm_node()
    assert result["status"] == "OK"
    assert result["nodes"][0]["state"] == "IDLE+DRAIN"
    assert result["nodes"][0]["partitions"] == "notebook,train"
    assert result["nodes"][0]["real_memory"] == 486377


def test_slurm_jobs_json_is_normalized(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "jobs": [
            {
                "job_id": 42,
                "user_name": "example-user",
                "job_state": ["PENDING"],
                "partition": "train",
                "name": "probe",
                "state_reason": "Resources",
                "node_count": 1,
            }
        ]
    }
    monkeypatch.setattr(
        handlers,
        "run_fixed",
        lambda *_args, **_kwargs: {
            "ok": True,
            "stdout": json.dumps(payload),
            "stderr": "",
            "exit_code": 0,
        },
    )
    result = handlers.slurm_jobs()
    assert result["status"] == "OK"
    assert result["jobs"][0] == {
        "job_id": 42,
        "user": "example-user",
        "state": "PENDING",
        "partition": "train",
        "name": "probe",
        "reason": "Resources",
        "nodes": 1,
    }
