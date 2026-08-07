import json
import subprocess
import uuid
from types import SimpleNamespace

import pytest
from h100_portal_worker import handlers
from h100_portal_worker.handlers import handle, run_fixed
from h100_portal_worker.protocol import ProtocolError, decode_frame, encode_frame
from h100_portal_worker.schemas import WorkerRequest, validate_payload
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def isolate_pilot_state(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    state_root = tmp_path / "pilot-state"
    state_root.mkdir()
    monkeypatch.setattr(handlers, "PILOT_STATE_ROOT", state_root)


def request(
    operation: str,
    payload: dict | None = None,  # type: ignore[type-arg]
    dry_run: bool = False,
    *,
    approved_by: str | None = None,
    idempotency_key: str = "worker-test-0001",
) -> WorkerRequest:
    return WorkerRequest(
        protocol_version=1,
        request_id=str(uuid.uuid4()),
        operation_type=operation,
        payload=payload or {},
        requested_by="origin-al",
        approved_by=approved_by,
        idempotency_key=idempotency_key,
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
    assert write["error"]["code"] == "STAGE_APPROVAL_BINDING_REJECTED"


def test_origin_pilot_is_the_only_portal3a_plan_target() -> None:
    assert validate_payload("user.plan", {"username": "origin-pilot"}) == {
        "username": "origin-pilot"
    }
    with pytest.raises(ValueError, match="origin-pilot"):
        validate_payload("user.plan", {"username": "origin-al"})
    with pytest.raises(ValueError, match="protected username"):
        validate_payload("user.stage", {"username": "origin-al"})
    with pytest.raises(ValueError, match="invalid node name"):
        validate_payload("slurm.resume", {"node_name": "other-node"})


def approved_stage_payload() -> dict[str, object]:
    return {
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
        "approval_reference": handlers.PORTAL3C_STAGE_APPROVAL_REFERENCE,
    }


def staged_result() -> dict[str, object]:
    return {
        "username": "origin-pilot",
        "uid": 20001,
        "gid": 20001,
        "shell": "/usr/sbin/nologin",
        "password": "LOCKED",
        "authorized_keys": "ABSENT",
        "supplemental_groups": [],
        "gpu_policy": {
            "unit": "user-20001.slice",
            "path": "/etc/systemd/system/user-20001.slice.d/50-h100-gpu-isolation.conf",
            "device_policy": "closed",
            "device_allow": [],
            "open_probe": "DENIED",
            "cuda_context_probe": "DENIED",
        },
        "quota": {"project_id": 30001, "hard_limit_gb": 300},
        "slurm": {
            "account": "company",
            "qos": "general",
            "max_gpus": 1,
            "node_state": "DRAIN",
            "queue": "EMPTY",
        },
        "container": {
            "name": "gpu-dev-origin-pilot",
            "state": "STOPPED",
            "gpu": "NONE",
            "cpus": 8,
            "memory_gb": 32,
            "pids_limit": 4096,
            "ssh_port": 22023,
            "image_digest": "sha256:" + "a" * 64,
        },
        "guard": {"timer": "ENABLED_ACTIVE", "status": "PASSING"},
        "ssh_key_state": "REQUIRED_BEFORE_ACTIVATION",
        "host_access": "DISABLED",
        "onboarding_state": "STAGED",
    }


def test_only_exact_approved_real_stage_executes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {name: {"integrity_ok": True} for name in handlers.STAGE_REQUIRED_SCRIPTS},
    )
    monkeypatch.setattr(
        handlers,
        "_user_stage_dry_run",
        lambda _payload: {"status": "DRY_RUN", "stage_status": "READY", "conflicts": []},
    )
    calls: list[list[str]] = []

    def execute(argv, timeout):  # type: ignore[no-untyped-def]
        calls.append(argv)
        assert timeout == handlers.STAGE_EXECUTION_TIMEOUT_SECONDS
        return {"ok": True, "exit_code": 0, "stdout": "STAGED", "stderr": ""}

    monkeypatch.setattr(handlers, "run_allowlisted_script", execute)
    monkeypatch.setattr(handlers, "_stage_postcondition_summary", lambda _payload: staged_result())
    result = handle(
        request(
            "user.stage",
            approved_stage_payload(),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3C_STAGE_IDEMPOTENCY_KEY,
        )
    )
    assert result["status"] == "SUCCEEDED"
    assert result["stage"]["onboarding_state"] == "STAGED"
    assert result["idempotent_replay"] is False
    assert calls == [handlers.build_user_stage_argv(approved_stage_payload())]
    assert "--public-key-file" not in calls[0]


@pytest.mark.parametrize(
    ("approved_by", "idempotency_key"),
    [
        (None, handlers.PORTAL3C_STAGE_IDEMPOTENCY_KEY),
        ("codexops", handlers.PORTAL3C_STAGE_IDEMPOTENCY_KEY),
        ("origin-al", "different-stage-key"),
    ],
)
def test_real_stage_rejects_actor_or_operation_binding(
    approved_by: str | None, idempotency_key: str
) -> None:
    result = handle(
        request(
            "user.stage",
            approved_stage_payload(),
            approved_by=approved_by,
            idempotency_key=idempotency_key,
        )
    )
    assert result["error"]["code"] == "STAGE_APPROVAL_BINDING_REJECTED"


def test_real_stage_fails_closed_on_script_hash_mismatch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {
            name: {"integrity_ok": name != "h100-user-create"}
            for name in handlers.STAGE_REQUIRED_SCRIPTS
        },
    )
    result = handle(
        request(
            "user.stage",
            approved_stage_payload(),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3C_STAGE_IDEMPOTENCY_KEY,
        )
    )
    assert result["error"]["code"] == "SCRIPT_INTEGRITY_FAILED"
    assert result["error"]["scripts"] == ["h100-user-create"]


