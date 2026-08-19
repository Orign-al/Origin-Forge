import base64
import hashlib
import json
import os
import queue
import socket
import struct
import subprocess
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from h100_portal_worker import handlers, server
from h100_portal_worker import terminal as worker_terminal
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
    requested_by: str = "origin-al",
    approved_by: str | None = None,
    idempotency_key: str = "worker-test-0001",
) -> WorkerRequest:
    return WorkerRequest(
        protocol_version=1,
        request_id=str(uuid.uuid4()),
        operation_type=operation,
        payload=payload or {},
        requested_by=requested_by,
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


def test_protocol_rejection_does_not_echo_private_key_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("H100_PORTAL_WORKER_TESTING", "1")
    private_marker = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "forbidden-test-material\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    request_body = json.dumps(
        {
            "protocol_version": 1,
            "request_id": str(uuid.uuid4()),
            "operation_type": "gpu.list",
            "payload": {},
            "requested_by": "origin-al",
            "approved_by": None,
            "idempotency_key": "worker-test-private-rejection",
            "dry_run": False,
            "private_key": private_marker,
        }
    ).encode()
    worker_socket, client_socket = socket.socketpair()
    try:
        client_socket.sendall(struct.pack("!I", len(request_body)) + request_body)
        server.process_connection(worker_socket)
        response = decode_frame(client_socket.recv(64 * 1024))
    finally:
        client_socket.close()
    serialized = json.dumps(response)
    assert response["error"]["code"] == "PROTOCOL_REJECTED"
    assert "PRIVATE KEY" not in serialized
    assert "forbidden-test-material" not in serialized


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


def test_portal3f_payloads_are_exact_and_reject_injection() -> None:
    client = dict(handlers.APPROVED_CLIENT_VALIDATION_PAYLOAD)
    pilot = dict(handlers.APPROVED_PILOT_ACCEPTANCE_PAYLOAD)
    assert validate_payload("user.ssh_client_validation.record", client) == client
    assert validate_payload("user.pilot.acceptance", pilot) == pilot

    with pytest.raises(ValueError, match="PORTAL3F_PLAN_MISMATCH"):
        validate_payload("user.pilot.acceptance", {**pilot, "partition": "train"})
    with pytest.raises(ValueError, match="PORTAL3F_PAYLOAD_REJECTED"):
        validate_payload("user.pilot.acceptance", {**pilot, "command": "nvidia-smi"})
    with pytest.raises(ValueError, match="PORTAL3F_PAYLOAD_REJECTED"):
        validate_payload("user.ssh_client_validation.record", {**client, "path": "/etc/shadow"})


def test_portal3f_client_validation_records_user_confirmation_without_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = {"onboarding_state": "ACTIVE", "slurm": {"node_state": "DRAIN"}}
    monkeypatch.setattr(handlers, "_portal3f_active_preflight", lambda: snapshot)
    result = handle(
        request(
            "user.ssh_client_validation.record",
            dict(handlers.APPROVED_CLIENT_VALIDATION_PAYLOAD),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY,
        )
    )
    assert result["status"] == "SUCCEEDED"
    assert result["host_client_validation"] == "PASS"
    assert result["container_client_validation"] == "PASS"
    assert result["private_key_handling"] == "NOT_ACCESSED"
    assert '"private_key":' not in json.dumps(result).casefold()


def test_portal3f_pilot_acceptance_uses_only_fixed_script_and_restores_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = {"onboarding_state": "ACTIVE", "slurm": {"node_state": "DRAIN"}}
    request_value = request(
        "user.pilot.acceptance",
        dict(handlers.APPROVED_PILOT_ACCEPTANCE_PAYLOAD),
        approved_by="origin-al",
        idempotency_key=handlers.PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY,
    )
    monkeypatch.setattr(
        handlers,
        "_portal3f_pilot_acceptance_plan",
        lambda _payload: {"preflight": snapshot},
    )
    monkeypatch.setattr(handlers, "_portal3f_active_preflight", lambda: snapshot)
    executed: list[list[str]] = []

    def execute(argv: list[str], timeout: float) -> dict[str, object]:
        executed.append(argv)
        assert timeout == 1500
        return {
            "ok": True,
            "stdout": "\n".join(
                [
                    "PORTAL3F_RESULT=PASSED",
                    "CPU_JOB_ID=101",
                    "GPU_JOB_ID=102",
                    "ALLOCATED_GPU_UUID=GPU-11111111-2222-3333-4444-555555555555",
                    "OUT_OF_JOB_GPU_OPEN=DENIED",
                    "OUT_OF_JOB_CUDA_CONTEXT=DENIED",
                    "IN_JOB_ALLOCATED_GPU=ALLOWED",
                    "IN_JOB_UNALLOCATED_GPUS=DENIED",
                    "IN_JOB_CUDA_CONTEXT=PASSED",
                    "FINAL_NODE_STATE=DRAIN",
                    (
                        "/srv/gpu-platform/platform/logs/portal3f-worker-"
                        f"{request_value.request_id}"
                    ).join(("WORKER_LOG_DIR=", "")),
                ]
            ),
        }

    monkeypatch.setattr(handlers, "run_allowlisted_script", execute)
    result = handle(request_value)
    assert result["status"] == "SUCCEEDED"
    assert result["acceptance"]["cpu_job_id"] == 101
    assert result["acceptance"]["gpu_job_id"] == 102
    assert result["acceptance"]["final_node_state"] == "DRAIN"
    assert executed == [
        [
            handlers.SCRIPT_ALLOWLIST["h100-origin-pilot-acceptance"],
            "--execute",
            request_value.request_id,
        ]
    ]


def test_portal3f_pilot_failure_reports_verified_drain_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handlers,
        "_portal3f_pilot_acceptance_plan",
        lambda _payload: {"preflight": {}},
    )
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda _argv, timeout: {"ok": False, "exit_code": 1},
    )
    monkeypatch.setattr(handlers, "_portal3f_drain_recovery_status", lambda: "DRAIN_RESTORED")
    result = handle(
        request(
            "user.pilot.acceptance",
            dict(handlers.APPROVED_PILOT_ACCEPTANCE_PAYLOAD),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY,
        )
    )
    assert result["status"] == "ERROR"
    assert result["rollback_status"] == "DRAIN_RESTORED"


def test_portal3g_payload_is_exact_and_rejects_command_injection() -> None:
    payload = dict(handlers.APPROVED_PRODUCTION_PILOT_PAYLOAD)
    assert validate_payload("slurm.production_pilot.start", payload) == payload
    with pytest.raises(ValueError, match="PORTAL3G_PLAN_MISMATCH"):
        validate_payload("slurm.production_pilot.start", {**payload, "max_gpus": 2})
    with pytest.raises(ValueError, match="PORTAL3G_PAYLOAD_REJECTED"):
        validate_payload(
            "slurm.production_pilot.start",
            {**payload, "argv": ["update", "NodeName=other"]},
        )


def test_portal3g_start_uses_only_fixed_resume_and_submits_no_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drain_snapshot = {"slurm": {"node_state": "DRAIN"}}
    idle_snapshot = {"slurm": {"node_state": "IDLE"}}
    monkeypatch.setattr(
        handlers,
        "_portal3g_production_pilot_plan",
        lambda _payload: {
            "preflight": drain_snapshot,
            "production_pilot_scope": {
                "mode": "SINGLE_NODE",
                "managed_users": ["origin-pilot"],
                "max_gpus": 1,
            },
        },
    )
    monkeypatch.setattr(
        handlers,
        "_portal3g_wait_for_node",
        lambda state, timeout_seconds=20: {
            "name": "sagsh100server",
            "state": state,
            "queue": "EMPTY",
            "reason": "NONE",
            "jobs_submitted": 0,
        },
    )
    monkeypatch.setattr(
        handlers,
        "_portal3f_active_preflight",
        lambda expected_state="DRAIN": (
            idle_snapshot if expected_state == "IDLE" else drain_snapshot
        ),
    )
    commands: list[tuple[str, list[str]]] = []

    def fixed(binary: str, args: list[str], timeout: float = 20) -> dict[str, object]:
        commands.append((binary, args))
        return {"ok": True, "stdout": ""}

    monkeypatch.setattr(handlers, "run_fixed", fixed)
    result = handle(
        request(
            "slurm.production_pilot.start",
            dict(handlers.APPROVED_PRODUCTION_PILOT_PAYLOAD),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3G_PRODUCTION_PILOT_IDEMPOTENCY_KEY,
        )
    )
    assert result["status"] == "SUCCEEDED"
    assert result["node"]["state"] == "IDLE"
    assert result["jobs_submitted"] == 0
    assert commands == [
        (
            "scontrol",
            ["update", "NodeName=sagsh100server", "State=RESUME"],
        )
    ]


def test_portal3g_postflight_failure_drains_and_does_not_retry_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handlers,
        "_portal3g_production_pilot_plan",
        lambda _payload: {
            "preflight": {},
            "production_pilot_scope": {
                "mode": "SINGLE_NODE",
                "managed_users": ["origin-pilot"],
                "max_gpus": 1,
            },
        },
    )
    monkeypatch.setattr(
        handlers,
        "_portal3g_wait_for_node",
        lambda state, timeout_seconds=20: {
            "name": "sagsh100server",
            "state": state,
            "queue": "EMPTY",
            "reason": "NONE",
            "jobs_submitted": 0,
        },
    )
    monkeypatch.setattr(
        handlers,
        "_portal3f_active_preflight",
        lambda _state="DRAIN": (_ for _ in ()).throw(
            handlers.LifecycleValidationError("PORTAL3G_POSTFLIGHT_FAILED", "health failed")
        ),
    )
    monkeypatch.setattr(handlers, "_portal3g_safety_drain", lambda: "DRAIN_RESTORED")
    commands: list[list[str]] = []

    def fixed(_binary: str, args: list[str], timeout: float = 20) -> dict[str, object]:
        commands.append(args)
        return {"ok": True, "stdout": ""}

    monkeypatch.setattr(handlers, "run_fixed", fixed)
    result = handle(
        request(
            "slurm.production_pilot.start",
            dict(handlers.APPROVED_PRODUCTION_PILOT_PAYLOAD),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3G_PRODUCTION_PILOT_IDEMPOTENCY_KEY,
        )
    )
    assert result["status"] == "ERROR"
    assert result["rollback_status"] == "DRAIN_RESTORED"
    assert commands == [["update", "NodeName=sagsh100server", "State=RESUME"]]


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
        "host_authorized_keys": "ABSENT",
        "container_authorized_keys": "ABSENT",
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


