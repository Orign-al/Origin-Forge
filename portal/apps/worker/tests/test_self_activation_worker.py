import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from h100_portal_worker import handlers
from h100_portal_worker.schemas import WorkerRequest, validate_payload

LEASE_SECONDS = 96 * 60 * 60


def activation_payload() -> dict[str, Any]:
    return {
        "activation_operation_id": str(uuid.uuid4()),
        "managed_user_id": str(uuid.uuid4()),
        "portal_account_id": str(uuid.uuid4()),
        "owner_login": "activation-owner",
        "request_id": str(uuid.uuid4()),
        "plan_id": str(uuid.uuid4()),
        "stage_operation_id": str(uuid.uuid4()),
        "dry_run_operation_id": str(uuid.uuid4()),
        "username": "origin-pilot2",
        "uid": 20002,
        "gid": 20002,
        "project_id": 30002,
        "ssh_port": 22024,
        "container_name": "gpu-dev-origin-pilot2",
        "workspace_path": "/storage/users/20002",
        "development_profile": "STANDARD_8CPU_32GB",
        "container_gpu": 0,
        "slurm_account": "company",
        "slurm_qos": "general",
        "ssh_key_record_ids": [str(uuid.uuid4())],
        "ssh_key_fingerprints": ["SHA256:ActivationOwner+/123"],
        "gpu_max": 1,
        "lease_seconds": LEASE_SECONDS,
        "expected_compute_state": "STAGED",
        "expected_container_state": "STOPPED",
        "expected_host_access": "DISABLED_BY_PLATFORM_POLICY",
        "expected_shell": "/usr/sbin/nologin",
        "expected_password_state": "LOCKED",
        "expected_gpu": "NONE",
        "lease_id": str(uuid.uuid4()),
        "deployment_version": "f" * 40,
    }


def gpu_activation_payload() -> dict[str, Any]:
    return {
        **activation_payload(),
        "development_profile": "GPU_1_8CPU_32GB",
        "container_gpu": 1,
        "expected_gpu": "SLURM_ALLOCATED_1",
    }


def worker_request(
    payload: dict[str, Any],
    *,
    operation: str = "compute.activate.self",
    requested_by: str | None = None,
) -> WorkerRequest:
    rollback = operation.endswith(".rollback")
    return WorkerRequest(
        protocol_version=1,
        request_id=str(uuid.uuid4()),
        operation_type=operation,
        payload=payload,
        requested_by=requested_by or payload["owner_login"],
        approved_by=payload["owner_login"],
        idempotency_key=(
            f"compute-activate-rollback:{payload['activation_operation_id']}"
            if rollback
            else f"compute-activate:{payload['activation_operation_id']}"
        ),
        dry_run=False,
    )


def script_integrity_pass() -> dict[str, dict[str, bool]]:
    return {name: {"integrity_ok": True} for name in handlers.SELF_ACTIVATE_REQUIRED_SCRIPTS}


def test_self_activation_payload_is_closed_and_container_only() -> None:
    payload = activation_payload()
    validated = validate_payload("compute.activate.self", payload)
    assert validated == payload
    with pytest.raises(ValueError, match="ACTIVATION_PAYLOAD_REJECTED"):
        validate_payload("compute.activate.self", {**payload, "container_id": str(uuid.uuid4())})
    with pytest.raises(ValueError, match="ACTIVATION_SECURITY_CONTRACT_REJECTED"):
        validate_payload("compute.activate.self", {**payload, "expected_gpu": "ALL"})
    with pytest.raises(ValueError, match="ACTIVATION_SECURITY_CONTRACT_REJECTED"):
        validate_payload("compute.activate.self", {**payload, "lease_seconds": LEASE_SECONDS + 1})
    with pytest.raises(ValueError, match="ACTIVATION_OWNER_REJECTED"):
        validate_payload("compute.activate.self", {**payload, "username": "root"})
    with pytest.raises(ValueError, match="ACTIVATION_PAYLOAD_REJECTED"):
        validate_payload("compute.activate.self", {**payload, "command": "docker start any"})

    gpu_payload = gpu_activation_payload()
    assert validate_payload("compute.activate.self", gpu_payload) == gpu_payload
    with pytest.raises(ValueError, match="ACTIVATION_SECURITY_CONTRACT_REJECTED"):
        validate_payload("compute.activate.self", {**gpu_payload, "container_gpu": 2})