def test_real_stage_revalidates_conflicts_before_script(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {name: {"integrity_ok": True} for name in handlers.STAGE_REQUIRED_SCRIPTS},
    )
    monkeypatch.setattr(
        handlers,
        "_user_stage_dry_run",
        lambda _payload: {
            "status": "ERROR",
            "stage_status": "CONFLICT",
            "error": {"code": "UID_CONFLICT"},
            "conflicts": [{"code": "UID_CONFLICT"}],
        },
    )
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: pytest.fail("Stage script must not execute after conflict"),
    )
    result = handle(
        request(
            "user.stage",
            approved_stage_payload(),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3C_STAGE_IDEMPOTENCY_KEY,
        )
    )
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "UID_CONFLICT"


def test_real_stage_reports_transaction_rollback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {name: {"integrity_ok": True} for name in handlers.STAGE_REQUIRED_SCRIPTS},
    )
    monkeypatch.setattr(
        handlers,
        "_user_stage_dry_run",
        lambda _payload: {"status": "DRY_RUN", "stage_status": "READY"},
    )
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: {"ok": False, "exit_code": 1},
    )
    monkeypatch.setattr(handlers, "_stage_retained_resources", lambda _payload: [])
    result = handle(
        request(
            "user.stage",
            approved_stage_payload(),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3C_STAGE_IDEMPOTENCY_KEY,
        )
    )
    assert result["status"] == "ERROR"
    assert result["rollback_status"] == "ROLLED_BACK"
    assert result["host_resources_retained"] is False


def test_real_stage_reports_partial_useradd_state_as_retained(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {name: {"integrity_ok": True} for name in handlers.STAGE_REQUIRED_SCRIPTS},
    )
    monkeypatch.setattr(
        handlers,
        "_user_stage_dry_run",
        lambda _payload: {"status": "DRY_RUN", "stage_status": "READY"},
    )
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: {"ok": False, "exit_code": 12},
    )
    monkeypatch.setattr(
        handlers,
        "_stage_retained_resources",
        lambda _payload: ["linux-group", "linux-user"],
    )
    result = handle(
        request(
            "user.stage",
            approved_stage_payload(),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3C_STAGE_IDEMPOTENCY_KEY,
        )
    )
    assert result["status"] == "ERROR"
    assert result["rollback_status"] == "PARTIAL_RETAINED"
    assert result["host_resources_retained"] is True
    assert result["retained_resources"] == ["linux-group", "linux-user"]