def effective_ssh_config(**overrides: str) -> dict[str, str]:
    config = {
        "pubkeyauthentication": "yes",
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "authenticationmethods": "publickey",
        "permitrootlogin": "prohibit-password",
        "authorizedkeysfile": ".ssh/authorized_keys .ssh/authorized_keys2",
        "usepam": "yes",
        "allowtcpforwarding": "yes",
        "x11forwarding": "yes",
    }
    config.update(overrides)
    return config


def passing_host_ssh_policy() -> dict[str, object]:
    return {
        "pubkey_authentication": True,
        "password_authentication": False,
        "keyboard_interactive_authentication": False,
        "authentication_methods": ["publickey"],
        "authentication_methods_raw": "publickey",
        "status": "PASSING",
        "source": "SSHD_EFFECTIVE_CONFIG",
    }


def test_managed_host_ssh_policy_requires_complete_public_key_only_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handlers,
        "_sshd_effective_config",
        lambda username: effective_ssh_config(),
    )

    policy = handlers.managed_host_ssh_policy("origin-pilot")

    assert policy["pubkey_authentication"] is True
    assert policy["password_authentication"] is False
    assert policy["keyboard_interactive_authentication"] is False
    assert policy["authentication_methods"] == ["publickey"]
    assert policy["status"] == "PASSING"


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("passwordauthentication", "yes"),
        ("pubkeyauthentication", "no"),
        ("kbdinteractiveauthentication", "yes"),
        ("authenticationmethods", "any"),
    ],
)
def test_managed_host_ssh_policy_fails_closed_on_unsafe_effective_value(
    monkeypatch: pytest.MonkeyPatch, override: str, value: str
) -> None:
    monkeypatch.setattr(
        handlers,
        "_sshd_effective_config",
        lambda username: effective_ssh_config(**{override: value}),
    )

    with pytest.raises(
        handlers.LifecycleValidationError,
        match="HOST_SSH_POLICY_PREFLIGHT_FAILED",
    ):
        handlers.managed_host_ssh_policy("origin-pilot")


def test_sshd_effective_config_command_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handlers,
        "run_fixed",
        lambda *_args, **_kwargs: {
            "ok": False,
            "exit_code": 255,
            "stdout": "",
            "stderr": "not logged by this test",
        },
    )

    with pytest.raises(
        handlers.LifecycleValidationError,
        match="HOST_SSH_POLICY_PREFLIGHT_FAILED",
    ):
        handlers._sshd_effective_config("origin-pilot")


def test_sshd_effective_config_rejects_unknown_user_and_context_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handlers,
        "run_fixed",
        lambda *_args, **_kwargs: pytest.fail("rejected identity must not reach sshd"),
    )

    for username in ("unknown-user", "origin-pilot,addr=203.0.113.9"):
        with pytest.raises(
            handlers.LifecycleValidationError,
            match="HOST_SSH_POLICY_TARGET_REJECTED",
        ):
            handlers._sshd_effective_config(username)


@pytest.mark.parametrize("username", ["origin-al", "codexops"])
def test_management_ssh_policy_regression_is_rejected(username: str) -> None:
    before = effective_ssh_config()
    handlers.validate_ssh_policy_no_regression(username, before, dict(before))
    after = {**before, "passwordauthentication": "yes"}

    with pytest.raises(
        handlers.LifecycleValidationError,
        match="HOST_SSH_POLICY_MANAGEMENT_REGRESSION",
    ):
        handlers.validate_ssh_policy_no_regression(username, before, after)


def test_malformed_sshd_effective_output_is_rejected() -> None:
    with pytest.raises(
        handlers.LifecycleValidationError,
        match="HOST_SSH_POLICY_PREFLIGHT_FAILED",
    ):
        handlers._parse_sshd_effective_config("passwordauthentication no\n")


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


def test_unapproved_activate_real_write_is_rejected() -> None:
    payload = {
        "managed_user_id": str(uuid.uuid4()),
        "approved_ssh_key_record_ids": [str(uuid.uuid4())],
        "expected_state": "STAGED",
        "approval_reference": "portal3c-test-approval",
    }
    result = handle(request("user.activate", payload, approved_by="origin-al"))
    assert result["error"]["code"] == "ACTIVATE_APPROVAL_BINDING_REJECTED"


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
    with pytest.raises(ValueError, match="PAYLOAD_REJECTED"):
        validate_payload("user.activate", {**base, "private_key_password": "forbidden"})
    with pytest.raises(ValueError, match="PAYLOAD_REJECTED"):
        validate_payload("user.activate", {**base, "private_key_path": "/forbidden"})


def portal3e_final_worker_payload() -> dict[str, object]:
    return {
        "managed_user_id": handlers.PORTAL3E_FINAL_MANAGED_USER_ID,
        "approved_ssh_key_record_ids": [handlers.PORTAL3E_FINAL_KEY_RECORD_ID],
        "expected_state": "STAGED",
        "approval_reference": handlers.PORTAL3E_FINAL_APPROVAL_REFERENCE,
        "dry_run_operation_id": handlers.PORTAL3E_FINAL_DRY_RUN_OPERATION_ID,
    }


def portal3e_final_key_record() -> dict[str, object]:
    return {
        "record_id": handlers.PORTAL3E_FINAL_KEY_RECORD_ID,
        "key_type": "ssh-ed25519",
        "fingerprint_sha256": handlers.PORTAL3E_FINAL_KEY_FINGERPRINT,
        "content_sha256": "a" * 64,
        "scope": "BOTH",
        "operation_id": str(uuid.uuid4()),
        "managed_user_id": handlers.PORTAL3E_FINAL_MANAGED_USER_ID,
        "size_bytes": 96,
    }


def test_portal3e_final_activate_requires_every_exact_approval_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = portal3e_final_worker_payload()
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {name: {"integrity_ok": True} for name in handlers.ACTIVATE_REQUIRED_SCRIPTS},
    )
    monkeypatch.setattr(
        handlers,
        "_user_activate_dry_run",
        lambda _payload: {"status": "DRY_RUN", "approved_ssh_keys": [portal3e_final_key_record()]},
    )
    monkeypatch.setattr(
        handlers,
        "_sshd_effective_config",
        lambda _username: effective_ssh_config(),
    )

    @handlers.contextmanager
    def bundles(request_id: str, _records: list[dict[str, object]]):  # type: ignore[no-untyped-def]
        yield (
            handlers.SSH_KEY_STAGING_ROOT / f"{request_id}.host.pub",
            handlers.SSH_KEY_STAGING_ROOT / f"{request_id}.container.pub",
        )

    monkeypatch.setattr(handlers, "activation_key_bundles", bundles)
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: {"ok": True, "stdout": "ACTIVE"},
    )
    monkeypatch.setattr(
        handlers,
        "_activate_postcondition_summary",
        lambda _records, _management: {"onboarding_state": "ACTIVE"},
    )

    approved = handle(
        request(
            "user.activate",
            payload,
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3E_FINAL_IDEMPOTENCY_KEY,
        )
    )
    wrong_idempotency = handle(
        request(
            "user.activate",
            payload,
            approved_by="origin-al",
            idempotency_key="portal3e-final-wrong-binding",
        )
    )
    wrong_dry_run = handle(
        request(
            "user.activate",
            {**payload, "dry_run_operation_id": str(uuid.uuid4())},
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3E_FINAL_IDEMPOTENCY_KEY,
        )
    )

    assert approved["status"] == "SUCCEEDED"
    assert approved["execution_enabled"] is True
    assert approved["activate"]["onboarding_state"] == "ACTIVE"
    assert wrong_idempotency["error"]["code"] == "ACTIVATE_APPROVAL_BINDING_REJECTED"
    assert wrong_dry_run["error"]["code"] == "ACTIVATE_APPROVAL_BINDING_REJECTED"


def test_portal3e_final_activate_rolls_back_failed_postcondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = portal3e_final_worker_payload()
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {name: {"integrity_ok": True} for name in handlers.ACTIVATE_REQUIRED_SCRIPTS},
    )
    monkeypatch.setattr(
        handlers,
        "_user_activate_dry_run",
        lambda _payload: {"status": "DRY_RUN", "approved_ssh_keys": [portal3e_final_key_record()]},
    )
    monkeypatch.setattr(
        handlers,
        "_sshd_effective_config",
        lambda _username: effective_ssh_config(),
    )

    @handlers.contextmanager
    def bundles(request_id: str, _records: list[dict[str, object]]):  # type: ignore[no-untyped-def]
        yield (
            handlers.SSH_KEY_STAGING_ROOT / f"{request_id}.host.pub",
            handlers.SSH_KEY_STAGING_ROOT / f"{request_id}.container.pub",
        )

    executed: list[list[str]] = []

    def run_script(argv: list[str], timeout: float) -> dict[str, object]:
        del timeout
        executed.append(argv)
        return {"ok": True, "stdout": "ok"}

    monkeypatch.setattr(handlers, "activation_key_bundles", bundles)
    monkeypatch.setattr(handlers, "run_allowlisted_script", run_script)
    monkeypatch.setattr(
        handlers,
        "_activate_postcondition_summary",
        lambda _records, _management: (_ for _ in ()).throw(
            handlers.LifecycleValidationError(
                "CONTAINER_SSH_POLICY_FAILED", "password authentication was enabled"
            )
        ),
    )
    monkeypatch.setattr(handlers, "_stage_postcondition_summary", lambda _payload: staged_result())

    result = handle(
        request(
            "user.activate",
            payload,
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3E_FINAL_IDEMPOTENCY_KEY,
        )
    )

    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "CONTAINER_SSH_POLICY_FAILED"
    assert result["rollback_status"] == "ROLLED_BACK"
    assert executed[-1] == handlers.build_user_activate_rollback_argv("origin-pilot")