def test_activation_records_bind_owner_and_allow_legacy_metadata_without_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = activation_payload()
    record = {
        "record_id": payload["ssh_key_record_ids"][0],
        "managed_user_id": payload["managed_user_id"],
        "username": None,
        "scope": "CONTAINER",
        "fingerprint_sha256": payload["ssh_key_fingerprints"][0],
    }
    monkeypatch.setattr(
        handlers,
        "validate_approved_ssh_key_records",
        lambda _record_ids: [record],
    )
    records, fingerprints = handlers._activation_records(payload)
    assert records == [record]
    assert fingerprints == payload["ssh_key_fingerprints"]

    record["managed_user_id"] = str(uuid.uuid4())
    with pytest.raises(handlers.LifecycleValidationError, match="not Container-only"):
        handlers._activation_records(payload)


def test_one_click_activation_keeps_lease_not_started_until_postconditions_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = activation_payload()
    fingerprints = list(payload["ssh_key_fingerprints"])
    lifecycle = {"STATUS": "STAGED"}
    events: list[tuple[str, Any]] = []
    active_starts_at: datetime | None = None
    active_expires_at: datetime | None = None

    monkeypatch.setattr(handlers, "script_integrity", script_integrity_pass)
    monkeypatch.setattr(handlers, "_activation_runtime_binding", lambda _payload: None)
    monkeypatch.setattr(
        handlers,
        "_activation_records",
        lambda _payload: ([{"record_id": payload["ssh_key_record_ids"][0]}], fingerprints),
    )
    monkeypatch.setattr(
        handlers, "_read_managed_lifecycle_state", lambda _username: dict(lifecycle)
    )
    monkeypatch.setattr(
        handlers,
        "_activation_staged_lifecycle",
        lambda _payload: events.append(("staged", None)),
    )
    monkeypatch.setattr(
        handlers,
        "_compute_stage_postconditions",
        lambda _payload: events.append(("stage-postconditions", None)),
    )
    monkeypatch.setattr(
        handlers,
        "_install_activation_keys",
        lambda _payload, _records, _fingerprints: events.append(("install-keys", None)),
    )

    def write_state(
        _payload: dict[str, Any],
        *,
        status: str,
        fingerprints: list[str],
        starts_at: datetime | None,
        expires_at: datetime | None,
        gpu_allocation_job_id: int | None = None,
        gpu_allocation_uuid: str | None = None,
    ) -> None:
        assert gpu_allocation_job_id is None
        assert gpu_allocation_uuid is None
        nonlocal active_starts_at
        nonlocal active_expires_at
        events.append((f"write-{status}", (starts_at, expires_at, list(fingerprints))))
        lifecycle["STATUS"] = status
        if status == "ACTIVE":
            active_starts_at = starts_at
            active_expires_at = expires_at

    monkeypatch.setattr(handlers, "_write_activation_lifecycle_state", write_state)
    monkeypatch.setattr(handlers, "_activation_container_running", lambda _payload: False)
    monkeypatch.setattr(
        handlers,
        "_activation_container_security",
        lambda _payload, *, require_running, expected_fingerprints: events.append(
            ("container-security", require_running)
        ),
    )

    def run_script(argv: list[str], **_kwargs: Any) -> dict[str, bool]:
        if Path(argv[0]).name == "h100-container-start":
            events.append(("container-start", None))
        else:
            events.append(("gpu-isolation", None))
        return {"ok": True}

    monkeypatch.setattr(handlers, "run_allowlisted_script", run_script)

    def active_lifecycle(
        _payload: dict[str, Any], _fingerprints: list[str]
    ) -> tuple[dict[str, str], datetime, datetime]:
        assert active_starts_at is not None and active_expires_at is not None
        events.append(("active-postcondition", None))
        return lifecycle, active_starts_at, active_expires_at

    monkeypatch.setattr(handlers, "_activation_active_lifecycle", active_lifecycle)

    result = handlers.handle(worker_request(payload))

    assert result["status"] == "SUCCEEDED"
    assert result["handler"] == "compute.activate.self"
    assert result["container_state"] == "RUNNING"
    assert datetime.fromisoformat(result["lease_expires_at"]) - datetime.fromisoformat(
        result["lease_starts_at"]
    ) == timedelta(seconds=LEASE_SECONDS)
    assert events == [
        ("staged", None),
        ("stage-postconditions", None),
        ("install-keys", None),
        ("write-ACTIVATING", (None, None, fingerprints)),
        ("container-security", False),
        ("container-start", None),
        ("container-security", True),
        ("gpu-isolation", None),
        ("write-ACTIVE", (active_starts_at, active_expires_at, fingerprints)),
        ("active-postcondition", None),
    ]


