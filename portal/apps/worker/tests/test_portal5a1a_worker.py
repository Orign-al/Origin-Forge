import hashlib
import inspect
import json
import subprocess
import uuid
from pathlib import Path

import pytest
from h100_portal_worker import handlers
from h100_portal_worker.schemas import WorkerRequest, validate_payload

PORTAL_ROOT = Path(__file__).resolve().parents[3]
PLATFORM_ROOT = PORTAL_ROOT.parent
REAL_IMAGE_VALIDATOR = handlers._standard_dev_image_identity


def image_identity(*, image_id: str = "sha256:" + "d" * 64) -> dict[str, object]:
    return {
        "status": "PASS",
        "validator_version": "compute-provision-stage-image-validator-v1",
        "identity_sha256": "e" * 64,
        "reference": "h100-local/dev-container:ubuntu24.04-origin-pilot-20260804",
        "image_id": image_id,
        "repo_digests": [f"h100-local/dev-container@{image_id}"],
        "created_at": "2026-08-04T00:00:00Z",
        "build_user": "DEFAULT_ROOT",
        "failure_code": None,
    }


@pytest.fixture(autouse=True)
def stable_standard_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "_standard_dev_image_identity", image_identity)


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


def stage_integrity(handler_sha256: str = "a" * 64) -> dict[str, dict[str, object]]:
    return {
        name: {
            "integrity_ok": True,
            "sha256": handler_sha256 if name == "h100-provision-stage" else "b" * 64,
        }
        for name in handlers.COMPUTE_STAGE_REQUIRED_SCRIPTS
    }


def stage_payload() -> dict[str, object]:
    payload = {**dry_run_payload(), "execution_enabled": True}
    payload.update(
        {
            "stage_operation_id": str(uuid.uuid4()),
            "dry_run_operation_id": str(uuid.uuid4()),
            "reservation_ids": {
                resource_type: str(uuid.uuid4())
                for resource_type in (
                    "UID",
                    "GID",
                    "PROJECT_ID",
                    "SSH_PORT",
                    "CONTAINER_NAME",
                )
            },
        }
    )
    payload["dry_run_stage_contract"] = handlers._compute_stage_contract(
        payload, integrity=stage_integrity()
    )
    return payload