def test_portal3e_final_internal_rollback_is_exact_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {"h100-user-create": {"integrity_ok": True}},
    )
    monkeypatch.setattr(handlers, "_stage_postcondition_summary", lambda _payload: staged_result())
    result = handle(
        request(
            "user.activate.rollback",
            portal3e_final_worker_payload(),
            approved_by="origin-al",
            idempotency_key=handlers.PORTAL3E_FINAL_ROLLBACK_IDEMPOTENCY_KEY,
        )
    )
    rejected = handle(
        request(
            "user.activate.rollback",
            portal3e_final_worker_payload(),
            approved_by="origin-al",
            idempotency_key="portal3e-final-unapproved-rollback",
        )
    )
    assert result["status"] == "SUCCEEDED"
    assert result["rollback_status"] == "ROLLED_BACK"
    assert result["idempotent_replay"] is True
    assert rejected["error"]["code"] == "ACTIVATE_ROLLBACK_BINDING_REJECTED"


def test_container_ssh_effective_policy_fails_closed_on_password_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effective = effective_ssh_config(
        permitrootlogin="no",
        authorizedkeysfile=".ssh/authorized_keys",
        x11forwarding="no",
    )

    observed_args: list[str] = []

    def docker_exec(_binary: str, args: list[str], timeout: float = 20.0) -> dict[str, object]:
        del timeout
        observed_args.extend(args)
        return {
            "ok": True,
            "stdout": "".join(f"{key} {value}\n" for key, value in effective.items()),
            "stderr": "",
        }

    monkeypatch.setattr(handlers, "run_fixed", docker_exec)
    assert handlers._container_ssh_effective_policy("gpu-dev-origin-pilot")["status"] == "PASSING"
    assert observed_args == [
        "exec",
        "gpu-dev-origin-pilot",
        "/usr/sbin/sshd",
        "-T",
        "-C",
        "user=origin-pilot,host=sagsh100server,addr=10.20.18.10",
    ]

    effective["passwordauthentication"] = "yes"
    with pytest.raises(handlers.LifecycleValidationError, match="public-key-only"):
        handlers._container_ssh_effective_policy("gpu-dev-origin-pilot")