def test_gpu_activation_obtains_one_owner_bound_allocation_before_container_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = gpu_activation_payload()
    fingerprints = list(payload["ssh_key_fingerprints"])
    gpu_uuid = "GPU-11111111-2222-3333-4444-555555555555"
    lifecycle: dict[str, str] = {"STATUS": "STAGED"}
    events: list[tuple[str, Any]] = []
    active_starts_at: datetime | None = None
    active_expires_at: datetime | None = None

    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: {
            **script_integrity_pass(),
            "h100-container-gpu-runtime": {"integrity_ok": True},
        },
    )
    monkeypatch.setattr(handlers, "_activation_runtime_binding", lambda _payload: None)
    monkeypatch.setattr(
        handlers,
        "_activation_records",
        lambda _payload: ([{"record_id": payload["ssh_key_record_ids"][0]}], fingerprints),
    )
    monkeypatch.setattr(
        handlers, "_read_managed_lifecycle_state", lambda _username: dict(lifecycle)
    )
    monkeypatch.setattr(handlers, "_activation_staged_lifecycle", lambda _payload: {})
    monkeypatch.setattr(handlers, "_compute_stage_postconditions", lambda _payload: {})
    monkeypatch.setattr(handlers, "_install_activation_keys", lambda *_args: True)
    monkeypatch.setattr(handlers, "_activation_container_running", lambda _payload: False)

    def security(bound_payload, *, require_running, expected_fingerprints):  # type: ignore[no-untyped-def]
        assert expected_fingerprints == fingerprints
        events.append(("security", require_running))
        if require_running:
            assert bound_payload["gpu_allocation_job_id"] == 701
            assert bound_payload["gpu_allocation_uuid"] == gpu_uuid
        return {}

    monkeypatch.setattr(handlers, "_activation_container_security", security)

    def write_state(
        _payload: dict[str, Any],
        *,
        status: str,
        fingerprints: list[str],
        starts_at: datetime | None,
        expires_at: datetime | None,
        gpu_allocation_job_id: int | None = None,
        gpu_allocation_uuid: str | None = None,
    ) -> None:
        nonlocal active_starts_at, active_expires_at
        lifecycle.update(
            {
                "STATUS": status,
                "GPU_ALLOCATION_JOB_ID": str(gpu_allocation_job_id or ""),
                "GPU_ALLOCATION_UUID": gpu_allocation_uuid or "",
            }
        )
        active_starts_at = starts_at if status == "ACTIVE" else active_starts_at
        active_expires_at = expires_at if status == "ACTIVE" else active_expires_at
        events.append(("state", (status, gpu_allocation_job_id, gpu_allocation_uuid)))

    monkeypatch.setattr(handlers, "_write_activation_lifecycle_state", write_state)
    monkeypatch.setattr(
        handlers,
        "_submit_gpu_development_allocation",
        lambda bound_payload: (
            events.append(("allocation", bound_payload["lease_expires_at"])) or (701, gpu_uuid)
        ),
    )

    def run_script(argv: list[str], **_kwargs: Any) -> dict[str, bool]:
        events.append((Path(argv[0]).name, argv[1:]))
        return {"ok": True}

    monkeypatch.setattr(handlers, "run_allowlisted_script", run_script)

    def active_lifecycle(
        _payload: dict[str, Any], _fingerprints: list[str]
    ) -> tuple[dict[str, str], datetime, datetime]:
        assert active_starts_at is not None and active_expires_at is not None
        return lifecycle, active_starts_at, active_expires_at

    monkeypatch.setattr(handlers, "_activation_active_lifecycle", active_lifecycle)
    result = handlers.handle(worker_request(payload))

    assert result["status"] == "SUCCEEDED"
    assert result["container_gpu"] == "SLURM_ALLOCATED_1"
    assert result["gpu_allocation_job_id"] == 701
    assert result["gpu_allocation_uuid"] == gpu_uuid
    allocation_index = next(i for i, event in enumerate(events) if event[0] == "allocation")
    start_index = next(
        i for i, event in enumerate(events) if event[0] == "h100-container-gpu-runtime"
    )
    assert allocation_index < start_index
    assert events[start_index][1] == ["start", "origin-pilot2", "701", gpu_uuid]