def request(operation: str, payload: dict[str, object], *, dry_run: bool) -> WorkerRequest:
    return WorkerRequest(
        protocol_version=1,
        request_id=str(uuid.uuid4()),
        operation_type=operation,
        payload=payload,
        requested_by="fixture-admin",
        approved_by="fixture-admin",
        idempotency_key=(
            f"compute-stage:{payload['stage_operation_id']}"
            if operation == "compute.provision.stage"
            else f"portal5a1a-{uuid.uuid4()}"
        ),
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


def test_standard_image_validator_accepts_docker_default_root_when_config_user_is_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "d" * 64

    def fixed(binary: str, args: list[str], timeout: float = 20.0):  # type: ignore[no-untyped-def]
        assert binary == "docker"
        assert timeout in {15, 20}
        if args[:2] == ["container", "inspect"]:
            return {
                "ok": True,
                "stdout": json.dumps(
                    [{"Config": {"Image": "h100-local/dev-container:accepted"}, "Image": image_id}]
                ),
                "stderr": "",
            }
        assert args[:2] == ["image", "inspect"]
        return {
            "ok": True,
            "stdout": json.dumps(
                [
                    {
                        "Id": image_id,
                        "RepoDigests": [f"h100-local/dev-container@{image_id}"],
                        "Created": "2026-08-04T00:00:00Z",
                        "Config": {
                            "User": None,
                            "Labels": {
                                "h100.dev.user": "origin-pilot",
                                "h100.dev.uid": "20001",
                                "h100.dev.gid": "20001",
                            },
                        },
                    }
                ]
            ),
            "stderr": "",
        }

    monkeypatch.setattr(handlers, "run_fixed", fixed)
    monkeypatch.setattr(handlers, "_standard_dev_image_identity", REAL_IMAGE_VALIDATOR)
    observed = handlers._standard_dev_image_identity()
    assert observed["status"] == "PASS"
    assert observed["build_user"] == "DEFAULT_ROOT"
    assert observed["image_id"] == image_id


def test_standard_image_validator_requires_repo_digest_bound_to_image_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_id = "sha256:" + "d" * 64

    def fixed(binary: str, args: list[str], timeout: float = 20.0):  # type: ignore[no-untyped-def]
        assert binary == "docker"
        assert timeout in {15, 20}
        if args[:2] == ["container", "inspect"]:
            return {
                "ok": True,
                "stdout": json.dumps(
                    [{"Config": {"Image": "h100-local/dev-container:accepted"}, "Image": image_id}]
                ),
                "stderr": "",
            }
        return {
            "ok": True,
            "stdout": json.dumps(
                [
                    {
                        "Id": image_id,
                        "RepoDigests": ["h100-local/dev-container@sha256:" + "e" * 64],
                        "Created": "2026-08-04T00:00:00Z",
                        "Config": {
                            "User": None,
                            "Labels": {
                                "h100.dev.user": "origin-pilot",
                                "h100.dev.uid": "20001",
                                "h100.dev.gid": "20001",
                            },
                        },
                    }
                ]
            ),
            "stderr": "",
        }

    monkeypatch.setattr(handlers, "run_fixed", fixed)
    monkeypatch.setattr(handlers, "_standard_dev_image_identity", REAL_IMAGE_VALIDATOR)
    observed = handlers._standard_dev_image_identity()
    assert observed["status"] == "FAIL"
    assert observed["failure_code"] == "IMAGE_REPO_DIGEST_INVALID"


def test_stage_image_identity_change_requires_new_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = stage_payload()
    monkeypatch.setattr(handlers, "script_integrity", stage_integrity)
    changed = image_identity(image_id="sha256:" + "f" * 64)
    changed["identity_sha256"] = "f" * 64
    monkeypatch.setattr(handlers, "_standard_dev_image_identity", lambda: changed)
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: pytest.fail("image identity drift must fail before execution"),
    )
    result = handlers.handle(request("compute.provision.stage", payload, dry_run=False))
    assert result["error"]["code"] == "DRY_RUN_STAGE_CONTRACT_MISMATCH"
    assert "IMAGE_IDENTITY" in result["contract_verification"]["mismatches"]
    assert result["side_effect_classification"] == "NO_SIDE_EFFECT"


def test_compute_stage_schema_binds_dry_run_and_all_reservations() -> None:
    payload = stage_payload()
    validated = validate_payload("compute.provision.stage", payload)
    assert validated["execution_enabled"] is True
    assert set(validated["reservation_ids"]) == {
        "UID",
        "GID",
        "PROJECT_ID",
        "SSH_PORT",
        "CONTAINER_NAME",
    }
    with pytest.raises(ValueError, match="RESERVATION_REJECTED"):
        validate_payload(
            "compute.provision.stage",
            {**payload, "reservation_ids": {"UID": str(uuid.uuid4())}},
        )
    with pytest.raises(ValueError, match="EXECUTION_GATE_REJECTED"):
        validate_payload("compute.provision.stage", {**payload, "execution_enabled": False})
    with pytest.raises(ValueError, match="fields are invalid"):
        validate_payload("compute.provision.stage", {**payload, "command": "useradd root"})