def generate_worker_public_key(tmp_path: Path, name: str) -> str:
    key_path = tmp_path / name
    subprocess.run(
        [
            "/usr/bin/ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "Worker test key",
            "-f",
            str(key_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    public_key = key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()
    key_path.unlink()
    key_path.with_suffix(".pub").unlink()
    return public_key


@pytest.fixture
def worker_public_key(tmp_path: Path) -> str:
    return generate_worker_public_key(tmp_path, "worker-test-ed25519")


@pytest.fixture
def controlled_staging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    staging = tmp_path / "ssh-key-staging"
    staging.mkdir(mode=0o700)
    staging.chmod(0o700)
    monkeypatch.setattr(handlers, "SSH_KEY_STAGING_ROOT", staging)
    monkeypatch.setattr(handlers, "SSH_KEY_STAGING_OWNER_UID", os.getuid())
    monkeypatch.setattr(handlers, "SSH_KEY_STAGING_OWNER_GID", os.getgid())
    return staging


def test_project_quota_validation_requires_enforcement_and_exact_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def quota_command(_binary: str, args: list[str], timeout: float = 20.0) -> dict[str, object]:
        del timeout
        if args[2] == "state":
            output = "Project quota state on test\n  Accounting: ON\n  Enforcement: ON\n"
        else:
            output = "#30001 0 0 314572800 00 [--------]\n"
        return {"ok": True, "stdout": output, "stderr": ""}

    monkeypatch.setattr(handlers, "run_fixed", quota_command)
    result = handlers._verified_project_quota(30001, 300)
    assert result["hard_limit_gb"] == 300
    assert result["enforcement"] == "ON"

    def wrong_limit(_binary: str, args: list[str], timeout: float = 20.0) -> dict[str, object]:
        del timeout
        output = (
            "Project quota state on test\n  Accounting: ON\n  Enforcement: ON\n"
            if args[2] == "state"
            else "#30001 0 0 1 00 [--------]\n"
        )
        return {"ok": True, "stdout": output, "stderr": ""}

    monkeypatch.setattr(handlers, "run_fixed", wrong_limit)
    with pytest.raises(handlers.LifecycleValidationError, match="hard quota"):
        handlers._verified_project_quota(30001, 300)


def test_guard_metrics_validation_binds_current_user_and_deny_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    metric_path = tmp_path / "h100_gpu_bypass_guard.prom"
    timestamp = int(handlers.time.time())
    metric_path.write_text(
        "\n".join(
            [
                "h100_gpu_bypass_guard_last_success 1",
                "h100_gpu_bypass_guard_managed_users 1",
                "h100_gpu_bypass_guard_users_verified 1",
                "h100_gpu_bypass_guard_policy_errors 0",
                "h100_gpu_bypass_guard_device_open_failures 0",
                "h100_gpu_bypass_guard_cuda_context_failures 0",
                "h100_gpu_bypass_guard_slurm_constrain_devices 1",
                "h100_gpu_bypass_guard_nvidia_gpu_count 4",
                "h100_gpu_bypass_guard_slurm_gpu_count 4",
                f"h100_gpu_bypass_guard_timestamp_seconds {timestamp}",
                'h100_gpu_user_isolation_success{username="origin-pilot",uid="20001"} 1',
                "",
            ]
        ),
        encoding="ascii",
    )
    metric_path.chmod(0o644)
    monkeypatch.setattr(handlers, "GUARD_METRIC_FILE", metric_path)
    monkeypatch.setattr(handlers, "GUARD_METRIC_OWNER_UID", os.getuid())
    monkeypatch.setattr(handlers, "GUARD_METRIC_OWNER_GID", os.getgid())

    result = handlers._verified_guard_metrics(20001)
    assert result["status"] == "PASSING"
    assert result["open_probe"] == "DENIED"
    assert result["cuda_context_probe"] == "DENIED"

    metric_path.write_text(
        metric_path.read_text(encoding="ascii").replace(
            "h100_gpu_bypass_guard_device_open_failures 0",
            "h100_gpu_bypass_guard_device_open_failures 1",
        ),
        encoding="ascii",
    )
    with pytest.raises(handlers.LifecycleValidationError, match="do not prove isolation"):
        handlers._verified_guard_metrics(20001)


def ssh_key_prepare_payload(public_key: str, *, scope: str = "BOTH") -> dict[str, object]:
    validated = handlers._validate_public_key_content(public_key)
    return {
        "record_id": str(uuid.uuid4()),
        "operation_id": str(uuid.uuid4()),
        "managed_user_id": str(uuid.uuid4()),
        "username": "origin-pilot",
        "public_key": public_key,
        "key_type": validated["key_type"],
        "fingerprint_sha256": validated["fingerprint_sha256"],
        "content_sha256": validated["content_sha256"],
        "scope": scope,
    }


def prepare_key(payload: dict[str, object]) -> dict[str, object]:
    record_id = str(payload["record_id"])
    return handle(
        request(
            "ssh_key.prepare",
            payload,
            approved_by="origin-al",
            idempotency_key=f"ssh-key-enroll:{record_id}",
        )
    )


def test_activation_bundles_separate_host_and_container_scopes(
    controlled_staging: Path,
    tmp_path: Path,
) -> None:
    managed_user_id = str(uuid.uuid4())
    host_public_key = generate_worker_public_key(tmp_path, "host-scope-key")
    container_public_key = generate_worker_public_key(tmp_path, "container-scope-key")
    host_payload = ssh_key_prepare_payload(host_public_key, scope="HOST")
    container_payload = ssh_key_prepare_payload(container_public_key, scope="CONTAINER")
    host_payload["managed_user_id"] = managed_user_id
    container_payload["managed_user_id"] = managed_user_id
    assert prepare_key(host_payload)["status"] == "SUCCEEDED"
    assert prepare_key(container_payload)["status"] == "SUCCEEDED"
    records = handlers.validate_approved_ssh_key_records(
        [str(host_payload["record_id"]), str(container_payload["record_id"])]
    )
    request_id = str(uuid.uuid4())

    with handlers.activation_key_bundles(request_id, records) as (host_path, container_path):
        assert host_path.read_text(encoding="utf-8") == f"{host_public_key}\n"
        assert container_path.read_text(encoding="utf-8") == f"{container_public_key}\n"
        assert host_public_key not in container_path.read_text(encoding="utf-8")
        assert container_public_key not in host_path.read_text(encoding="utf-8")
        assert host_path.stat().st_mode & 0o777 == 0o600
        assert container_path.stat().st_mode & 0o777 == 0o600
        argv = handlers.build_user_activate_argv("origin-pilot", host_path, container_path)
        assert argv == [
            handlers.SCRIPT_ALLOWLIST["h100-user-create"],
            "--activate",
            "origin-pilot",
            "--host-public-key-file",
            str(host_path),
            "--container-public-key-file",
            str(container_path),
            "--confirm-activate",
            "origin-pilot",
        ]

    assert not (controlled_staging / f"{request_id}.host.pub").exists()
    assert not (controlled_staging / f"{request_id}.container.pub").exists()


def test_activation_bundle_both_scope_covers_both_targets(
    controlled_staging: Path,
    worker_public_key: str,
) -> None:
    payload = ssh_key_prepare_payload(worker_public_key, scope="BOTH")
    assert prepare_key(payload)["status"] == "SUCCEEDED"
    records = handlers.validate_approved_ssh_key_records([str(payload["record_id"])])
    request_id = str(uuid.uuid4())

    with handlers.activation_key_bundles(request_id, records) as (host_path, container_path):
        expected = f"{worker_public_key}\n"
        assert host_path.read_text(encoding="utf-8") == expected
        assert container_path.read_text(encoding="utf-8") == expected

    assert sorted(item.name for item in controlled_staging.iterdir()) == sorted(
        [f"{payload['record_id']}.meta.json", f"{payload['record_id']}.pub"]
    )


def test_activate_argv_rejects_arbitrary_or_malformed_bundle_paths(
    controlled_staging: Path,
) -> None:
    request_id = str(uuid.uuid4())
    valid_host = controlled_staging / f"{request_id}.host.pub"
    valid_container = controlled_staging / f"{request_id}.container.pub"
    with pytest.raises(handlers.LifecycleValidationError, match="ARBITRARY_PATH_REJECTED"):
        handlers.build_user_activate_argv("origin-pilot", Path("/etc/passwd"), valid_container)
    with pytest.raises(handlers.LifecycleValidationError, match="ARBITRARY_PATH_REJECTED"):
        handlers.build_user_activate_argv(
            "origin-pilot", controlled_staging / f"{'a' * 36}.host.pub", valid_container
        )
    with pytest.raises(handlers.LifecycleValidationError, match="ACTIVATE_TARGET_REJECTED"):
        handlers.build_user_activate_argv("other-user", valid_host, valid_container)


def test_worker_prepares_root_controlled_key_and_sidecar_idempotently(
    controlled_staging: Path, worker_public_key: str
) -> None:
    payload = ssh_key_prepare_payload(worker_public_key)

    first = prepare_key(payload)
    second = prepare_key(payload)

    assert first["status"] == "SUCCEEDED"
    assert first["idempotent_replay"] is False
    assert second["status"] == "SUCCEEDED"
    assert second["idempotent_replay"] is True
    record_id = str(payload["record_id"])
    key_path = controlled_staging / f"{record_id}.pub"
    metadata_path = controlled_staging / f"{record_id}.meta.json"
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert metadata_path.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE KEY" not in key_path.read_text(encoding="utf-8").upper()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["operation_id"] == payload["operation_id"]
    assert metadata["managed_user_id"] == payload["managed_user_id"]
    assert metadata["scope"] == "BOTH"
    assert (
        handlers.validate_approved_ssh_key_records([record_id])[0]["fingerprint_sha256"]
        == payload["fingerprint_sha256"]
    )


def test_worker_discard_requires_binding_and_is_idempotent(
    controlled_staging: Path, worker_public_key: str
) -> None:
    payload = ssh_key_prepare_payload(worker_public_key)
    assert prepare_key(payload)["status"] == "SUCCEEDED"
    record_id = str(payload["record_id"])
    discard_payload = {
        "record_id": record_id,
        "operation_id": payload["operation_id"],
        "content_sha256": payload["content_sha256"],
    }
    discard_request = request(
        "ssh_key.discard",
        discard_payload,
        approved_by="origin-al",
        idempotency_key=f"ssh-key-discard:{record_id}",
    )

    first = handle(discard_request)
    second = handle(discard_request)

    assert first["status"] == "SUCCEEDED" and first["removed"] is True
    assert second["status"] == "SUCCEEDED" and second["removed"] is False
    assert list(controlled_staging.iterdir()) == []


@pytest.mark.parametrize("unsafe_kind", ["symlink", "writable", "oversized"])
def test_worker_rejects_unsafe_staging_file(
    controlled_staging: Path,
    worker_public_key: str,
    unsafe_kind: str,
) -> None:
    payload = ssh_key_prepare_payload(worker_public_key)
    assert prepare_key(payload)["status"] == "SUCCEEDED"
    key_path = controlled_staging / f"{payload['record_id']}.pub"
    if unsafe_kind == "symlink":
        key_path.unlink()
        key_path.symlink_to("/etc/passwd")
    elif unsafe_kind == "writable":
        key_path.chmod(0o666)
    else:
        key_path.write_bytes(b"x" * (handlers.MAX_SSH_KEY_FILE_BYTES + 1))
        key_path.chmod(0o600)

    with pytest.raises(handlers.LifecycleValidationError, match="PUBLIC_KEY_VALIDATION_FAILED"):
        handlers.validate_approved_ssh_key_records([str(payload["record_id"])])


def test_worker_prepare_rejects_private_material_before_write(
    controlled_staging: Path,
) -> None:
    record_id = str(uuid.uuid4())
    payload = {
        "record_id": record_id,
        "operation_id": str(uuid.uuid4()),
        "managed_user_id": str(uuid.uuid4()),
        "username": "origin-pilot",
        "public_key": "-----BEGIN OPENSSH PRIVATE KEY----- forbidden",
        "key_type": "ssh-ed25519",
        "fingerprint_sha256": "SHA256:test",
        "content_sha256": hashlib.sha256(b"test").hexdigest(),
        "scope": "BOTH",
    }
    result = handle(
        request(
            "ssh_key.prepare",
            payload,
            approved_by="origin-al",
            idempotency_key=f"ssh-key-enroll:{record_id}",
        )
    )
    assert result["error"]["code"] == "SSH_PRIVATE_KEY_UPLOAD_REJECTED"
    assert list(controlled_staging.iterdir()) == []


def test_worker_prepare_rejects_private_key_fields_before_write(
    controlled_staging: Path,
    worker_public_key: str,
) -> None:
    payload = ssh_key_prepare_payload(worker_public_key)
    payload["private_key_path"] = "/forbidden"
    result = handle(
        request(
            "ssh_key.prepare",
            payload,
            approved_by="origin-al",
            idempotency_key=f"ssh-key-enroll:{payload['record_id']}",
        )
    )
    assert result["error"]["code"] == "SSH_PRIVATE_KEY_UPLOAD_REJECTED"
    assert list(controlled_staging.iterdir()) == []


def test_activate_dry_run_binds_managed_user_scope_and_staged_state(
    controlled_staging: Path,
    worker_public_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = ssh_key_prepare_payload(worker_public_key)
    assert prepare_key(payload)["status"] == "SUCCEEDED"
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {name: {"integrity_ok": True} for name in handlers.STAGE_REQUIRED_SCRIPTS},
    )
    monkeypatch.setattr(handlers, "_stage_postcondition_summary", lambda _payload: staged_result())
    monkeypatch.setattr(
        handlers,
        "managed_host_ssh_policy",
        lambda username: passing_host_ssh_policy(),
    )
    activate = {
        "managed_user_id": payload["managed_user_id"],
        "approved_ssh_key_record_ids": [payload["record_id"]],
        "expected_state": "STAGED",
        "approval_reference": "portal3d-r-test-v1",
    }

    result = handle(request("user.activate", activate, dry_run=True))
    wrong_owner = handle(
        request(
            "user.activate",
            {**activate, "managed_user_id": str(uuid.uuid4())},
            dry_run=True,
        )
    )

    assert result["status"] == "DRY_RUN"
    assert result["activate_status"] == "READY"
    assert result["execution_enabled"] is False
    assert result["host_authorized_keys_install"] == "PLANNED"
    assert result["container_authorized_keys_install"] == "PLANNED"
    assert result["host_authorized_keys_current"] == "ABSENT"
    assert result["container_authorized_keys_current"] == "ABSENT"
    assert result["host_ssh_policy"] == passing_host_ssh_policy()
    assert wrong_owner["error"]["code"] == "PUBLIC_KEY_RECORD_OWNER_MISMATCH"


def test_activate_dry_run_rejects_incomplete_scope(
    controlled_staging: Path,
    worker_public_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = ssh_key_prepare_payload(worker_public_key, scope="HOST")
    assert prepare_key(payload)["status"] == "SUCCEEDED"
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {name: {"integrity_ok": True} for name in handlers.STAGE_REQUIRED_SCRIPTS},
    )
    monkeypatch.setattr(handlers, "_stage_postcondition_summary", lambda _payload: staged_result())
    monkeypatch.setattr(
        handlers,
        "managed_host_ssh_policy",
        lambda username: passing_host_ssh_policy(),
    )
    result = handle(
        request(
            "user.activate",
            {
                "managed_user_id": payload["managed_user_id"],
                "approved_ssh_key_record_ids": [payload["record_id"]],
                "expected_state": "STAGED",
                "approval_reference": "portal3d-r-test-v1",
            },
            dry_run=True,
        )
    )
    assert result["error"]["code"] == "SSH_KEY_SCOPE_INCOMPLETE"


def test_activate_dry_run_reports_exact_target_scope_mapping(
    controlled_staging: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_user_id = str(uuid.uuid4())
    host_payload = ssh_key_prepare_payload(
        generate_worker_public_key(tmp_path, "dry-run-host-key"), scope="HOST"
    )
    container_payload = ssh_key_prepare_payload(
        generate_worker_public_key(tmp_path, "dry-run-container-key"), scope="CONTAINER"
    )
    for payload in (host_payload, container_payload):
        payload["managed_user_id"] = managed_user_id
        assert prepare_key(payload)["status"] == "SUCCEEDED"
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {name: {"integrity_ok": True} for name in handlers.STAGE_REQUIRED_SCRIPTS},
    )
    monkeypatch.setattr(handlers, "_stage_postcondition_summary", lambda _payload: staged_result())
    monkeypatch.setattr(
        handlers,
        "managed_host_ssh_policy",
        lambda username: passing_host_ssh_policy(),
    )

    result = handle(
        request(
            "user.activate",
            {
                "managed_user_id": managed_user_id,
                "approved_ssh_key_record_ids": [
                    host_payload["record_id"],
                    container_payload["record_id"],
                ],
                "expected_state": "STAGED",
                "approval_reference": "portal3d-r-scope-test-v1",
            },
            dry_run=True,
        )
    )

    assert result["activate_cli_contract"] == "TARGET_SCOPED_ROOT_CONTROLLED_BUNDLES"
    assert [item["record_id"] for item in result["host_authorized_keys_plan"]] == [
        host_payload["record_id"]
    ]
    assert [item["record_id"] for item in result["container_authorized_keys_plan"]] == [
        container_payload["record_id"]
    ]


def managed_container_inspect(*, state: str = "STOPPED") -> dict[str, object]:
    running = state == "RUNNING"
    return {
        "status": "OK",
        "container": {
            "name": "/gpu-dev-origin-pilot",
            "owner": "origin-pilot",
            "image": "h100-local/dev-container:ubuntu24.04-origin-pilot-20260804",
            "image_id": "sha256:" + "a" * 64,
            "cpu_limit": 8.0,
            "memory_limit_bytes": 32 * 1024**3,
            "pids_limit": 4096,
            "ssh_port": "22023",
            "ssh_host_ip": "0.0.0.0",  # noqa: S104 -- approved publish contract fixture
            "privileged": False,
            "network_mode": "bridge",
            "pid_mode": "",
            "ipc_mode": "private",
            "gpu": "NONE",
            "docker_socket_mounted": False,
            "state": {
                "Running": running,
                "Status": "running" if running else "exited",
                "Health": {"Status": "healthy" if running else "none"},
            },
            "mounts": [
                {
                    "Type": "bind",
                    "Source": "/srv/gpu-platform/users/origin-pilot/home",
                    "Destination": "/home/origin-pilot",
                    "RW": True,
                },
                {
                    "Type": "bind",
                    "Source": "/srv/gpu-platform/users/origin-pilot/workspace",
                    "Destination": "/workspace",
                    "RW": True,
                },
                {
                    "Type": "bind",
                    "Source": "/srv/gpu-platform/users/origin-pilot/shared",
                    "Destination": "/shared",
                    "RW": True,
                },
                {
                    "Type": "bind",
                    "Source": "/srv/gpu-platform/container-data/origin-pilot/ssh-host-keys",
                    "Destination": "/etc/ssh/persistent",
                    "RW": True,
                },
            ],
        },
    }


def managed_container_start_payload() -> dict[str, object]:
    return {
        "name": "gpu-dev-origin-pilot",
        "username": "origin-pilot",
        "managed_user_id": str(uuid.uuid4()),
        "expected_compute_state": "ACTIVE",
        "expected_container_state": "STOPPED",
        "expected_ssh_key_state": "INSTALLED",
    }


def configure_managed_container_start_preconditions(
    monkeypatch: pytest.MonkeyPatch,
    inspected: dict[str, object],
) -> None:
    fingerprint = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    monkeypatch.setattr(
        handlers,
        "_read_active_pilot_state",
        lambda _username: {
            "UID": "20001",
            "GID": "20001",
            "CONTAINER_KEY_FINGERPRINTS": fingerprint,
        },
    )
    monkeypatch.setattr(
        handlers.pwd,
        "getpwnam",
        lambda _username: SimpleNamespace(pw_uid=20001, pw_gid=20001, pw_shell="/bin/bash"),
    )
    monkeypatch.setattr(
        handlers,
        "run_fixed",
        lambda *_args, **_kwargs: {"ok": True, "stdout": "origin-pilot L 2026-08-08 0 99999 7 -1"},
    )
    monkeypatch.setattr(handlers, "_installed_key_fingerprints", lambda *_args: [fingerprint])
    monkeypatch.setattr(handlers, "containers_inspect", lambda _payload: inspected)


def test_managed_container_start_preconditions_accept_only_exact_safe_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspected = managed_container_inspect()
    configure_managed_container_start_preconditions(monkeypatch, inspected)
    result = handlers._verify_managed_container_start_preconditions(
        managed_container_start_payload()
    )
    assert result["container"]["gpu"] == "NONE"


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("owner", "other-user"),
        ("privileged", True),
        ("network_mode", "host"),
        ("pid_mode", "host"),
        ("ipc_mode", "host"),
        ("gpu", "REQUESTED"),
        ("docker_socket_mounted", True),
        ("ssh_host_ip", "10.82.36.1"),
    ],
)
def test_managed_container_start_rejects_unsafe_runtime_properties(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    unsafe_value: object,
) -> None:
    inspected = managed_container_inspect()
    container = inspected["container"]
    assert isinstance(container, dict)
    container[field] = unsafe_value
    configure_managed_container_start_preconditions(monkeypatch, inspected)
    with pytest.raises(
        handlers.LifecycleValidationError, match="CONTAINER_START_SECURITY_REJECTED"
    ):
        handlers._verify_managed_container_start_preconditions(managed_container_start_payload())


def test_managed_container_start_rejects_extra_or_sensitive_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspected = managed_container_inspect()
    container = inspected["container"]
    assert isinstance(container, dict)
    mounts = container["mounts"]
    assert isinstance(mounts, list)
    mounts.append(
        {
            "Type": "bind",
            "Source": "/var/run/docker.sock",
            "Destination": "/var/run/docker.sock",
            "RW": True,
        }
    )
    configure_managed_container_start_preconditions(monkeypatch, inspected)
    with pytest.raises(
        handlers.LifecycleValidationError, match="CONTAINER_START_SECURITY_REJECTED"
    ):
        handlers._verify_managed_container_start_preconditions(managed_container_start_payload())


def test_managed_container_start_schema_rejects_cross_identity_and_staged_state() -> None:
    payload = managed_container_start_payload()
    with pytest.raises(ValueError, match="CONTAINER_OWNERSHIP_REJECTED"):
        validate_payload("container.start", {**payload, "name": "gpu-dev-other-user"})
    with pytest.raises(ValueError, match="CONTAINER_START_STATE_REJECTED"):
        validate_payload("container.start", {**payload, "expected_compute_state": "STAGED"})


def test_managed_container_start_executes_fixed_scripts_and_stops_on_unsafe_postcondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = managed_container_start_payload()
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {
            "h100-container-start": {"integrity_ok": True},
            "h100-container-stop": {"integrity_ok": True},
        },
    )
    monkeypatch.setattr(
        handlers, "_verify_managed_container_start_preconditions", lambda _payload: {}
    )
    calls: list[list[str]] = []

    def execute(argv: list[str], timeout: float) -> dict[str, object]:
        del timeout
        calls.append(argv)
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(handlers, "run_allowlisted_script", execute)
    unsafe = managed_container_inspect(state="RUNNING")
    unsafe_container = unsafe["container"]
    assert isinstance(unsafe_container, dict)
    unsafe_container["gpu"] = "REQUESTED"
    safely_stopped = managed_container_inspect()
    inspections = iter((unsafe, safely_stopped))
    monkeypatch.setattr(handlers, "containers_inspect", lambda _payload: next(inspections))
    token = str(uuid.uuid4())
    result = handle(
        request(
            "container.start",
            payload,
            approved_by="origin-al",
            idempotency_key=f"container-start:{payload['managed_user_id']}:{token}",
        )
    )

    assert result["error"]["code"] == "CONTAINER_START_POSTCONDITION_FAILED"
    assert calls == [
        [handlers.SCRIPT_ALLOWLIST["h100-container-start"], "origin-pilot"],
        [handlers.SCRIPT_ALLOWLIST["h100-container-stop"], "origin-pilot"],
    ]