def test_container_start_failure_rolls_back_without_active_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = activation_payload()
    fingerprints = list(payload["ssh_key_fingerprints"])
    statuses: list[str] = []
    rolled_back: list[bool] = []
    monkeypatch.setattr(handlers, "script_integrity", script_integrity_pass)
    monkeypatch.setattr(handlers, "_activation_runtime_binding", lambda _payload: None)
    monkeypatch.setattr(
        handlers,
        "_activation_records",
        lambda _payload: ([{"record_id": payload["ssh_key_record_ids"][0]}], fingerprints),
    )
    monkeypatch.setattr(
        handlers,
        "_read_managed_lifecycle_state",
        lambda _username: {"STATUS": "STAGED"},
    )
    monkeypatch.setattr(handlers, "_activation_staged_lifecycle", lambda _payload: {})
    monkeypatch.setattr(handlers, "_compute_stage_postconditions", lambda _payload: {})
    monkeypatch.setattr(handlers, "_install_activation_keys", lambda *_args: True)
    monkeypatch.setattr(
        handlers,
        "_write_activation_lifecycle_state",
        lambda _payload, *, status, **_kwargs: statuses.append(status),
    )
    monkeypatch.setattr(handlers, "_activation_container_running", lambda _payload: False)
    monkeypatch.setattr(handlers, "_activation_container_security", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda argv, **_kwargs: {"ok": Path(argv[0]).name != "h100-container-start"},
    )
    monkeypatch.setattr(
        handlers,
        "_rollback_self_activation",
        lambda _payload, _fingerprints: rolled_back.append(True),
    )

    result = handlers.handle(worker_request(payload))

    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "CONTAINER_START_FAILED"
    assert result["rollback_status"] == "ROLLED_BACK"
    assert statuses == ["ACTIVATING"]
    assert rolled_back == [True]


def test_active_replay_does_not_restart_or_extend_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = activation_payload()
    starts = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)
    expires = starts + timedelta(seconds=LEASE_SECONDS)
    monkeypatch.setattr(handlers, "script_integrity", script_integrity_pass)
    monkeypatch.setattr(handlers, "_activation_runtime_binding", lambda _payload: None)
    monkeypatch.setattr(
        handlers,
        "_activation_records",
        lambda _payload: (
            [{"record_id": payload["ssh_key_record_ids"][0]}],
            list(payload["ssh_key_fingerprints"]),
        ),
    )
    monkeypatch.setattr(
        handlers,
        "_read_managed_lifecycle_state",
        lambda _username: {"STATUS": "ACTIVE"},
    )
    monkeypatch.setattr(
        handlers,
        "_activation_active_lifecycle",
        lambda _payload, _fingerprints: ({"STATUS": "ACTIVE"}, starts, expires),
    )
    monkeypatch.setattr(handlers, "_activation_container_security", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: pytest.fail("ACTIVE replay must not run lifecycle scripts"),
    )
    monkeypatch.setattr(
        handlers,
        "_write_activation_lifecycle_state",
        lambda *_args, **_kwargs: pytest.fail("ACTIVE replay must not rewrite Lease timestamps"),
    )

    result = handlers.handle(worker_request(payload))

    assert result["status"] == "SUCCEEDED"
    assert result["idempotent_replay"] is True
    assert result["lease_starts_at"] == starts.isoformat()
    assert result["lease_expires_at"] == expires.isoformat()


def test_owner_and_operation_binding_rejects_cross_user_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = activation_payload()
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        lambda: pytest.fail("owner binding must reject before integrity or mutation"),
    )

    result = handlers.handle(worker_request(payload, requested_by="different-user"))

    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "ACTIVATION_OWNER_BINDING_REJECTED"
    assert result["rollback_status"] == "NOT_REQUIRED"


def test_rollback_refuses_active_lifecycle_from_another_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = activation_payload()
    monkeypatch.setattr(
        handlers,
        "_read_managed_lifecycle_state",
        lambda _username: {
            "STATUS": "ACTIVE",
            "ACTIVATION_OPERATION_ID": str(uuid.uuid4()),
            "CONTAINER_KEY_FINGERPRINTS": payload["ssh_key_fingerprints"][0],
        },
    )
    monkeypatch.setattr(
        handlers,
        "_validate_activation_lifecycle_common",
        lambda _lifecycle, _payload: None,
    )
    monkeypatch.setattr(
        handlers,
        "containers_inspect",
        lambda _payload: pytest.fail("conflicting ACTIVE state must fail before Docker access"),
    )

    with pytest.raises(handlers.LifecycleValidationError, match="another operation"):
        handlers._rollback_self_activation(payload, list(payload["ssh_key_fingerprints"]))


def test_activation_dry_run_is_not_a_product_step() -> None:
    payload = activation_payload()
    request = worker_request(payload).model_copy(update={"dry_run": True})
    result = handlers.handle(request)
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "ACTIVATION_DRY_RUN_REJECTED"