def test_compute_retry_verify_is_closed_and_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    stage = stage_payload()
    payload = {
        key: stage[key]
        for key in {
            "request_id",
            "plan_id",
            "stage_operation_id",
            "portal_account_id",
            "username",
            "uid",
            "gid",
            "project_id",
            "ssh_port",
            "container_name",
        }
    }
    validated = validate_payload("compute.provision.retry_verify", payload)
    assert validated["stage_operation_id"] == payload["stage_operation_id"]
    with pytest.raises(ValueError, match="fields are invalid"):
        validate_payload(
            "compute.provision.retry_verify", {**payload, "command": "useradd fixture-user"}
        )

    monkeypatch.setattr(
        handlers,
        "script_integrity",
        stage_integrity,
    )
    monkeypatch.setattr(handlers, "_compute_stage_retained_resources", lambda _payload: [])
    verified = handlers.handle(request("compute.provision.retry_verify", payload, dry_run=True))
    assert verified["retry_verification_status"] == "VERIFIED_ZERO_RESIDUE"
    assert verified["script_integrity"] == "PASS"
    assert verified["resource_residue"] == []
    denied = handlers.handle(request("compute.provision.retry_verify", payload, dry_run=False))
    assert denied["error"]["code"] == "COMPUTE_RETRY_VERIFY_EXECUTION_REJECTED"


def test_ordinary_multi_user_ssh_key_prepare_is_container_only() -> None:
    payload = {
        "record_id": str(uuid.uuid4()),
        "operation_id": str(uuid.uuid4()),
        "managed_user_id": str(uuid.uuid4()),
        "username": "origin-pilot2",
        "public_key": "ssh-ed25519 " + "A" * 48,
        "key_type": "ssh-ed25519",
        "fingerprint_sha256": "SHA256:" + "A" * 43,
        "content_sha256": "a" * 64,
        "scope": "CONTAINER",
    }
    validated = validate_payload("ssh_key.prepare", payload)
    assert validated["username"] == "origin-pilot2"
    assert validated["scope"] == "CONTAINER"
    with pytest.raises(ValueError, match="CONTAINER-only"):
        validate_payload("ssh_key.prepare", {**payload, "scope": "HOST"})
    with pytest.raises(ValueError, match="invalid or protected"):
        validate_payload("ssh_key.prepare", {**payload, "username": "root"})


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
        handlers, "_compute_platform_checks", lambda _username, **_kwargs: [passed("platform")]
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


def test_project_allocator_includes_xfs_quota_records_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "_safe_file_lines", lambda _path: [])
    monkeypatch.setattr(handlers, "_pilot_state_records", lambda: [])
    monkeypatch.setattr(
        handlers,
        "run_fixed",
        lambda *_args, **_kwargs: {
            "ok": True,
            "stdout": "#30001 0 0 314572800 00 [------]\n#30002 0 0 0 00 [------]\n",
        },
    )
    used, readable = handlers._used_project_ids()
    assert readable is True
    assert {30001, 30002}.issubset(used)
    candidate, evidence = handlers._candidate_project_id()
    assert candidate == 30003
    assert "XFS quota report" in evidence["source"]

    monkeypatch.setattr(
        handlers,
        "run_fixed",
        lambda *_args, **_kwargs: {"ok": False, "stdout": "", "stderr": "unavailable"},
    )
    candidate, evidence = handlers._candidate_project_id()
    assert candidate is None
    assert evidence["reservation"] == "NOT_AVAILABLE"


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
        handlers, "_compute_platform_checks", lambda _username, **_kwargs: [passed("platform")]
    )
    monkeypatch.setattr(handlers, "script_integrity", stage_integrity)
    result = handlers.handle(request("compute.provision.dry_run", payload, dry_run=True))
    assert result["status"] == "DRY_RUN"
    assert result["dry_run_status"] == "READY_FOR_PROVISION"
    assert result["execution_enabled"] is False
    assert result["infrastructure_side_effects"] == "NONE"
    assert not any(result["resource_writes"].values())
    contract = result["stage_contract"]
    assert contract["status"] == "PASS"
    assert contract["handler"]["sha256"] == "a" * 64
    assert contract["argv_contract"]["argument_13"] == {
        "index": 13,
        "semantic_role": "EXPLICIT_STAGE_CONFIRMATION_FLAG",
        "binding_status": "VALID",
    }
    assert contract["argv_contract"]["argument_14"] == {
        "index": 14,
        "semantic_role": "CONFIRMED_TARGET_USERNAME",
        "binding_status": "VALID",
    }
    assert contract["confirmation_gate"]["status"] == "PASS"

    monkeypatch.setattr(
        handlers,
        "_compute_provision_dry_run",
        lambda _payload: pytest.fail("real execution must never enter dry-run handler"),
    )
    denied = handlers.handle(request("compute.provision.dry_run", payload, dry_run=False))
    assert denied["status"] == "ERROR"
    assert denied["error"]["code"] == "WRITE_EXECUTION_DISABLED"