def test_managed_container_start_reports_unproven_stop_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = managed_container_start_payload()
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {
            "h100-container-start": {"integrity_ok": True},
            "h100-container-stop": {"integrity_ok": True},
        },
    )
    monkeypatch.setattr(
        handlers, "_verify_managed_container_start_preconditions", lambda _payload: {}
    )
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: {"ok": True, "stdout": "", "stderr": ""},
    )
    unsafe = managed_container_inspect(state="RUNNING")
    unsafe_container = unsafe["container"]
    assert isinstance(unsafe_container, dict)
    unsafe_container["gpu"] = "REQUESTED"
    monkeypatch.setattr(handlers, "containers_inspect", lambda _payload: unsafe)

    token = str(uuid.uuid4())
    result = handle(
        request(
            "container.start",
            payload,
            approved_by="origin-al",
            idempotency_key=f"container-start:{payload['managed_user_id']}:{token}",
        )
    )

    assert result["error"]["code"] == "CONTAINER_STOP_RECOVERY_FAILED"


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


def managed_job_payload(*, username: str = "origin-pilot", uid: int = 20001) -> dict[str, object]:
    portal_job_id = str(uuid.uuid4())
    script = "#!/bin/bash\nset -eu\nwhoami\nid\n"
    return {
        "portal_job_id": portal_job_id,
        "managed_user_id": "3b95b4f0-95d9-444a-8f0b-46288195a807",
        "lease_id": str(uuid.uuid4()),
        "username": username,
        "uid": uid,
        "gid": uid,
        "name": "portal-job",
        "script_relative_path": f"workspace/.portal/job-scripts/{portal_job_id}.sh",
        "script_content": script,
        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "workdir_relative_path": "workspace",
        "stdout_relative_path": f"workspace/.portal/jobs/{portal_job_id}.out",
        "stderr_relative_path": f"workspace/.portal/jobs/{portal_job_id}.err",
        "cpus": 1,
        "memory_mb": 1024,
        "gpu_count": 1,
        "time_limit_seconds": 600,
        "lease_deadline_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "slurm_account": "company",
        "slurm_qos": "general",
        "max_gpu": 1,
        "image_ref": None,
    }


def managed_terminal_payload(
    *, username: str = "origin-pilot", uid: int = 20001
) -> dict[str, object]:
    return {
        "managed_user_id": "3b95b4f0-95d9-444a-8f0b-46288195a807",
        "username": username,
        "uid": uid,
        "gid": uid,
        "name": f"gpu-dev-{username}",
        "lease_id": str(uuid.uuid4()),
        "lease_expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "expected_gpu": "NONE",
        "host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_key_fingerprints": [handlers.PORTAL3E_FINAL_KEY_FINGERPRINT],
        "cols": 120,
        "rows": 32,
    }


def test_portal4a_worker_schema_fixes_job_outputs_and_gpu_limit() -> None:
    payload = managed_job_payload()
    validated = validate_payload("self.job.submit", payload)
    assert validated["stdout_relative_path"] == payload["stdout_relative_path"]
    with pytest.raises(ValueError, match="output path"):
        validate_payload(
            "self.job.submit",
            {**payload, "stdout_relative_path": "workspace/other.out"},
        )
    with pytest.raises(ValueError, match="GPU"):
        validate_payload("self.job.submit", {**payload, "gpu_count": 2})
    for changed in (
        {"script_sha256": "0" * 64},
        {"script_relative_path": "workspace/user-selected.sh"},
        {"workdir_relative_path": "workspace/other"},
        {"slurm_account": "platform-admin"},
        {"username": "root", "uid": 0, "gid": 0},
    ):
        with pytest.raises(ValueError):
            validate_payload("self.job.submit", {**payload, **changed})