def test_real_stage_idempotent_replay_does_not_execute(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    state_root = tmp_path / "users"
    state_root.mkdir()
    (state_root / "origin-pilot.state").write_text("STATUS=STAGED\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "PILOT_STATE_ROOT", state_root)
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {name: {"integrity_ok": True} for name in handlers.STAGE_REQUIRED_SCRIPTS},
    )
    monkeypatch.setattr(handlers, "_stage_postcondition_summary", lambda _payload: staged_result())
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: pytest.fail("idempotent replay must not run Stage script"),
    )
    result = handle(
        request(
            "user.stage",
            approved_stage_payload(),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3C_STAGE_IDEMPOTENCY_KEY,
        )
    )
    assert result["status"] == "SUCCEEDED"
    assert result["idempotent_replay"] is True


def test_activate_real_write_remains_disabled() -> None:
    payload = {
        "managed_user_id": str(uuid.uuid4()),
        "approved_ssh_key_record_ids": [str(uuid.uuid4())],
        "expected_state": "STAGED",
        "approval_reference": "portal3c-test-approval",
    }
    result = handle(request("user.activate", payload, approved_by="origin-al"))
    assert result["error"]["code"] == "WRITE_EXECUTION_DISABLED"


def test_stage_contract_defers_public_key() -> None:
    payload = approved_stage_payload()
    assert validate_payload("user.stage", payload)["username"] == "origin-pilot"
    assert (
        validate_payload(
            "user.stage",
            {key: value for key, value in payload.items() if key != "approval_reference"},
        )["uid"]
        == 20001
    )
    with pytest.raises(ValueError, match="PUBLIC_KEY_NOT_ALLOWED_DURING_STAGE"):
        validate_payload("user.stage", {**payload, "public_key_file": "record-id.pub"})
    with pytest.raises(ValueError, match="STAGE_PAYLOAD_REJECTED"):
        validate_payload("user.stage", {"username": "origin-pilot"})


def test_activate_requires_record_ids_before_any_worker_write() -> None:
    with pytest.raises(ValueError, match="PUBLIC_KEY_REQUIRED_FOR_ACTIVATION"):
        validate_payload(
            "user.activate",
            {
                "managed_user_id": str(uuid.uuid4()),
                "expected_state": "STAGED",
                "approval_reference": "portal3b-r-test-v1",
            },
        )
    rejected = handle(
        request(
            "user.activate",
            {
                "managed_user_id": str(uuid.uuid4()),
                "expected_state": "STAGED",
                "approval_reference": "portal3b-r-test-v1",
            },
            dry_run=True,
        )
    )
    assert rejected["error"]["code"] == "PUBLIC_KEY_REQUIRED_FOR_ACTIVATION"


def test_stage_dry_run_has_no_key_and_fixed_future_argv(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        handlers,
        "_user_plan",
        lambda _username: {
            "status": "DRY_RUN",
            "plan_status": "READY",
            "proposed_uid": 20001,
            "proposed_gid": 20001,
            "proposed_project_id": 30001,
            "proposed_ssh_port": 22023,
            "proposed_quota_hard_limit_gb": 300,
            "proposed_slurm_account": "company",
            "proposed_qos": "general",
            "proposed_max_gpus": 1,
            "proposed_container": {
                "name": "gpu-dev-origin-pilot",
                "cpus": 8,
                "memory_gb": 32,
                "pids_limit": 4096,
                "gpu": "none",
            },
            "conflicts": [],
        },
    )
    payload = approved_stage_payload()
    result = handle(request("user.stage", payload, dry_run=True))
    assert result["status"] == "DRY_RUN"
    assert result["stage_status"] == "READY"
    assert result["ssh_key_status"] == "NOT_REQUIRED_FOR_STAGE"
    assert result["post_stage_ssh_key_state"] == "REQUIRED_BEFORE_ACTIVATION"
    assert result["execution_enabled"] is False
    assert "--public-key-file" not in handlers.build_user_stage_argv(payload)


def test_activate_rejects_arbitrary_path_and_private_key_field() -> None:
    base = {
        "managed_user_id": str(uuid.uuid4()),
        "approved_ssh_key_record_ids": [str(uuid.uuid4())],
        "expected_state": "STAGED",
        "approval_reference": "portal3b-r-test-v1",
    }
    with pytest.raises(ValueError, match="ARBITRARY_PATH_REJECTED"):
        validate_payload("user.activate", {**base, "public_key_path": "/etc/shadow"})
    with pytest.raises(ValueError, match="PAYLOAD_REJECTED"):
        validate_payload("user.activate", {**base, "raw_private_key": "PRIVATE KEY"})


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