@pytest.mark.parametrize("index", [13, 14])
def test_dry_run_argument_binding_mismatch_is_contract_incomplete_and_read_only(
    monkeypatch: pytest.MonkeyPatch, index: int
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
        handlers, "_compute_platform_checks", lambda _username, **_kwargs: [passed("platform")]
    )
    monkeypatch.setattr(handlers, "script_integrity", stage_integrity)
    original_builder = handlers._compute_stage_argv

    def mismatched_builder(stage: dict[str, object]) -> list[str]:
        argv = original_builder(stage)
        argv[index] = "synthetic-binding-mismatch"
        return argv

    monkeypatch.setattr(handlers, "_compute_stage_argv", mismatched_builder)
    result = handlers.handle(request("compute.provision.dry_run", payload, dry_run=True))
    assert result["dry_run_status"] == "CONTRACT_INCOMPLETE"
    assert result["stage_contract"]["status"] == "FAIL"
    assert (
        result["stage_contract"]["argv_contract"][f"argument_{index}"]["binding_status"]
        == "INVALID"
    )
    assert result["infrastructure_side_effects"] == "NONE"
    assert not any(result["resource_writes"].values())


def test_compute_stage_runs_only_hash_pinned_fixed_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = stage_payload()
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        stage_integrity,
    )
    monkeypatch.setattr(
        handlers,
        "_compute_provision_dry_run",
        lambda _payload: {"dry_run_status": "READY_FOR_PROVISION"},
    )
    observed: list[str] = []

    def execute(argv: list[str], timeout: float) -> dict[str, object]:
        observed.extend(argv)
        assert timeout == handlers.STAGE_EXECUTION_TIMEOUT_SECONDS
        return {"ok": True, "exit_code": 0, "stdout": "STAGED", "stderr": ""}

    monkeypatch.setattr(handlers, "run_allowlisted_script", execute)
    monkeypatch.setattr(handlers, "_compute_stage_postconditions", lambda _payload: {"ok": True})
    monkeypatch.setattr(handlers.Path, "exists", lambda _path: False)
    monkeypatch.setattr(handlers.Path, "is_symlink", lambda _path: False)
    result = handlers.handle(request("compute.provision.stage", payload, dry_run=False))
    assert result["status"] == "SUCCEEDED"
    assert result["handler"] == "compute.provision.stage"
    assert len(observed) == 15
    assert observed[0] == "/usr/local/sbin/h100-provision-stage"
    assert observed[-2:] == ["--confirm-stage", payload["username"]]

    dry_run_denied = handlers.handle(request("compute.provision.stage", payload, dry_run=True))
    assert dry_run_denied["status"] == "ERROR"
    assert dry_run_denied["error"]["code"] == "COMPUTE_STAGE_DRY_RUN_REJECTED"


def test_compute_stage_missing_contract_evidence_fails_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = stage_payload()
    payload.pop("dry_run_stage_contract")
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: pytest.fail("missing evidence must forbid Stage"),
    )
    result = handlers.handle(request("compute.provision.stage", payload, dry_run=False))
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "DRY_RUN_STAGE_CONTRACT_INCOMPLETE"
    assert result["side_effect_classification"] == "NO_SIDE_EFFECT"