def test_multi_user_worker_schema_accepts_origin_pilot2_owner_binding() -> None:
    job = validate_payload(
        "self.job.submit",
        managed_job_payload(username="origin-pilot2", uid=20002),
    )
    terminal = validate_payload(
        "self.container.terminal",
        managed_terminal_payload(username="origin-pilot2", uid=20002),
    )

    assert job["username"] == terminal["username"] == "origin-pilot2"
    assert job["uid"] == job["gid"] == 20002
    assert terminal["name"] == "gpu-dev-origin-pilot2"
    assert terminal["uid"] == terminal["gid"] == 20002
    argv = worker_terminal._terminal_argv(
        terminal,
        "h100-portal-terminal-00000000-0000-4000-8000-000000000099",
    )
    assert argv[argv.index("--user") + 1] == "20002:20002"
    assert "gpu-dev-origin-pilot2" in argv
    assert argv[0] == "/usr/bin/docker"


def test_web_terminal_schema_binds_owned_container_without_command_fields() -> None:
    payload = managed_terminal_payload()
    validated = validate_payload("self.container.terminal", payload)
    request_id = "00000000-0000-4000-8000-000000000099"
    marker = "h100-portal-terminal-00000000-0000-4000-8000-000000000099"
    assert validated["name"] == "gpu-dev-origin-pilot"
    assert validated["uid"] == validated["gid"] == 20001
    assert validated["expected_gpu"] == "NONE"
    assert validated["host_access"] == "DISABLED_BY_PLATFORM_POLICY"
    assert worker_terminal._terminal_marker(request_id) == marker
    assert worker_terminal._terminal_argv(validated, marker) == [
        "/usr/bin/docker",
        "exec",
        "--detach-keys=ctrl-]",
        "--interactive",
        "--tty",
        "--user",
        "20001:20001",
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/home/origin-pilot",
        "--env",
        "USER=origin-pilot",
        "--env",
        "LOGNAME=origin-pilot",
        "--env",
        "TERM=xterm-256color",
        "gpu-dev-origin-pilot",
        "/bin/bash",
        "-c",
        'exec -a "$1" /bin/bash --login',
        "h100-portal-terminal-wrapper",
        marker,
    ]
    for changed in (
        {"name": "gpu-dev-other-user"},
        {"uid": 0},
        {"expected_gpu": "ALL"},
        {"host_access": "ENABLED"},
        {"command": "id"},
        {"cols": 500},
    ):
        with pytest.raises(ValueError):
            validate_payload("self.container.terminal", {**payload, **changed})
    with pytest.raises(ValueError, match="reserved detach control"):
        worker_terminal._decode_input(
            {
                "type": "input",
                "data_b64": base64.b64encode(b"safe\x1dunsafe").decode("ascii"),
            }
        )


def test_web_terminal_process_discovery_matches_only_fixed_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "h100-portal-terminal-00000000-0000-4000-8000-000000000099"

    def docker(argv, **_kwargs):  # type: ignore[no-untyped-def]
        if argv[1] == "ps":
            return SimpleNamespace(
                returncode=0,
                stdout="gpu-dev-origin-pilot\ngpu-dev-origin-pilot2\nunmanaged\n",
            )
        username = argv[2].removeprefix("gpu-dev-")
        lines = [
            "PID PPID USER COMMAND",
            f"101 10 {username} user-process --login",
            "102 10 root /usr/sbin/sshd -D",
        ]
        if username == "origin-pilot2":
            lines.append(f"100 10 {username} {marker} --login")
        return SimpleNamespace(returncode=0, stdout="\n".join(lines))

    monkeypatch.setattr(worker_terminal.subprocess, "run", docker)
    assert worker_terminal._marked_host_processes(marker) == [(100, marker)]
    assert worker_terminal._marked_host_processes() == [(100, marker)]


def test_web_terminal_preflight_requires_nologin_and_absent_host_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = validate_payload("self.container.terminal", managed_terminal_payload())
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    monkeypatch.setattr(
        worker_terminal,
        "_managed_account",
        lambda _payload: SimpleNamespace(pw_shell="/usr/sbin/nologin", pw_dir=str(host_home)),
    )
    monkeypatch.setattr(
        worker_terminal,
        "_managed_container_security",
        lambda *_args, **_kwargs: {"state": {"Running": True, "Health": {"Status": "healthy"}}},
    )
    monkeypatch.setattr(
        worker_terminal,
        "_installed_key_fingerprints",
        lambda *_args: [handlers.PORTAL3E_FINAL_KEY_FINGERPRINT],
    )
    monkeypatch.setattr(worker_terminal, "PILOT_DATA_ROOT", tmp_path / "users")
    worker_terminal._terminal_preflight(payload)

    host_keys = host_home / ".ssh/authorized_keys"
    host_keys.parent.mkdir()
    host_keys.write_text("ssh-ed25519 forbidden-host-key\n", encoding="utf-8")
    with pytest.raises(handlers.LifecycleValidationError) as rejected:
        worker_terminal._terminal_preflight(payload)
    assert rejected.value.code == "HOST_ACCESS_POLICY_REJECTED"


def test_web_terminal_retries_partial_pty_input_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pieces: list[bytes] = []

    def partial_write(_descriptor: int, data: bytes) -> int:
        count = min(2, len(data))
        pieces.append(data[:count])
        return count

    monkeypatch.setattr(worker_terminal.os, "write", partial_write)
    worker_terminal._write_input(17, b"abcdef")
    assert b"".join(pieces) == b"abcdef"


def test_web_terminal_input_reader_can_stop_with_a_full_queue() -> None:
    worker_socket, client_socket = socket.socketpair()
    incoming: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
    incoming.put({"type": "already-full"})
    stopped = threading.Event()
    reader = threading.Thread(
        target=worker_terminal._input_reader,
        args=(worker_socket, incoming, stopped),
        daemon=True,
    )
    reader.start()
    client_socket.sendall(encode_frame({"type": "resize", "cols": 120, "rows": 32}))
    stopped.set()
    reader.join(timeout=1)
    client_socket.close()
    worker_socket.close()
    assert not reader.is_alive()


def test_worker_routes_terminal_to_stream_handler_without_general_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("H100_PORTAL_WORKER_TESTING", "1")
    payload = managed_terminal_payload()
    envelope = {
        "protocol_version": 1,
        "request_id": str(uuid.uuid4()),
        "operation_type": "self.container.terminal",
        "payload": payload,
        "requested_by": "origin-pilot",
        "approved_by": "origin-pilot",
        "idempotency_key": (
            f"self-container-terminal:3b95b4f0-95d9-444a-8f0b-46288195a807:{uuid.uuid4()}"
        ),
        "dry_run": False,
    }
    called: list[str] = []

    def stream(connection: socket.socket, worker_request: WorkerRequest) -> None:
        called.append(worker_request.operation_type)
        connection.sendall(
            encode_frame(
                {
                    "status": "READY",
                    "type": "ready",
                    "request_id": worker_request.request_id,
                }
            )
        )

    monkeypatch.setattr(server, "serve_terminal_connection", stream)
    worker_socket, client_socket = socket.socketpair()
    encoded = json.dumps(envelope).encode()
    try:
        client_socket.sendall(struct.pack("!I", len(encoded)) + encoded)
        server.process_connection(worker_socket)
        response = decode_frame(client_socket.recv(64 * 1024))
    finally:
        client_socket.close()
    assert response["status"] == "READY"
    assert called == ["self.container.terminal"]


def test_portal4a_worker_rejects_spoofed_self_actor_and_container_actor() -> None:
    self_request = request("self.job.submit", managed_job_payload())
    denied = handle(self_request)
    assert denied["error"]["code"] == "RESOURCE_OWNERSHIP_REJECTED"

    container_payload = {
        "managed_user_id": "3b95b4f0-95d9-444a-8f0b-46288195a807",
        "username": "origin-pilot",
        "uid": 20001,
        "gid": 20001,
        "name": "gpu-dev-origin-pilot",
        "lease_id": str(uuid.uuid4()),
        "lease_expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "expected_gpu": "NONE",
    }
    denied = handle(request("container.start", container_payload))
    assert denied["error"]["code"] == "RESOURCE_OWNERSHIP_REJECTED"


def test_worker_routes_origin_pilot2_self_job_for_matching_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = managed_job_payload(username="origin-pilot2", uid=20002)
    monkeypatch.setattr(
        handlers,
        "_execute_self_job_submit",
        lambda _request, validated: {
            "status": "SUCCEEDED",
            "username": validated["username"],
            "uid": validated["uid"],
        },
    )

    result = handle(
        request(
            "self.job.submit",
            payload,
            requested_by="origin-pilot2",
            approved_by="origin-pilot2",
        )
    )

    assert result == {"status": "SUCCEEDED", "username": "origin-pilot2", "uid": 20002}


def test_portal4a_component_open_rejects_symlink_and_pins_staged_inode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    uid = os.getuid()
    gid = os.getgid()
    root = tmp_path / "users" / "origin-pilot"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    script = workspace / "job.sh"
    script.write_bytes(b"#!/bin/sh\necho approved\n")
    (workspace / "escape").symlink_to("/etc")
    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", tmp_path / "users")

    with pytest.raises(handlers.LifecycleValidationError) as escaped:
        handlers._managed_user_path(
            "origin-pilot",
            "workspace/escape/passwd",
            directory=False,
            uid=uid,
            gid=gid,
        )
    assert escaped.value.code == "SYMLINK_ESCAPE_REJECTED"

    portal_job_id = str(uuid.uuid4())
    staged, descriptor = handlers._stage_user_job_script(
        {
            "portal_job_id": portal_job_id,
            "username": "origin-pilot",
            "uid": uid,
            "gid": gid,
        },
        script.read_bytes(),
    )
    try:
        replacement = staged.with_suffix(".replacement")
        replacement.write_bytes(b"#!/bin/sh\necho replaced\n")
        os.replace(replacement, staged)
        with open(f"/proc/self/fd/{descriptor}", "rb") as pinned:
            assert pinned.read() == b"#!/bin/sh\necho approved\n"
        assert staged.read_bytes() == b"#!/bin/sh\necho replaced\n"
    finally:
        os.close(descriptor)


