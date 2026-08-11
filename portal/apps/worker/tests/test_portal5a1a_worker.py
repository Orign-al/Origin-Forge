import json
import uuid

import pytest
from h100_portal_worker import handlers
from h100_portal_worker.schemas import WorkerRequest, validate_payload


def plan_payload() -> dict[str, object]:
    return {
        "request_id": str(uuid.uuid4()),
        "portal_account_id": str(uuid.uuid4()),
        "username": "fixture-user",
        "requested_gpu_max": 1,
        "requested_storage_bytes": 300 * 1024**3,
        "requested_container_profile": "STANDARD_8CPU_32GB",
        "requested_lease_seconds": 96 * 60 * 60,
        "reserved_uids": [20001],
        "reserved_gids": [20001],
        "reserved_project_ids": [30001],
        "reserved_ssh_ports": [22023],
        "reserved_container_names": ["gpu-dev-origin-pilot"],
    }


def dry_run_payload() -> dict[str, object]:
    return {
        "request_id": str(uuid.uuid4()),
        "plan_id": str(uuid.uuid4()),
        "portal_account_id": str(uuid.uuid4()),
        "username": "fixture-user",
        "uid": 20002,
        "gid": 20002,
        "project_id": 30002,
        "ssh_port": 22024,
        "container_name": "gpu-dev-fixture-user",
        "storage_bytes": 300 * 1024**3,
        "container_profile": "STANDARD_8CPU_32GB",
        "container_cpus": 8,
        "container_memory_gb": 32,
        "container_pids_limit": 4096,
        "container_gpu": 0,
        "slurm_account": "company",
        "slurm_qos": "general",
        "gpu_max": 1,
        "lease_seconds": 96 * 60 * 60,
        "lease_state": "NOT_STARTED",
        "host_ssh": "DISABLED",
        "shell": "/usr/sbin/nologin",
        "password_state": "LOCKED",
        "execution_enabled": False,
    }


def request(operation: str, payload: dict[str, object], *, dry_run: bool) -> WorkerRequest:
    return WorkerRequest(
        protocol_version=1,
        request_id=str(uuid.uuid4()),
        operation_type=operation,
        payload=payload,
        requested_by="fixture-admin",
        approved_by="fixture-admin",
        idempotency_key=f"portal5a1a-{uuid.uuid4()}",
        dry_run=dry_run,
    )


def passed(name: str) -> dict[str, str]:
    return {"check": name, "status": "PASS", "detail": "fixture read-only pass"}


def test_compute_plan_schema_is_closed_and_enforces_standard_policy() -> None:
    payload = plan_payload()
    assert validate_payload("compute.provision.plan", payload)["requested_gpu_max"] == 1
    with pytest.raises(ValueError, match="GPU_MAX_REJECTED"):
        validate_payload("compute.provision.plan", {**payload, "requested_gpu_max": 2})
    with pytest.raises(ValueError, match="STANDARD_COMPUTE_PROFILE_REQUIRED"):
        validate_payload(
            "compute.provision.plan", {**payload, "requested_storage_bytes": 100 * 1024**3}
        )
    with pytest.raises(ValueError, match="STANDARD_COMPUTE_PROFILE_REQUIRED"):
        validate_payload(
            "compute.provision.plan", {**payload, "requested_lease_seconds": 97 * 3600}
        )
    with pytest.raises(ValueError, match="fields are invalid"):
        validate_payload("compute.provision.plan", {**payload, "command": "useradd root"})
    with pytest.raises(ValueError, match="protected username"):
        validate_payload("compute.provision.plan", {**payload, "username": "root"})


def test_compute_dry_run_schema_binds_every_reserved_value() -> None:
    payload = dry_run_payload()
    validated = validate_payload("compute.provision.dry_run", payload)
    assert validated["execution_enabled"] is False
    assert validated["host_ssh"] == "DISABLED"
    assert validated["container_gpu"] == 0
    with pytest.raises(ValueError, match="PLAN_MISMATCH"):
        validate_payload(
            "compute.provision.dry_run", {**payload, "container_name": "gpu-dev-other"}
        )
    with pytest.raises(ValueError, match="PLAN_MISMATCH"):
        validate_payload("compute.provision.dry_run", {**payload, "execution_enabled": True})
    with pytest.raises(ValueError, match="GPU_MAX_REJECTED"):
        validate_payload("compute.provision.dry_run", {**payload, "gpu_max": 4})


def test_allocator_plan_excludes_portal_reservations_and_never_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = plan_payload()
    observed: dict[str, set[int]] = {}
    monkeypatch.setattr(
        handlers,
        "_compute_target_checks",
        lambda _username, _container: [passed("target")],
    )
    monkeypatch.setattr(
        handlers, "_compute_platform_checks", lambda _username: [passed("platform")]
    )

    def uid_candidate(excluded: set[int]):
        observed["uid"] = excluded
        return 20002, [], {"legacy_ownership_scan": "PASS"}

    def project_candidate(excluded: set[int]):
        observed["project"] = excluded
        return 30002, {"source": "fixture"}

    def port_candidate(excluded: set[int]):
        observed["port"] = excluded
        return 22024, {"source": "fixture"}

    monkeypatch.setattr(handlers, "_candidate_uid_gid", uid_candidate)
    monkeypatch.setattr(handlers, "_candidate_project_id", project_candidate)
    monkeypatch.setattr(handlers, "_candidate_ssh_port", port_candidate)
    result = handlers.handle(request("compute.provision.plan", payload, dry_run=True))
    assert result["status"] == "DRY_RUN"
    assert result["plan_status"] == "READY"
    assert result["execution_enabled"] is False
    assert result["proposed_uid"] == result["proposed_gid"] == 20002
    assert result["proposed_project_id"] == 30002
    assert result["proposed_ssh_port"] == 22024
    assert result["proposed_container_name"] == "gpu-dev-fixture-user"
    assert result["proposed_lease"]["state"] == "NOT_STARTED"
    assert result["proposed_lease"]["starts_at"] is None
    assert observed == {"uid": {20001}, "project": {30001}, "port": {22023}}
    assert "command" not in json.dumps(result).casefold()


def test_exact_dry_run_reports_zero_side_effects_and_real_execution_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = dry_run_payload()
    monkeypatch.setattr(
        handlers,
        "_compute_target_checks",
        lambda _username, _container: [passed("target")],
    )
    monkeypatch.setattr(
        handlers, "_compute_exact_resource_checks", lambda _payload: [passed("reservation")]
    )
    monkeypatch.setattr(
        handlers, "_compute_platform_checks", lambda _username: [passed("platform")]
    )
    result = handlers.handle(request("compute.provision.dry_run", payload, dry_run=True))
    assert result["status"] == "DRY_RUN"
    assert result["dry_run_status"] == "READY_FOR_PROVISION"
    assert result["execution_enabled"] is False
    assert result["infrastructure_side_effects"] == "NONE"
    assert not any(result["resource_writes"].values())

    monkeypatch.setattr(
        handlers,
        "_compute_provision_dry_run",
        lambda _payload: pytest.fail("real execution must never enter dry-run handler"),
    )
    denied = handlers.handle(request("compute.provision.dry_run", payload, dry_run=False))
    assert denied["status"] == "ERROR"
    assert denied["error"]["code"] == "WRITE_EXECUTION_DISABLED"