@pytest.mark.parametrize(
    ("index", "mismatch"),
    [
        (13, "ARGUMENT_13_BINDING"),
        (14, "ARGUMENT_14_BINDING"),
    ],
)
def test_compute_stage_argument_binding_mismatch_requires_new_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    index: int,
    mismatch: str,
) -> None:
    payload = stage_payload()
    original_builder = handlers._compute_stage_argv

    def mismatched_builder(stage: dict[str, object]) -> list[str]:
        argv = original_builder(stage)
        argv[index] = "synthetic-binding-mismatch"
        return argv

    monkeypatch.setattr(handlers, "script_integrity", stage_integrity)
    monkeypatch.setattr(handlers, "_compute_stage_argv", mismatched_builder)
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: pytest.fail("contract mismatch must forbid Stage"),
    )
    result = handlers.handle(request("compute.provision.stage", payload, dry_run=False))
    assert result["error"]["code"] == "DRY_RUN_STAGE_CONTRACT_MISMATCH"
    assert mismatch in result["contract_verification"]["mismatches"]
    assert result["side_effect_classification"] == "NO_SIDE_EFFECT"


@pytest.mark.parametrize(
    ("mutation", "mismatch"),
    [
        ("handler", "HANDLER_SHA256"),
        ("dependency", "CONTRACT_SHA256"),
        ("argv", "ARGV_CONTRACT_SHA256"),
        ("validator", "CONFIRMATION_VALIDATOR_SHA256"),
    ],
)
def test_compute_stage_contract_identity_change_requires_new_dry_run(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    mismatch: str,
) -> None:
    payload = stage_payload()
    expected = payload["dry_run_stage_contract"]
    assert isinstance(expected, dict)
    if mutation == "handler":
        monkeypatch.setattr(handlers, "script_integrity", lambda: stage_integrity("c" * 64))
    elif mutation == "dependency":
        changed_integrity = stage_integrity()
        changed_integrity["h100-platform-common"]["sha256"] = "c" * 64
        monkeypatch.setattr(handlers, "script_integrity", lambda: changed_integrity)
    else:
        monkeypatch.setattr(handlers, "script_integrity", stage_integrity)
        if mutation == "argv":
            expected["argv_contract"]["sha256"] = "c" * 64
        else:
            expected["confirmation_gate"]["validator_sha256"] = "c" * 64
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda *_args, **_kwargs: pytest.fail("changed contract must forbid Stage"),
    )
    result = handlers.handle(request("compute.provision.stage", payload, dry_run=False))
    assert result["error"]["code"] == "DRY_RUN_STAGE_CONTRACT_MISMATCH"
    assert mismatch in result["contract_verification"]["mismatches"]
    assert result["side_effect_classification"] == "NO_SIDE_EFFECT"


def test_stage_contract_evidence_excludes_sensitive_argv_values() -> None:
    marker = "SYNTHETIC_SECRET_MARKER_MUST_NOT_LEAK"
    payload = {**stage_payload(), "dry_run_operation_id": marker}
    evidence = handlers._compute_stage_contract(payload, integrity=stage_integrity())
    encoded = json.dumps(evidence, sort_keys=True)
    assert marker not in encoded
    assert "fixture-user" not in encoded
    assert "argv" not in evidence


def test_compute_provision_transport_remains_local_unix_socket_and_shell_false() -> None:
    server_source = (PORTAL_ROOT / "apps/worker/src/h100_portal_worker/server.py").read_text()
    runner_source = inspect.getsource(handlers.run_allowlisted_script)
    assert 'SOCKET_PATH = "/run/h100-portal/worker.sock"' in server_source
    assert "socket.AF_UNIX" in server_source
    assert "shell=False" in runner_source
    assert "ssh" not in inspect.getsource(handlers._execute_compute_provision_stage).casefold()