def test_portal4a_setpriv_uses_fixed_argv_and_never_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def completed(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="123\n", stderr="")

    monkeypatch.setitem(handlers.BINARIES, "setpriv", "/usr/bin/setpriv")
    monkeypatch.setattr(handlers.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(handlers.subprocess, "run", completed)
    result = handlers._run_as_managed_user(
        {"uid": 20001, "gid": 20001},
        ["/usr/bin/sbatch", "--parsable", "/proc/self/fd/9"],
        timeout=30,
        pass_fds=(9,),
    )
    assert result["ok"] is True
    assert captured["argv"] == [
        "/usr/bin/setpriv",
        "--reuid=20001",
        "--regid=20001",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--bounding-set=-all",
        "--",
        "/usr/bin/sbatch",
        "--parsable",
        "/proc/self/fd/9",
    ]
    assert captured["shell"] is False
    assert captured["pass_fds"] == (9,)


def test_job_script_is_staged_then_only_fixed_sbatch_runs_as_target_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = validate_payload(
        "self.job.submit",
        managed_job_payload(username="origin-pilot2", uid=20002),
    )
    users_root = tmp_path / "users"
    output_parent = users_root / "origin-pilot2/workspace/.portal/jobs"
    output_parent.mkdir(parents=True)
    workdir = users_root / "origin-pilot2/workspace"
    staged = tmp_path / "staged-job.sh"
    captured: dict[str, object] = {}

    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", users_root)
    monkeypatch.setattr(handlers, "_managed_slurm_security_preflight", lambda _payload: None)
    monkeypatch.setattr(
        handlers,
        "_managed_user_path",
        lambda *_args, **_kwargs: workdir,
    )
    monkeypatch.setattr(
        handlers,
        "_ensure_managed_owned_directory",
        lambda *_args, **_kwargs: output_parent,
    )

    def stage(_payload, content):  # type: ignore[no-untyped-def]
        captured["content"] = content
        staged.write_bytes(content)
        return staged, os.open(staged, os.O_RDONLY)

    def run_as_user(bound_payload, command, timeout, *, pass_fds=()):  # type: ignore[no-untyped-def]
        captured["payload"] = bound_payload
        captured["command"] = command
        captured["timeout"] = timeout
        captured["pass_fds"] = pass_fds
        return {"ok": True, "stdout": "42\n", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(handlers, "_stage_user_job_script", stage)
    monkeypatch.setattr(handlers, "_run_as_managed_user", run_as_user)
    monkeypatch.setattr(handlers, "_slurm_job_owner", lambda _job_id: "origin-pilot2")

    result = handlers._execute_self_job_submit(
        request("self.job.submit", approved_by="origin-pilot2"), payload
    )

    assert result["status"] == "SUCCEEDED"
    assert captured["content"] == payload["script_content"].encode()
    assert captured["payload"]["uid"] == captured["payload"]["gid"] == 20002
    command = captured["command"]
    assert command[0] == "/usr/bin/sbatch"
    assert command[-1].startswith("/proc/self/fd/")
    assert payload["script_content"] not in command
    assert not any(item in {"bash", "sh", "sudo"} for item in command)


def test_portal4a_gpu_job_mounts_only_owned_root_and_disables_host_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = managed_job_payload()
    payload["image_ref"] = (
        "nvcr.io#nvidia/cuda:13.2.0-base-ubuntu24.04@"
        "sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a"
    )
    payload["lease_deadline_at"] = "2026-08-14T05:24:55.083442+00:00"
    users_root = tmp_path / "users"
    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", users_root)
    argv = handlers._managed_sbatch_argv(
        payload,
        workdir=users_root / "origin-pilot/workspace",
        stdout=users_root / "origin-pilot/workspace/.portal/jobs/job.out",
        stderr=users_root / "origin-pilot/workspace/.portal/jobs/job.err",
        staged_descriptor=9,
    )
    owned_root = users_root / "origin-pilot"
    assert "--gres=gpu:1" in argv
    assert "--deadline=2026-08-14T05:24:55" in argv
    assert not any(".083442" in item for item in argv)
    assert f"--container-image={payload['image_ref']}" in argv
    assert "--no-container-mount-home" in argv
    assert f"--container-mounts={owned_root}:{owned_root}" in argv
    assert argv[-1] == "/proc/self/fd/9"
    assert not any("/home/origin-pilot" in item for item in argv)


def test_portal4a_host_revoke_rolls_back_shell_and_key_on_postcondition_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    username = "origin-pilot"
    fingerprint = "SHA256:nek6vyEb3GT+UJAcY5y/8PgY4achF2ouNy+8C2JqUVc"
    managed_home = tmp_path / "home"
    host_keys = managed_home / username / ".ssh/authorized_keys"
    host_keys.parent.mkdir(parents=True)
    host_keys.write_text("ssh-ed25519 fixture portal4a\n", encoding="utf-8")
    container_keys = tmp_path / "users" / username / "home/.ssh/authorized_keys"
    container_keys.parent.mkdir(parents=True)
    container_keys.write_text("ssh-ed25519 fixture portal4a\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "MANAGED_HOME_ROOT", managed_home)
    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", tmp_path / "users")
    monkeypatch.setattr(handlers, "PLATFORM_BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(handlers, "_copy_root_only", lambda _source, _destination: "a" * 64)
    shell = {"value": "/bin/bash"}
    monkeypatch.setattr(
        handlers,
        "_managed_account",
        lambda _payload: SimpleNamespace(pw_shell=shell["value"]),
    )
    monkeypatch.setattr(
        handlers.pwd,
        "getpwnam",
        lambda _username: SimpleNamespace(pw_shell=shell["value"]),
    )
    monkeypatch.setattr(
        handlers,
        "_installed_key_fingerprints",
        lambda _path, _uid, _gid: [fingerprint],
    )
    monkeypatch.setattr(handlers, "_sshd_effective_config", lambda _username: {"policy": "ok"})

    def fixed(binary, args, **_kwargs):  # type: ignore[no-untyped-def]
        assert binary == "usermod"
        shell["value"] = args[1]
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(handlers, "run_fixed", fixed)
    monkeypatch.setattr(
        handlers,
        "validate_ssh_policy_no_regression",
        lambda *_args: (_ for _ in ()).throw(
            handlers.LifecycleValidationError("SSH_POLICY_REGRESSION", "fixture regression")
        ),
    )
    payload = {
        "managed_user_id": "3b95b4f0-95d9-444a-8f0b-46288195a807",
        "username": username,
        "uid": 20001,
        "gid": 20001,
        "key_record_id": "7427da72-37b9-4ac2-8ada-2f0c83b7718e",
        "key_fingerprint": fingerprint,
        "container_name": "gpu-dev-origin-pilot",
        "expected_scope": "BOTH",
    }
    result = handlers._execute_host_access_revoke(
        request("host_access.revoke_managed_user"), payload
    )
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "SSH_POLICY_REGRESSION"
    assert result["error"]["rollback_status"] == "ROLLED_BACK"
    assert shell["value"] == "/bin/bash"
    assert host_keys.read_text(encoding="utf-8") == "ssh-ed25519 fixture portal4a\n"
    assert not list(host_keys.parent.glob(".authorized_keys.portal4a-*"))


def portal4a_restore_payload() -> dict[str, object]:
    return {
        "restore_request_id": str(uuid.uuid4()),
        "managed_user_id": "3b95b4f0-95d9-444a-8f0b-46288195a807",
        "username": "origin-pilot",
        "uid": 20001,
        "gid": 20001,
        "container_name": "gpu-dev-origin-pilot",
        "expected_gpu": "NONE",
        "host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_key_fingerprints": [handlers.PORTAL3E_FINAL_KEY_FINGERPRINT],
    }


def portal4a_recycle_payload() -> dict[str, object]:
    payload = {
        **portal4a_restore_payload(),
        "lease_id": str(uuid.uuid4()),
        "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    }
    payload.pop("restore_request_id")
    return payload


def test_portal4a_restore_is_idempotent_after_worker_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = portal4a_restore_payload()
    active = tmp_path / "users/origin-pilot/home/.ssh/authorized_keys"
    active.parent.mkdir(parents=True)
    active.write_text("ssh-ed25519 fixture\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", tmp_path / "users")
    monkeypatch.setattr(handlers, "MANAGED_HOME_ROOT", tmp_path / "host-home")
    monkeypatch.setattr(
        handlers,
        "_managed_account",
        lambda _payload: SimpleNamespace(pw_shell="/usr/sbin/nologin"),
    )
    monkeypatch.setattr(
        handlers,
        "_installed_key_fingerprints",
        lambda *_args: [handlers.PORTAL3E_FINAL_KEY_FINGERPRINT],
    )
    monkeypatch.setattr(
        handlers,
        "_managed_container_security",
        lambda *_args, **_kwargs: {"state": {"Running": True}},
    )
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: pytest.fail("idempotent replay must not restart the container"),
    )
    result = handlers._execute_resource_restore(request("resource.restore"), payload)
    assert result["status"] == "SUCCEEDED"
    assert result["idempotent_replay"] is True
    assert result["container_key_fingerprints"] == [handlers.PORTAL3E_FINAL_KEY_FINGERPRINT]


def test_portal4a_restore_rolls_back_container_and_key_after_postcondition_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = portal4a_restore_payload()
    suspended = tmp_path / "users/origin-pilot/home/.ssh/authorized_keys.portal-recycle"
    suspended.parent.mkdir(parents=True)
    suspended.write_text("ssh-ed25519 fixture\n", encoding="utf-8")
    active = suspended.with_name("authorized_keys")
    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", tmp_path / "users")
    monkeypatch.setattr(handlers, "MANAGED_HOME_ROOT", tmp_path / "host-home")
    monkeypatch.setattr(
        handlers,
        "_managed_account",
        lambda _payload: SimpleNamespace(pw_shell="/usr/sbin/nologin"),
    )
    monkeypatch.setattr(
        handlers,
        "_installed_key_fingerprints",
        lambda *_args: [handlers.PORTAL3E_FINAL_KEY_FINGERPRINT],
    )
    security_calls = 0

    def security(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal security_calls
        security_calls += 1
        if security_calls == 1:
            return {"state": {"Running": False}}
        raise handlers.LifecycleValidationError(
            "CONTAINER_SECURITY_REJECTED", "fixture postcondition failure"
        )

    monkeypatch.setattr(handlers, "_managed_container_security", security)
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {
            "h100-container-start": {"integrity_ok": True},
            "h100-container-stop": {"integrity_ok": True},
        },
    )
    scripts: list[str] = []

    def run_script(argv, **_kwargs):  # type: ignore[no-untyped-def]
        scripts.append(argv[0])
        return {"ok": True}

    monkeypatch.setattr(handlers, "run_allowlisted_script", run_script)
    result = handlers._execute_resource_restore(request("resource.restore"), payload)
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "CONTAINER_SECURITY_REJECTED"
    assert result["error"]["rollback_status"] == "ROLLED_BACK"
    assert scripts == [
        handlers.SCRIPT_ALLOWLIST["h100-container-start"],
        handlers.SCRIPT_ALLOWLIST["h100-container-stop"],
    ]
    assert suspended.exists()
    assert not active.exists()


def test_portal4a_restore_stops_partial_start_before_resuspending_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = portal4a_restore_payload()
    suspended = tmp_path / "users/origin-pilot/home/.ssh/authorized_keys.portal-recycle"
    suspended.parent.mkdir(parents=True)
    suspended.write_text("ssh-ed25519 fixture\n", encoding="utf-8")
    active = suspended.with_name("authorized_keys")
    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", tmp_path / "users")
    monkeypatch.setattr(handlers, "MANAGED_HOME_ROOT", tmp_path / "host-home")
    monkeypatch.setattr(
        handlers,
        "_managed_account",
        lambda _payload: SimpleNamespace(pw_shell="/usr/sbin/nologin"),
    )
    monkeypatch.setattr(
        handlers,
        "_installed_key_fingerprints",
        lambda *_args: [handlers.PORTAL3E_FINAL_KEY_FINGERPRINT],
    )
    monkeypatch.setattr(
        handlers,
        "_managed_container_security",
        lambda *_args, **_kwargs: {"state": {"Running": False}},
    )
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {
            "h100-container-start": {"integrity_ok": True},
            "h100-container-stop": {"integrity_ok": True},
        },
    )
    scripts: list[str] = []

    def run_script(argv, **_kwargs):  # type: ignore[no-untyped-def]
        scripts.append(argv[0])
        return {"ok": argv[0] == handlers.SCRIPT_ALLOWLIST["h100-container-stop"]}

    monkeypatch.setattr(handlers, "run_allowlisted_script", run_script)
    result = handlers._execute_resource_restore(request("resource.restore"), payload)
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "CONTAINER_START_FAILED"
    assert result["error"]["rollback_status"] == "ROLLED_BACK"
    assert scripts == [
        handlers.SCRIPT_ALLOWLIST["h100-container-start"],
        handlers.SCRIPT_ALLOWLIST["h100-container-stop"],
    ]
    assert suspended.exists()
    assert not active.exists()


def test_portal4a_recycle_rejects_unapproved_key_before_runtime_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = portal4a_recycle_payload()
    active = tmp_path / "users/origin-pilot/home/.ssh/authorized_keys"
    active.parent.mkdir(parents=True)
    active.write_text("ssh-ed25519 fixture\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", tmp_path / "users")
    monkeypatch.setattr(handlers, "MANAGED_HOME_ROOT", tmp_path / "host-home")
    monkeypatch.setattr(
        handlers,
        "_managed_account",
        lambda _payload: SimpleNamespace(pw_shell="/usr/sbin/nologin"),
    )
    monkeypatch.setattr(
        handlers,
        "_installed_key_fingerprints",
        lambda *_args: ["SHA256:unapproved"],
    )
    monkeypatch.setattr(
        handlers,
        "_active_user_slurm_jobs",
        lambda *_args: pytest.fail("key binding must be checked before Slurm changes"),
    )
    monkeypatch.setattr(
        handlers,
        "_managed_container_security",
        lambda *_args, **_kwargs: pytest.fail(
            "key binding must be checked before container changes"
        ),
    )
    result = handlers._execute_resource_recycle(request("resource.recycle"), payload)
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "CONTAINER_KEY_BINDING_REJECTED"
    assert active.exists()


def test_recycle_suspends_new_ssh_access_before_container_stop_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = portal4a_recycle_payload()
    active = tmp_path / "users/origin-pilot/home/.ssh/authorized_keys"
    active.parent.mkdir(parents=True)
    active.write_text("ssh-ed25519 fixture\n", encoding="utf-8")
    suspended = active.with_name("authorized_keys.portal-recycle")
    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", tmp_path / "users")
    monkeypatch.setattr(handlers, "MANAGED_HOME_ROOT", tmp_path / "host-home")
    monkeypatch.setattr(
        handlers,
        "_managed_account",
        lambda _payload: SimpleNamespace(pw_shell="/usr/sbin/nologin"),
    )
    monkeypatch.setattr(
        handlers,
        "_installed_key_fingerprints",
        lambda *_args: [handlers.PORTAL3E_FINAL_KEY_FINGERPRINT],
    )
    monkeypatch.setattr(handlers, "_active_user_slurm_jobs", lambda _username: [])
    monkeypatch.setattr(
        handlers,
        "_managed_container_security",
        lambda *_args, **_kwargs: {"state": {"Running": True}},
    )
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {"h100-container-stop": {"integrity_ok": True}},
    )
    secret_marker = "fixture-secret-must-not-leak"
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: {
            "ok": False,
            "exit_code": 1,
            "stderr": secret_marker,
        },
    )

    result = handlers._execute_resource_recycle(request("resource.recycle"), payload)

    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "CONTAINER_STOP_FAILED"
    assert result["first_failed_step"] == "CONTAINER_STOP"
    assert result["container_key_state"] == "SUSPENDED_BY_RECYCLE"
    assert result["new_access"] == "DENIED"
    assert result["cleanup_retryable"] is True
    assert result["data_preserved"] is True
    assert not active.exists()
    assert suspended.exists()
    assert secret_marker not in json.dumps(result)


def test_recycle_retry_after_stop_failure_is_idempotent_and_preserves_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = portal4a_recycle_payload()
    suspended = tmp_path / "users/origin-pilot/home/.ssh/authorized_keys.portal-recycle"
    suspended.parent.mkdir(parents=True)
    suspended.write_text("ssh-ed25519 fixture\n", encoding="utf-8")
    active = suspended.with_name("authorized_keys")
    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", tmp_path / "users")
    monkeypatch.setattr(handlers, "MANAGED_HOME_ROOT", tmp_path / "host-home")
    monkeypatch.setattr(
        handlers,
        "_managed_account",
        lambda _payload: SimpleNamespace(pw_shell="/usr/sbin/nologin"),
    )
    monkeypatch.setattr(
        handlers,
        "_installed_key_fingerprints",
        lambda *_args: [handlers.PORTAL3E_FINAL_KEY_FINGERPRINT],
    )
    monkeypatch.setattr(handlers, "_active_user_slurm_jobs", lambda _username: [])
    running = {"value": True}

    def security(*_args, require_running=None, **_kwargs):  # type: ignore[no-untyped-def]
        if require_running is False:
            assert running["value"] is False
        return {"state": {"Running": running["value"]}}

    monkeypatch.setattr(handlers, "_managed_container_security", security)
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {"h100-container-stop": {"integrity_ok": True}},
    )
    script_calls = 0

    def stop(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal script_calls
        script_calls += 1
        running["value"] = False
        return {"ok": True}

    monkeypatch.setattr(handlers, "run_allowlisted_script", stop)
    first = handlers._execute_resource_recycle(request("resource.recycle"), payload)
    second = handlers._execute_resource_recycle(request("resource.recycle"), payload)

    assert first["status"] == second["status"] == "SUCCEEDED"
    assert first["container_state"] == second["container_state"] == "STOPPED"
    assert first["container_key_state"] == "SUSPENDED_BY_RECYCLE"
    assert second["new_access"] == "DENIED"
    assert first["data_preserved"] is second["data_preserved"] is True
    assert script_calls == 1
    assert suspended.exists()
    assert not active.exists()


def test_recycle_running_job_uses_controlled_cancel_and_preserves_history_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = portal4a_recycle_payload()
    active = tmp_path / "users/origin-pilot/home/.ssh/authorized_keys"
    active.parent.mkdir(parents=True)
    active.write_text("ssh-ed25519 fixture\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "PILOT_DATA_ROOT", tmp_path / "users")
    monkeypatch.setattr(handlers, "MANAGED_HOME_ROOT", tmp_path / "host-home")
    monkeypatch.setattr(
        handlers,
        "_managed_account",
        lambda _payload: SimpleNamespace(pw_shell="/usr/sbin/nologin"),
    )
    monkeypatch.setattr(
        handlers,
        "_installed_key_fingerprints",
        lambda *_args: [handlers.PORTAL3E_FINAL_KEY_FINGERPRINT],
    )
    job_queries = 0

    def jobs(_username: str) -> list[tuple[int, str]]:
        nonlocal job_queries
        job_queries += 1
        return [(701, "RUNNING")] if job_queries == 1 else []

    monkeypatch.setattr(handlers, "_active_user_slurm_jobs", jobs)
    monkeypatch.setattr(handlers.time, "sleep", lambda _seconds: None)
    commands: list[tuple[str, list[str]]] = []

    def fixed(binary: str, args: list[str], **_kwargs):  # type: ignore[no-untyped-def]
        commands.append((binary, args))
        return {"ok": True}

    monkeypatch.setattr(handlers, "run_fixed", fixed)
    monkeypatch.setattr(
        handlers,
        "_managed_container_security",
        lambda *_args, **_kwargs: {"state": {"Running": False}},
    )

    result = handlers._execute_resource_recycle(request("resource.recycle"), payload)

    assert result["status"] == "SUCCEEDED"
    assert result["cancelled_running_job_ids"] == [701]
    assert commands == [
        ("scancel", ["--signal=TERM", "--full", "701"]),
        ("scancel", ["701"]),
    ]
    assert result["data_preserved"] is True
    assert result["auto_permanent_delete"] is False


def test_container_stop_script_does_not_require_global_slurm_drain() -> None:
    script = Path(__file__).resolve().parents[4] / "scripts/h100-container-stop"
    source = script.read_text(encoding="utf-8")
    assert "h100_require_slurm_drained" not in source
    assert 'docker stop --time 30 "${container}"' in source