def test_origin_pilot_plan_is_structured_and_never_executes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fake_getpwnam(username: str) -> SimpleNamespace:
        if username == handlers.MANAGEMENT_USERNAME:
            return SimpleNamespace(pw_uid=1000, pw_gid=1000)
        raise KeyError(username)

    def fake_getgrnam(group_name: str) -> None:
        raise KeyError(group_name)

    monkeypatch.setattr(handlers.pwd, "getpwnam", fake_getpwnam)
    monkeypatch.setattr(handlers.grp, "getgrnam", fake_getgrnam)
    monkeypatch.setattr(handlers, "_group_names", lambda _username, _gid: ["adm", "lxd", "sudo"])
    monkeypatch.setattr(handlers.Path, "exists", lambda _path: False)
    monkeypatch.setattr(handlers, "_registry_entries", lambda: [])
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {
            "h100-user-create": {"integrity_ok": True},
            "h100-user-gpu-isolation": {"integrity_ok": True},
            "h100-container-create": {"integrity_ok": True},
            "h100-container-stop": {"integrity_ok": True},
            "h100-gpu-bypass-guard": {"integrity_ok": True},
        },
    )
    monkeypatch.setattr(
        handlers, "_candidate_uid_gid", lambda: (20001, [], {"legacy_ownership_scan": "PASS"})
    )
    monkeypatch.setattr(
        handlers,
        "_candidate_project_id",
        lambda: (30001, {"reservation": "PROPOSED — NOT RESERVED"}),
    )
    monkeypatch.setattr(
        handlers, "_candidate_ssh_port", lambda: (22023, {"reservation": "PROPOSED — NOT RESERVED"})
    )
    monkeypatch.setattr(
        handlers,
        "_slurm_plan_checks",
        lambda _username: [
            {"check": "slurm_node_drained", "status": "PASS", "detail": "DRAIN"},
            {"check": "slurm_queue_empty", "status": "PASS", "detail": "empty"},
            {"check": "slurm_account_company", "status": "PASS", "detail": "company"},
            {"check": "slurm_qos_general_max_gpu", "status": "PASS", "detail": "one"},
            {"check": "slurm_association_absent", "status": "PASS", "detail": "absent"},
        ],
    )

    def fixed_result(binary, args, **_kwargs):  # type: ignore[no-untyped-def]
        if binary == "docker":
            return {
                "ok": False,
                "stdout": "",
                "stderr": "No such container: gpu-dev-origin-pilot",
                "exit_code": 1,
            }
        if binary == "systemctl":
            state = "inactive" if args[0] == "is-active" else "disabled"
            return {"ok": False, "stdout": f"{state}\n", "stderr": "", "exit_code": 1}
        return {"ok": True, "stdout": "", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(handlers, "run_fixed", fixed_result)
    result = handle(request("user.plan", {"username": "origin-pilot"}, dry_run=True))
    assert result["status"] == "DRY_RUN"
    assert result["plan_status"] == "READY"
    assert result["execution_enabled"] is False
    assert result["proposed_uid"] == 20001
    assert result["proposed_project_id"] == 30001
    assert result["proposed_ssh_port"] == 22023
    assert result["ssh_key_status"] == "REQUIRED BEFORE ACTIVATION"
    assert "command" not in json.dumps(result).casefold()


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


def test_live_image_inventory_requires_digest_and_uses_whitelisted_fields(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    digest = "sha256:" + "a" * 64

    def docker_result(_binary, args, **_kwargs):  # type: ignore[no-untyped-def]
        if args[:2] == ["image", "ls"]:
            return {
                "ok": True,
                "stdout": json.dumps(
                    {
                        "Repository": "h100-local/dev-container",
                        "Tag": "ubuntu24.04",
                        "Digest": digest,
                        "ID": digest,
                        "Size": "983MB",
                        "Containers": "1",
                        "CreatedAt": "2026-08-04",
                    }
                ),
            }
        return {
            "ok": True,
            "stdout": json.dumps([{"Architecture": "amd64", "Os": "linux"}]),
        }

    monkeypatch.setattr(handlers, "run_fixed", docker_result)
    result = handlers.images_list()
    assert result["status"] == "OK"
    assert result["images"][0]["digest"] == digest
    assert result["images"][0]["immutable"] is True
    assert result["images"][0]["architecture"] == "amd64"