@pytest.mark.parametrize(
    ("declared", "expected_rollback"),
    [
        ("NO_SIDE_EFFECT", "NOT_REQUIRED"),
        ("PARTIAL_ROLLED_BACK", "ROLLED_BACK"),
    ],
)
def test_compute_stage_failure_classification_is_structured_and_zero_residue_bound(
    monkeypatch: pytest.MonkeyPatch,
    declared: str,
    expected_rollback: str,
) -> None:
    payload = stage_payload()
    monkeypatch.setattr(
        handlers,
        "script_integrity",
        stage_integrity,
    )
    monkeypatch.setattr(
        handlers,
        "_compute_provision_dry_run",
        lambda _payload: {"dry_run_status": "READY_FOR_PROVISION"},
    )
    monkeypatch.setattr(handlers, "_compute_stage_retained_resources", lambda _payload: [])
    stderr = "\n".join(
        [
            "COMPUTE STAGE FIRST FAILED STEP: EXPLICIT_STAGE_CONFIRMATION_GATE",
            "COMPUTE STAGE LAST SUCCESSFUL STEP: SCRIPT_ARGUMENT_COUNT",
            f"COMPUTE STAGE SIDE EFFECT CLASSIFICATION: {declared}",
        ]
    )
    monkeypatch.setattr(
        handlers,
        "run_allowlisted_script",
        lambda _argv, timeout: {
            "ok": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": stderr,
        },
    )
    result = handlers.handle(request("compute.provision.stage", payload, dry_run=False))
    assert result["status"] == "ERROR"
    assert result["side_effect_classification"] == declared
    assert result["rollback_status"] == expected_rollback
    assert result["first_failed_step"] == "EXPLICIT_STAGE_CONFIRMATION_GATE"
    assert result["last_successful_step"] == "SCRIPT_ARGUMENT_COUNT"
    assert result["retained_resources"] == []


def test_compute_stage_idempotency_binding_fails_before_side_effects() -> None:
    payload = stage_payload()
    unbound = request("compute.provision.stage", payload, dry_run=False).model_copy(
        update={"idempotency_key": f"compute-stage:{uuid.uuid4()}"}
    )
    result = handlers.handle(unbound)
    assert result["error"]["code"] == "COMPUTE_STAGE_IDEMPOTENCY_BINDING_REJECTED"
    assert result["side_effect_classification"] == "NO_SIDE_EFFECT"
    assert result["rollback_status"] == "NOT_REQUIRED"


def test_compute_stage_script_confirmation_uses_parameters_above_nine() -> None:
    source = PLATFORM_ROOT / "scripts/h100-provision-stage"
    manifest = json.loads((PORTAL_ROOT / "deploy/worker-scripts.json").read_text())
    content = source.read_text()

    subprocess.run(["/usr/bin/bash", "-n", str(source)], check=True)
    assert '"${13:-}" == --confirm-stage' in content
    assert '"${2:-}" == "${14:-}"' in content
    assert '"$13"' not in content
    assert '"$14"' not in content
    assert manifest["h100-provision-stage"] == hashlib.sha256(source.read_bytes()).hexdigest()

    argv = [
        "--execute",
        "fixture-user",
        "20002",
        "20002",
        "30002",
        "22024",
        "company",
        "general",
        "1",
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        "--confirm-stage",
        "fixture-user",
    ]
    old_gate = '[[ "$1" == --execute && "$13" == --confirm-stage && "$2" == "$14" ]]'
    fixed_gate = (
        '[[ "${1:-}" == --execute && "${13:-}" == --confirm-stage && "${2:-}" == "${14:-}" ]]'
    )
    old = subprocess.run(["/usr/bin/bash", "-c", old_gate, "gate", *argv], check=False)
    fixed = subprocess.run(["/usr/bin/bash", "-c", fixed_gate, "gate", *argv], check=False)
    assert old.returncode != 0
    assert fixed.returncode == 0


def test_stage_image_gate_precedes_any_compute_write_and_pins_repo_digest() -> None:
    source = (PLATFORM_ROOT / "scripts/h100-provision-stage").read_text()
    assert source.index("current_step=CONTAINER_IMAGE_PREWRITE_GATE") < source.index(
        "current_step=LINUX_IDENTITY"
    )
    assert '((.[0].Config.User // "") == "" or .[0].Config.User == "root")' in source
    assert "FROM ${base_image_digest}" in source
    assert "FROM ${base_image_id}" not in source


def test_stage_rollback_evidence_parser_preserves_idempotent_group_cleanup() -> None:
    stderr = "\n".join(
        [
            "COMPUTE STAGE ROLLBACK STEP: LINUX_USER=SUCCEEDED",
            "COMPUTE STAGE ROLLBACK STEP: LINUX_GROUP=SUCCEEDED",
            "COMPUTE STAGE SIDE EFFECT CLASSIFICATION: PARTIAL_ROLLED_BACK",
        ]
    )
    assert handlers._stage_rollback_steps(stderr) == {
        "LINUX_GROUP": "SUCCEEDED",
        "LINUX_USER": "SUCCEEDED",
    }


def test_stage_rollback_treats_private_group_removed_by_userdel_as_success() -> None:
    source = (PLATFORM_ROOT / "scripts/h100-provision-stage").read_text()
    rollback_mark = source[source.index("rollback_mark() {") : source.index("\nrollback() {")]
    rollback = source[source.index("rollback() {") : source.index("\non_exit() {")]
    fixture = f"""
{rollback_mark}
{rollback}
directory_contains_user_data() {{ return 1; }}
rm() {{ return 0; }}
systemctl() {{ return 0; }}
usermod() {{ return 0; }}
passwd() {{ return 0; }}
group_present=1
getent() {{
  if [[ $1 == passwd ]]; then return 0; fi
  ((group_present == 1))
}}
userdel() {{ group_present=0; return 0; }}
groupdel() {{ printf 'GROUPDEL_CALLED\\n' >&2; return 1; }}
created_container=0
created_compose=0
created_image=0
created_association=0
created_mapping=0
created_policy=0
created_container_data=0
created_data=0
created_user=1
created_group=1
isolation_marker_one=
isolation_marker_two=
username=fixture-user
user_root=/fixture/users/fixture-user
state_file=/fixture/state
container_name=gpu-dev-fixture-user
compose_file=/fixture/compose.yml
compose_dir=/fixture
derived_image=fixture-image
slurm_account=company
project_name=h100_fixture-user
H100_DATA_ROOT=/fixture
backup_dir=/fixture/backup
PROJECTS_FILE=/fixture/projects
PROJID_FILE=/fixture/projid
GPU_ISOLATION_TOOL=/fixture/isolation
CONTAINER_DATA_ROOT=/fixture/container-data
rollback 1
exit 0
"""
    result = subprocess.run(
        ["/usr/bin/bash", "-c", fixture],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "GROUPDEL_CALLED" not in result.stderr
    assert "COMPUTE STAGE ROLLBACK STEP: LINUX_USER=SUCCEEDED" in result.stderr
    assert "COMPUTE STAGE ROLLBACK STEP: LINUX_GROUP=SUCCEEDED" in result.stderr
    assert "COMPUTE STAGE SIDE EFFECT CLASSIFICATION: PARTIAL_ROLLED_BACK" in result.stderr


def test_compute_stage_fails_closed_when_sourced_library_integrity_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = stage_payload()
    integrity = {name: {"integrity_ok": True} for name in handlers.COMPUTE_STAGE_REQUIRED_SCRIPTS}
    integrity["h100-platform-common"] = {"integrity_ok": False}
    monkeypatch.setattr(handlers, "script_integrity", lambda: integrity)
    result = handlers.handle(request("compute.provision.stage", payload, dry_run=False))
    assert result["status"] == "ERROR"
    assert result["error"]["code"] == "SCRIPT_INTEGRITY_FAILED"
    assert result["error"]["scripts"] == ["h100-platform-common"]


def test_compute_stage_retained_scan_holds_derived_image_mapping_quota_and_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = stage_payload()
    username = str(payload["username"])
    plan_id = str(payload["plan_id"])
    project_id = int(payload["project_id"])
    monkeypatch.setattr(handlers.Path, "exists", lambda _path: False)
    monkeypatch.setattr(handlers.Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(
        handlers,
        "_safe_file_lines",
        lambda path: (
            [f"{project_id}:{handlers.PILOT_DATA_ROOT / username}"]
            if path == handlers.PROJECTS_FILE
            else [f"h100_{username}:{project_id}"]
            if path == handlers.PROJID_FILE
            else []
        ),
    )
    monkeypatch.setattr(
        handlers,
        "_registry_entries",
        lambda: [{"username": username, "uid": payload["uid"]}],
    )
    monkeypatch.setattr(handlers, "_assoc_exists", lambda _username: False)
    monkeypatch.setattr(handlers, "_ownership_conflict", lambda _number: ("PASS", None))
    monkeypatch.setattr(handlers, "_used_ssh_ports", lambda: (set(), True))

    def fixed(binary: str, args: list[str], timeout: float = 20.0):  # type: ignore[no-untyped-def]
        if binary == "docker" and args[:2] == ["image", "inspect"]:
            assert args[2] == f"h100-local/dev-container:ubuntu24.04-{username}-{plan_id}"
            return {"ok": True, "stdout": "[]", "stderr": ""}
        if binary == "docker":
            return {"ok": False, "stdout": "", "stderr": "No such container"}
        if binary == "xfs_quota":
            return {
                "ok": True,
                "stdout": f"#{project_id} 0 0 314572800 00 [--------]\n",
                "stderr": "",
            }
        raise AssertionError((binary, args, timeout))

    monkeypatch.setattr(handlers, "run_fixed", fixed)
    retained = handlers._compute_stage_retained_resources(payload)
    assert "derived-image" in retained
    assert "xfs-project-mapping" in retained
    assert "xfs-project-quota" in retained
    assert "gpu-registry" in retained


def test_compute_retry_retained_scan_checks_numeric_identity_and_ssh_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = stage_payload()
    monkeypatch.setattr(handlers.Path, "exists", lambda _path: False)
    monkeypatch.setattr(handlers.Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(handlers, "_safe_file_lines", lambda _path: [])
    monkeypatch.setattr(handlers, "_registry_entries", lambda: [])
    monkeypatch.setattr(handlers, "_assoc_exists", lambda _username: False)
    monkeypatch.setattr(handlers, "_ownership_conflict", lambda _number: ("CONFLICT", "/redacted"))
    monkeypatch.setattr(handlers, "_used_ssh_ports", lambda: ({int(payload["ssh_port"])}, True))
    monkeypatch.setattr(handlers.pwd, "getpwnam", lambda _username: (_ for _ in ()).throw(KeyError))
    monkeypatch.setattr(handlers.grp, "getgrnam", lambda _username: (_ for _ in ()).throw(KeyError))
    monkeypatch.setattr(handlers.pwd, "getpwuid", lambda _uid: object())
    monkeypatch.setattr(handlers.grp, "getgrgid", lambda _gid: object())

    def fixed(binary: str, _args: list[str], timeout: float = 20.0):  # type: ignore[no-untyped-def]
        if binary == "docker":
            return {"ok": False, "stdout": "", "stderr": "No such object"}
        if binary == "xfs_quota":
            return {"ok": True, "stdout": "", "stderr": ""}
        raise AssertionError((binary, timeout))

    monkeypatch.setattr(handlers, "run_fixed", fixed)
    retained = handlers._compute_stage_retained_resources(payload)
    assert {
        "linux-uid",
        "linux-gid",
        "uid-ownership",
        "gid-ownership",
        "ssh-port-allocation",
    }.issubset(retained)
