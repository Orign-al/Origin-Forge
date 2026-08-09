import uuid

import pytest
from h100_portal_api.enums import AccountState, OnboardingState, OperationStatus, RiskLevel
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalOperationApproval,
    PortalRole,
    PortalSetting,
    PortalSshKey,
    PortalUser,
    utcnow,
)
from h100_portal_api.operations import can_transition, can_transition_onboarding, risk_for
from h100_portal_api.routes import operations as operations_route
from h100_portal_api.routes.operations import (
    APPROVED_PORTAL3F_CLIENT_VALIDATION,
    APPROVED_PORTAL3F_PILOT_ACCEPTANCE,
    APPROVED_PORTAL3G_PRODUCTION_PILOT,
    PORTAL3C_STAGE_IDEMPOTENCY_KEY,
    PORTAL3E_FINAL_APPROVAL_REFERENCE,
    PORTAL3E_FINAL_APPROVAL_TEXT,
    PORTAL3E_FINAL_DRY_RUN_OPERATION_ID,
    PORTAL3E_FINAL_IDEMPOTENCY_KEY,
    PORTAL3E_FINAL_KEY_FINGERPRINT,
    PORTAL3E_FINAL_KEY_RECORD_ID,
    PORTAL3E_FINAL_MANAGED_USER_ID,
    PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY,
    PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY,
    PORTAL3G_CLIENT_VALIDATION_OPERATION_ID,
    PORTAL3G_PILOT_ACCEPTANCE_OPERATION_ID,
    PORTAL3G_PRODUCTION_PILOT_IDEMPOTENCY_KEY,
    OperationPayloadError,
    is_portal3c_real_stage,
    is_portal3e_final_real_activate,
    persist_portal3c_staged_identity,
    persist_portal3e_activated_identity,
    persist_portal3f_client_validation,
    persist_portal3f_pilot_acceptance,
    persist_portal3g_production_pilot,
    validate_activate_database_bindings,
    validate_activate_execution_result,
    validate_activate_worker_result,
    validate_operation_payload,
    validate_persisted_activate_execution_result,
    validate_portal3f_client_validation_plan,
    validate_portal3f_client_validation_result,
    validate_portal3f_pilot_acceptance_plan,
    validate_portal3f_pilot_acceptance_result,
    validate_portal3g_production_pilot_plan,
    validate_portal3g_production_pilot_result,
)
from h100_portal_api.security import safe_metadata
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker


def test_operation_state_machine() -> None:
    assert can_transition(OperationStatus.DRAFT, OperationStatus.PENDING_APPROVAL)
    assert can_transition(OperationStatus.PENDING_APPROVAL, OperationStatus.APPROVED)
    assert can_transition(OperationStatus.APPROVED, OperationStatus.QUEUED)
    assert can_transition(OperationStatus.QUEUED, OperationStatus.RUNNING)
    assert can_transition(OperationStatus.RUNNING, OperationStatus.SUCCEEDED)
    assert can_transition(OperationStatus.ROLLED_BACK, OperationStatus.DRAFT)
    assert not can_transition(OperationStatus.SUCCEEDED, OperationStatus.RUNNING)
    assert can_transition_onboarding(OnboardingState.DRAFT, OnboardingState.STAGED)
    assert can_transition_onboarding(OnboardingState.STAGED, OnboardingState.ACTIVE)
    assert not can_transition_onboarding(OnboardingState.DRAFT, OnboardingState.ACTIVE)


def test_risk_classification() -> None:
    assert risk_for("slurm.resume") == RiskLevel.CRITICAL
    assert risk_for("user.pilot.acceptance") == RiskLevel.CRITICAL
    assert risk_for("slurm.production_pilot.start") == RiskLevel.CRITICAL
    assert risk_for("quota.update") == RiskLevel.HIGH
    assert risk_for("user.plan") == RiskLevel.MEDIUM


def test_operation_payload_rejects_arbitrary_fields_and_protected_users() -> None:
    with pytest.raises(ValueError):
        validate_operation_payload("user.stage", {"username": "root"})
    with pytest.raises(ValueError):
        validate_operation_payload("container.start", {"name": "gpu-dev-user", "command": "sh"})
    with pytest.raises(ValueError):
        validate_operation_payload("slurm.resume", {"node_name": "other-node"})


def test_portal3a_plan_requires_separate_compute_username() -> None:
    assert validate_operation_payload("user.plan", {"username": "origin-pilot"}) == {
        "username": "origin-pilot"
    }
    with pytest.raises(ValueError):
        validate_operation_payload("user.plan", {"username": "origin-al"})


def test_quota_is_bounded() -> None:
    assert (
        validate_operation_payload(
            "quota.update", {"username": "example-user", "quota_bytes": 300 * 1024**3}
        )["quota_bytes"]
        == 300 * 1024**3
    )
    with pytest.raises(ValueError):
        validate_operation_payload("quota.update", {"username": "example-user", "quota_bytes": -1})


def test_stage_payload_has_no_ssh_key_field() -> None:
    payload = {
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
        "approval_reference": "portal3b-r-test-v1",
    }
    assert validate_operation_payload("user.stage", payload)["uid"] == 20001
    no_reference = {key: value for key, value in payload.items() if key != "approval_reference"}
    assert validate_operation_payload("user.stage", no_reference)["uid"] == 20001
    with pytest.raises(ValueError, match="PUBLIC_KEY_NOT_ALLOWED_DURING_STAGE"):
        validate_operation_payload("user.stage", {**payload, "public_key_file": "record-id.pub"})


def test_activate_payload_requires_approved_record_ids_and_rejects_paths() -> None:
    with pytest.raises(ValueError, match="PUBLIC_KEY_REQUIRED_FOR_ACTIVATION"):
        validate_operation_payload(
            "user.activate",
            {
                "managed_user_id": str(uuid.uuid4()),
                "expected_state": "STAGED",
                "approval_reference": "portal3b-r-test-v1",
            },
        )
    valid = {
        "managed_user_id": str(uuid.uuid4()),
        "approved_ssh_key_record_ids": [str(uuid.uuid4())],
        "expected_state": "STAGED",
        "approval_reference": "portal3b-r-test-v1",
    }
    assert validate_operation_payload("user.activate", valid)["expected_state"] == "STAGED"
    with pytest.raises(ValueError, match="ARBITRARY_PATH_REJECTED"):
        validate_operation_payload("user.activate", {**valid, "public_key_path": "/etc/passwd"})


def test_activate_worker_result_requires_structured_host_ssh_policy() -> None:
    result = {
        "approved_ssh_keys": [],
        "host_authorized_keys_install": "PLANNED",
        "container_authorized_keys_install": "PLANNED",
        "host_authorized_keys_plan": [],
        "container_authorized_keys_plan": [],
        "activate_cli_contract": "TARGET_SCOPED_ROOT_CONTROLLED_BUNDLES",
        "execution_enabled": False,
        "host_ssh_policy": {
            "pubkey_authentication": True,
            "password_authentication": False,
            "keyboard_interactive_authentication": False,
            "authentication_methods": ["publickey"],
            "status": "PASSING",
        },
    }
    validate_activate_worker_result([], result)

    missing_policy = {key: value for key, value in result.items() if key != "host_ssh_policy"}
    with pytest.raises(OperationPayloadError, match="ACTIVATE_DRY_RUN_INCOMPLETE"):
        validate_activate_worker_result([], missing_policy)

    password_enabled = {
        **result,
        "host_ssh_policy": {
            **result["host_ssh_policy"],
            "password_authentication": True,
        },
    }
    with pytest.raises(OperationPayloadError, match="ACTIVATE_DRY_RUN_INCOMPLETE"):
        validate_activate_worker_result([], password_enabled)


def portal3e_execution_result(record: PortalSshKey) -> dict[str, object]:
    return {
        "status": "SUCCEEDED",
        "handler": "user.activate",
        "execution_enabled": True,
        "request_id": str(uuid.uuid4()),
        "approved_ssh_keys": [
            {
                "record_id": str(record.id),
                "key_type": record.key_type,
                "fingerprint_sha256": record.fingerprint_sha256,
                "content_sha256": record.content_sha256,
                "scope": record.scope,
                "managed_user_id": str(record.managed_user_id),
                "operation_id": str(record.enrollment_operation_id),
            }
        ],
        "activate": {
            "username": "origin-pilot",
            "uid": 20001,
            "gid": 20001,
            "onboarding_state": "ACTIVE",
            "shell": "/bin/bash",
            "password": "LOCKED",
            "host_access": "ENABLED",
            "ssh_key_state": "INSTALLED",
            "host_authorized_keys": "INSTALLED",
            "container_authorized_keys": "INSTALLED",
            "host_key_fingerprints": [record.fingerprint_sha256],
            "container_key_fingerprints": [record.fingerprint_sha256],
            "host_ssh_policy": {
                "pubkey_authentication": True,
                "password_authentication": False,
                "keyboard_interactive_authentication": False,
                "authentication_methods": ["publickey"],
                "status": "PASSING",
            },
            "container_ssh_policy": {
                "pubkey_authentication": True,
                "password_authentication": False,
                "keyboard_interactive_authentication": False,
                "authentication_methods": "publickey",
                "permit_root_login": "no",
                "authorized_keys_file": ".ssh/authorized_keys",
                "status": "PASSING",
            },
            "host_ssh_server": {
                "service_state": "ACTIVE",
                "config_validation": "PASSED",
                "approved_address": "10.82.36.1",
                "port": 22,
                "status": "READY_FOR_CLIENT_VALIDATION",
            },
            "container_ssh_server": {
                "service_state": "ACTIVE",
                "internal_port": 22,
                "status": "READY_FOR_CLIENT_VALIDATION",
                "bind": {"address": "10.82.36.1", "port": 22023, "status": "LISTENING"},
            },
            "host_server_fingerprint": "SHA256:test-host-server-fingerprint",
            "container_server_fingerprint": "SHA256:test-container-server-fingerprint",
            "management_ssh_policy": {"origin-al": "UNCHANGED", "codexops": "UNCHANGED"},
            "gpu_policy": {
                "unit": "user-20001.slice",
                "device_policy": "closed",
                "device_allow": [],
                "status": "PASSING",
                "out_of_job_gpu": "DENIED",
            },
            "quota": {"project_id": 30001, "hard_limit_gb": 300, "enforcement": "ON"},
            "slurm": {
                "account": "company",
                "qos": "general",
                "max_gpus": 1,
                "node_state": "DRAIN",
                "queue": "EMPTY",
            },
            "container": {
                "name": "gpu-dev-origin-pilot",
                "state": "RUNNING",
                "gpu": "NONE",
                "cpus": 8,
                "memory_gb": 32,
                "pids_limit": 4096,
                "ssh_address": "10.82.36.1",
                "ssh_port": 22023,
            },
            "guard": {
                "status": "PASSING",
                "managed_users": 1,
                "users_verified": 1,
                "nvidia_gpu_count": 4,
                "slurm_gpu_count": 4,
            },
            "host_ssh_client_validation": "PENDING",
            "container_ssh_client_validation": "PENDING",
        },
    }


def create_portal3e_database_state(
    database: Session,
) -> tuple[PortalUser, PortalManagedUser, PortalSshKey, PortalOperation]:
    owner_role = database.scalar(select(PortalRole).where(PortalRole.name == "platform_owner"))
    assert owner_role is not None
    owner = PortalUser(
        login_name="Origin-al",
        normalized_login="origin-al",
        display_name="Origin-al",
        unix_username="origin-al",
        account_state=AccountState.ACTIVE,
        resource_onboarding_state=OnboardingState.STAGED,
        roles=[owner_role],
    )
    database.add(owner)
    database.flush()
    managed = PortalManagedUser(  # noqa: S604 -- ORM shell field, not a subprocess shell.
        id=PORTAL3E_FINAL_MANAGED_USER_ID,
        portal_user_id=owner.id,
        unix_username="origin-pilot",
        uid=20001,
        gid=20001,
        shell="/usr/sbin/nologin",
        host_access_state="DISABLED",
        gpu_isolation_state="VERIFIED",
        slurm_account="company",
        slurm_qos="general",
        project_id=30001,
        quota_bytes=300 * 1024**3,
        container_name="gpu-dev-origin-pilot",
        container_port=22023,
        onboarding_state=OnboardingState.STAGED,
        ssh_key_state="VALIDATED",
        ssh_key_count=1,
        staged_at=utcnow(),
    )
    database.add(managed)
    database.flush()
    enrollment = PortalOperation(
        operation_type="ssh_key.enroll",
        target_type="ssh_public_key",
        target_id=str(PORTAL3E_FINAL_KEY_RECORD_ID),
        requested_by=owner.id,
        owner_managed_user_id=managed.id,
        approved_by=owner.id,
        request_summary="Portal-3D-R approved key enrollment",
        validated_payload={"fingerprint_sha256": PORTAL3E_FINAL_KEY_FINGERPRINT},
        idempotency_key=f"ssh-key-enroll:{PORTAL3E_FINAL_KEY_RECORD_ID}",
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.SUCCEEDED,
    )
    database.add(enrollment)
    database.flush()
    key = PortalSshKey(
        id=PORTAL3E_FINAL_KEY_RECORD_ID,
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        key_type="ssh-ed25519",
        fingerprint_sha256=PORTAL3E_FINAL_KEY_FINGERPRINT,
        public_key="ssh-ed25519 TEST-ONLY-NOT-A-REAL-KEY",
        comment="Portal-3D-R test record",
        scope="BOTH",
        state="VALIDATED",
        generation_method="BROWSER_GENERATED",
        created_by=owner.id,
        enrollment_operation_id=enrollment.id,
        staging_file_name=f"{PORTAL3E_FINAL_KEY_RECORD_ID}.pub",
        content_sha256="a" * 64,
        approved_by=owner.id,
        approved_at=utcnow(),
        validated_at=utcnow(),
        active=True,
    )
    database.add(key)
    database.add(
        PortalContainer(
            managed_user_id=managed.id,
            owner_managed_user_id=managed.id,
            name="gpu-dev-origin-pilot",
            image_digest="sha256:" + "b" * 64,
            ssh_port=22023,
            desired_state="STOPPED",
            observed_state="STOPPED",
            safe_spec={"gpu": "NONE", "authorized_keys": "ABSENT", "max_gpus": 1},
        )
    )
    dry_run = PortalOperation(
        id=PORTAL3E_FINAL_DRY_RUN_OPERATION_ID,
        operation_type="user.activate",
        target_type="compute_identity",
        target_id="origin-pilot",
        requested_by=owner.id,
        owner_managed_user_id=managed.id,
        request_summary="Portal-3E-R dry-run",
        validated_payload={
            "managed_user_id": str(managed.id),
            "approved_ssh_key_record_ids": [str(key.id)],
            "expected_state": "STAGED",
            "approval_reference": "portal3e-r-host-ssh-policy-v1",
        },
        idempotency_key=f"portal3e-r-activate:{uuid.uuid4()}",
        risk_level=RiskLevel.CRITICAL,
        status=OperationStatus.DRAFT,
        dry_run_result={
            "status": "DRY_RUN",
            "activate_status": "READY",
            "execution_enabled": False,
        },
    )
    database.add(dry_run)
    database.flush()
    operation = PortalOperation(
        operation_type="user.activate",
        target_type="compute_identity",
        target_id="origin-pilot",
        requested_by=owner.id,
        owner_managed_user_id=managed.id,
        approved_by=owner.id,
        request_summary="Portal-3E-FINAL real Activate",
        validated_payload={
            "managed_user_id": str(managed.id),
            "approved_ssh_key_record_ids": [str(key.id)],
            "expected_state": "STAGED",
            "approval_reference": PORTAL3E_FINAL_APPROVAL_REFERENCE,
            "dry_run_operation_id": str(PORTAL3E_FINAL_DRY_RUN_OPERATION_ID),
        },
        idempotency_key=PORTAL3E_FINAL_IDEMPOTENCY_KEY,
        risk_level=RiskLevel.CRITICAL,
        status=OperationStatus.QUEUED,
        approved_at=utcnow(),
    )
    database.add(operation)
    database.flush()
    database.add(
        PortalOperationApproval(
            operation_id=operation.id,
            approver_id=owner.id,
            decision="APPROVE",
            safe_comment=PORTAL3E_FINAL_APPROVAL_TEXT,
        )
    )
    database.flush()
    return owner, managed, key, operation


def test_portal3e_final_binding_and_active_persistence(database: Session) -> None:
    owner, managed, key, operation = create_portal3e_database_state(database)
    assert is_portal3e_final_real_activate(database, operation, owner, owner) is True

    result = portal3e_execution_result(key)
    validate_activate_execution_result([key], result)
    persisted = persist_portal3e_activated_identity(
        database, owner=owner, operation=operation, worker_result=result
    )
    database.commit()

    assert persisted.onboarding_state == OnboardingState.ACTIVE
    assert persisted.shell == "/bin/bash"
    assert persisted.host_access_state == "ENABLED"
    assert persisted.ssh_key_state == "INSTALLED"
    assert persisted.ssh_key_count == 1
    assert owner.unix_username == "origin-al"
    assert owner.resource_onboarding_state == OnboardingState.ACTIVE
    assert key.state == "INSTALLED"
    assert key.installed_at is not None
    container = database.scalar(
        select(PortalContainer).where(PortalContainer.managed_user_id == managed.id)
    )
    assert container is not None
    assert container.observed_state == "RUNNING"
    assert container.safe_spec["gpu"] == "NONE"
    assert container.safe_spec["host_ssh_client_validation"] == "PENDING"
    assert container.safe_spec["container_ssh_client_validation"] == "PENDING"
    assert container.safe_spec["slurm_node_state"] == "DRAIN"


def portal3f_server_snapshot(record: PortalSshKey) -> dict[str, object]:
    activate = portal3e_execution_result(record)["activate"]
    assert isinstance(activate, dict)
    return {
        **activate,
        "gpu_health": {
            "count": 4,
            "mig": "DISABLED",
            "dcgm": "4/4 PASS",
            "kernel_errors": "CLEAR",
        },
        "systemd_failed_units": 0,
    }


def test_portal3f_payloads_and_worker_results_are_structured() -> None:
    assert (
        validate_operation_payload(
            "user.ssh_client_validation.record", APPROVED_PORTAL3F_CLIENT_VALIDATION
        )
        == APPROVED_PORTAL3F_CLIENT_VALIDATION
    )
    assert (
        validate_operation_payload("user.pilot.acceptance", APPROVED_PORTAL3F_PILOT_ACCEPTANCE)
        == APPROVED_PORTAL3F_PILOT_ACCEPTANCE
    )
    with pytest.raises(OperationPayloadError, match="PORTAL3F_PLAN_MISMATCH"):
        validate_operation_payload(
            "user.pilot.acceptance",
            {**APPROVED_PORTAL3F_PILOT_ACCEPTANCE, "max_gpus": 2},
        )
    with pytest.raises(OperationPayloadError, match="PORTAL3F_PAYLOAD_REJECTED"):
        validate_operation_payload(
            "user.pilot.acceptance",
            {**APPROVED_PORTAL3F_PILOT_ACCEPTANCE, "argv": ["nvidia-smi"]},
        )

    assert (
        validate_operation_payload(
            "slurm.production_pilot.start", APPROVED_PORTAL3G_PRODUCTION_PILOT
        )
        == APPROVED_PORTAL3G_PRODUCTION_PILOT
    )
    with pytest.raises(OperationPayloadError, match="PORTAL3G_PLAN_MISMATCH"):
        validate_operation_payload(
            "slurm.production_pilot.start",
            {**APPROVED_PORTAL3G_PRODUCTION_PILOT, "max_gpus": 2},
        )
    with pytest.raises(OperationPayloadError, match="PORTAL3G_PAYLOAD_REJECTED"):
        validate_operation_payload(
            "slurm.production_pilot.start",
            {**APPROVED_PORTAL3G_PRODUCTION_PILOT, "command": "scontrol"},
        )


def test_portal3f_client_and_pilot_persistence(database: Session) -> None:
    owner, _managed, key, activate_operation = create_portal3e_database_state(database)
    persist_portal3e_activated_identity(
        database,
        owner=owner,
        operation=activate_operation,
        worker_result=portal3e_execution_result(key),
    )
    snapshot = portal3f_server_snapshot(key)
    client_plan = {
        "status": "DRY_RUN",
        "handler": "user.ssh_client_validation.record",
        "execution_enabled": False,
        "validated_username": "origin-pilot",
        "confirmation_source": "USER_CONFIRMED_REAL_CLIENT_CONNECTIONS",
        "host_client_validation": "PASS",
        "container_client_validation": "PASS",
        "private_key_handling": "NOT_ACCESSED",
        "slurm_execution": "NOT_PERFORMED",
        "server_preflight": snapshot,
    }
    validate_portal3f_client_validation_plan(client_plan)
    client_result = {
        "status": "SUCCEEDED",
        "handler": "user.ssh_client_validation.record",
        "execution_enabled": True,
        "username": "origin-pilot",
        "host_client_validation": "PASS",
        "container_client_validation": "PASS",
        "confirmation_source": "USER_CONFIRMED_REAL_CLIENT_CONNECTIONS",
        "private_key_handling": "NOT_ACCESSED",
        "server_preflight": snapshot,
    }
    validate_portal3f_client_validation_result(client_result)
    client_operation = PortalOperation(
        id=PORTAL3G_CLIENT_VALIDATION_OPERATION_ID,
        operation_type="user.ssh_client_validation.record",
        target_type="compute_identity",
        target_id="origin-pilot",
        requested_by=owner.id,
        approved_by=owner.id,
        request_summary="Portal-3F client validation",
        validated_payload=APPROVED_PORTAL3F_CLIENT_VALIDATION,
        idempotency_key=PORTAL3F_CLIENT_VALIDATION_IDEMPOTENCY_KEY,
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.RUNNING,
    )
    database.add(client_operation)
    database.flush()
    container = persist_portal3f_client_validation(
        database,
        owner=owner,
        operation=client_operation,
        worker_result=client_result,
    )
    assert container.safe_spec["host_ssh_client_validation"] == "PASS"
    assert container.safe_spec["container_ssh_client_validation"] == "PASS"

    pilot_plan = {
        "status": "DRY_RUN",
        "handler": "user.pilot.acceptance",
        "execution_enabled": False,
        "validated_username": "origin-pilot",
        "node_name": "sagsh100server",
        "partition": "notebook",
        "account": "company",
        "qos": "general",
        "max_gpus": 1,
        "image_ref": APPROVED_PORTAL3F_PILOT_ACCEPTANCE["image_ref"],
        "tests": [
            "CPU_JOB",
            "SINGLE_GPU_PYXIS_ENROOT",
            "IN_JOB_ALLOCATED_GPU_ALLOW",
            "IN_JOB_UNALLOCATED_GPU_DENY",
            "IN_JOB_CUDA_CONTEXT",
            "OUT_OF_JOB_GPU_OPEN_DENY_CONCURRENT",
            "OUT_OF_JOB_CUDA_CONTEXT_DENY_CONCURRENT",
        ],
        "final_node_state": "DRAIN",
        "preflight": snapshot,
    }
    validate_portal3f_pilot_acceptance_plan(pilot_plan)
    request_id = str(uuid.uuid4())
    pilot_result = {
        "status": "SUCCEEDED",
        "handler": "user.pilot.acceptance",
        "execution_enabled": True,
        "request_id": request_id,
        "username": "origin-pilot",
        "approval_reference": APPROVED_PORTAL3F_PILOT_ACCEPTANCE["approval_reference"],
        "client_validation": {"host": "PASS", "container": "PASS"},
        "acceptance": {
            "cpu_job_id": 201,
            "gpu_job_id": 202,
            "allocated_gpu_uuid": "GPU-11111111-2222-3333-4444-555555555555",
            "out_of_job_gpu_open": "DENIED",
            "out_of_job_cuda_context": "DENIED",
            "in_job_allocated_gpu": "ALLOWED",
            "in_job_unallocated_gpus": "DENIED",
            "in_job_cuda_context": "PASSED",
            "final_node_state": "DRAIN",
            "worker_log_dir": f"/srv/gpu-platform/platform/logs/portal3f-worker-{request_id}",
        },
        "preflight": snapshot,
        "postflight": snapshot,
        "rollback_status": "NOT_REQUIRED",
    }
    validate_portal3f_pilot_acceptance_result(pilot_result)
    pilot_operation = PortalOperation(
        id=PORTAL3G_PILOT_ACCEPTANCE_OPERATION_ID,
        operation_type="user.pilot.acceptance",
        target_type="compute_identity",
        target_id="origin-pilot",
        requested_by=owner.id,
        approved_by=owner.id,
        request_summary="Portal-3F Pilot acceptance",
        validated_payload=APPROVED_PORTAL3F_PILOT_ACCEPTANCE,
        idempotency_key=PORTAL3F_PILOT_ACCEPTANCE_IDEMPOTENCY_KEY,
        risk_level=RiskLevel.CRITICAL,
        status=OperationStatus.RUNNING,
    )
    database.add(pilot_operation)
    database.flush()
    container = persist_portal3f_pilot_acceptance(
        database,
        owner=owner,
        operation=pilot_operation,
        worker_result=pilot_result,
    )
    assert container.safe_spec["pilot_acceptance_status"] == "PASSED"
    assert container.safe_spec["pilot_cpu_job_id"] == 201
    assert container.safe_spec["pilot_gpu_job_id"] == 202
    assert container.safe_spec["pilot_final_node_state"] == "DRAIN"
    assert container.safe_spec["gpu_scheduling_available"] is False

    client_operation.status = OperationStatus.SUCCEEDED
    pilot_operation.status = OperationStatus.SUCCEEDED
    slurm_snapshot = snapshot["slurm"]
    assert isinstance(slurm_snapshot, dict)
    idle_snapshot = {
        **snapshot,
        "slurm": {**slurm_snapshot, "node_state": "IDLE", "drain_reason": "NONE"},
    }
    production_plan = {
        "status": "DRY_RUN",
        "handler": "slurm.production_pilot.start",
        "execution_enabled": False,
        "validated_username": "origin-pilot",
        "node_name": "sagsh100server",
        "action": "RESUME",
        "production_pilot_scope": {
            "mode": "SINGLE_NODE",
            "managed_users": ["origin-pilot"],
            "max_gpus": 1,
        },
        "client_validation_operation_id": str(PORTAL3G_CLIENT_VALIDATION_OPERATION_ID),
        "pilot_acceptance_operation_id": str(PORTAL3G_PILOT_ACCEPTANCE_OPERATION_ID),
        "expected_node_state": "DRAIN",
        "target_node_state": "IDLE",
        "jobs_submitted": 0,
        "preflight": snapshot,
        "failure_action": "DRAIN",
    }
    validate_portal3g_production_pilot_plan(production_plan)
    production_result = {
        "status": "SUCCEEDED",
        "handler": "slurm.production_pilot.start",
        "execution_enabled": True,
        "username": "origin-pilot",
        "node_name": "sagsh100server",
        "action": "RESUME",
        "previous_node_state": "DRAIN",
        "node": {
            "name": "sagsh100server",
            "state": "IDLE",
            "queue": "EMPTY",
            "reason": "NONE",
            "jobs_submitted": 0,
        },
        "scheduler": "AVAILABLE",
        "production_pilot_state": "ACTIVE",
        "production_pilot_scope": production_plan["production_pilot_scope"],
        "client_validation_operation_id": str(PORTAL3G_CLIENT_VALIDATION_OPERATION_ID),
        "pilot_acceptance_operation_id": str(PORTAL3G_PILOT_ACCEPTANCE_OPERATION_ID),
        "approval_reference": APPROVED_PORTAL3G_PRODUCTION_PILOT["approval_reference"],
        "jobs_submitted": 0,
        "preflight": snapshot,
        "postflight": idle_snapshot,
        "rollback_status": "NOT_REQUIRED",
    }
    validate_portal3g_production_pilot_result(production_result)
    production_operation = PortalOperation(
        operation_type="slurm.production_pilot.start",
        target_type="slurm_node",
        target_id="sagsh100server",
        requested_by=owner.id,
        approved_by=owner.id,
        request_summary="Portal-3G Production Pilot start",
        validated_payload=APPROVED_PORTAL3G_PRODUCTION_PILOT,
        idempotency_key=PORTAL3G_PRODUCTION_PILOT_IDEMPOTENCY_KEY,
        risk_level=RiskLevel.CRITICAL,
        status=OperationStatus.RUNNING,
    )
    database.add(production_operation)
    database.flush()
    setting = persist_portal3g_production_pilot(
        database,
        owner=owner,
        operation=production_operation,
        worker_result=production_result,
    )
    database.commit()
    assert database.get(PortalSetting, setting.key) is setting
    assert setting.value["state"] == "ACTIVE"
    assert setting.value["node_state"] == "IDLE"
    assert setting.value["per_user_max_gpu"] == 1
    assert container.safe_spec["slurm_node_state"] == "IDLE"
    assert container.safe_spec["gpu_scheduling_available"] is True


def test_portal3e_persisted_result_revalidates_redacted_password_state(
    database: Session,
) -> None:
    _owner, _managed, key, _operation = create_portal3e_database_state(database)
    raw_result = portal3e_execution_result(key)
    persisted_result = safe_metadata(raw_result)
    assert isinstance(persisted_result, dict)

    with pytest.raises(OperationPayloadError, match="ACTIVATE_RESULT_INCOMPLETE"):
        validate_activate_execution_result([key], persisted_result)

    activate = validate_persisted_activate_execution_result([key], persisted_result)
    assert activate["password"] == "LOCKED"


def test_portal3e_persisted_result_requires_exact_redaction_marker(database: Session) -> None:
    _owner, _managed, key, _operation = create_portal3e_database_state(database)
    raw_result = portal3e_execution_result(key)

    with pytest.raises(OperationPayloadError, match="password redaction marker"):
        validate_persisted_activate_execution_result([key], raw_result)


def test_portal3e_final_execution_result_rejects_container_password_authentication() -> None:
    key = PortalSshKey(
        id=PORTAL3E_FINAL_KEY_RECORD_ID,
        managed_user_id=PORTAL3E_FINAL_MANAGED_USER_ID,
        owner_managed_user_id=PORTAL3E_FINAL_MANAGED_USER_ID,
        key_type="ssh-ed25519",
        fingerprint_sha256=PORTAL3E_FINAL_KEY_FINGERPRINT,
        public_key="ssh-ed25519 TEST-ONLY-NOT-A-REAL-KEY",
        comment="test",
        scope="BOTH",
        state="VALIDATED",
        generation_method="IMPORTED",
        created_by=uuid.uuid4(),
        enrollment_operation_id=uuid.uuid4(),
        staging_file_name=f"{PORTAL3E_FINAL_KEY_RECORD_ID}.pub",
        content_sha256="a" * 64,
        validated_at=utcnow(),
        active=True,
    )
    result = portal3e_execution_result(key)
    activate = result["activate"]
    assert isinstance(activate, dict)
    container_policy = activate["container_ssh_policy"]
    assert isinstance(container_policy, dict)
    container_policy["password_authentication"] = True
    with pytest.raises(OperationPayloadError, match="ACTIVATE_RESULT_INCOMPLETE"):
        validate_activate_execution_result([key], result)


def test_activate_ids_are_bound_to_the_owners_staged_identity(database: Session) -> None:
    owner = PortalUser(
        login_name="Origin-al",
        normalized_login="origin-al",
        display_name="Origin-al",
        unix_username="origin-al",
    )
    database.add(owner)
    database.flush()
    managed = PortalManagedUser(  # noqa: S604 -- ORM field, not subprocess shell execution.
        portal_user_id=owner.id,
        unix_username="origin-pilot",
        uid=20001,
        gid=20001,
        shell="/usr/sbin/nologin",
        host_access_state="NOT_ACTIVATED",
        gpu_isolation_state="VERIFIED",
        onboarding_state=OnboardingState.STAGED,
        ssh_key_state="REQUIRED_BEFORE_ACTIVATION",
        ssh_key_count=0,
    )
    database.add(managed)
    database.flush()
    key_id = uuid.uuid4()
    enrollment = PortalOperation(
        operation_type="ssh_key.enroll",
        target_type="ssh_public_key",
        target_id=str(key_id),
        requested_by=owner.id,
        owner_managed_user_id=managed.id,
        approved_by=owner.id,
        request_summary="test self-service SSH key enrollment",
        validated_payload={"fingerprint_sha256": "SHA256:portal3br-test-fingerprint"},
        idempotency_key=f"ssh-key-enroll:{key_id}",
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.SUCCEEDED,
    )
    database.add(enrollment)
    database.flush()
    key = PortalSshKey(
        id=key_id,
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        key_type="ssh-ed25519",
        fingerprint_sha256="SHA256:portal3br-test-fingerprint",
        public_key="ssh-ed25519 TEST-ONLY-NOT-A-REAL-KEY",
        comment="test self-service record",
        scope="BOTH",
        state="VALIDATED",
        generation_method="IMPORTED",
        created_by=owner.id,
        enrollment_operation_id=enrollment.id,
        staging_file_name=f"{key_id}.pub",
        content_sha256="a" * 64,
        approved_by=owner.id,
        approved_at=utcnow(),
        validated_at=utcnow(),
        active=True,
    )
    database.add(key)
    database.flush()
    payload = {
        "managed_user_id": str(managed.id),
        "approved_ssh_key_record_ids": [str(key.id)],
        "expected_state": "STAGED",
        "approval_reference": "portal3b-r-test-v1",
    }

    validate_activate_database_bindings(
        database, owner_id=owner.id, target_id="origin-pilot", payload=payload
    )
    with pytest.raises(ValueError, match="USER_NOT_IN_STAGED_STATE"):
        validate_activate_database_bindings(
            database, owner_id=uuid.uuid4(), target_id="origin-pilot", payload=payload
        )


def test_portal3c_stage_result_persists_separate_staged_identity(database: Session) -> None:
    owner_role = database.scalar(select(PortalRole).where(PortalRole.name == "platform_owner"))
    assert owner_role is not None
    owner = PortalUser(
        login_name="Origin-al",
        normalized_login="origin-al",
        display_name="Origin-al",
        unix_username="origin-al",
        account_state=AccountState.ACTIVE,
        resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        roles=[owner_role],
    )
    database.add(owner)
    database.flush()
    payload = {
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
        "approval_reference": "portal3b-r-lifecycle-revalidated",
    }
    operation = PortalOperation(
        operation_type="user.stage",
        target_type="compute_identity",
        target_id="origin-pilot",
        requested_by=owner.id,
        approved_by=owner.id,
        request_summary="Portal-3C Stage",
        validated_payload=payload,
        idempotency_key=PORTAL3C_STAGE_IDEMPOTENCY_KEY,
        risk_level=RiskLevel.HIGH,
        status=OperationStatus.RUNNING,
    )
    database.add(operation)
    database.flush()
    stage = {
        "username": "origin-pilot",
        "uid": 20001,
        "gid": 20001,
        "shell": "/usr/sbin/nologin",
        "password": "LOCKED",
        "authorized_keys": "ABSENT",
        "host_access": "DISABLED",
        "onboarding_state": "STAGED",
        "ssh_key_state": "REQUIRED_BEFORE_ACTIVATION",
        "gpu_policy": {
            "unit": "user-20001.slice",
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
            "image_digest": "sha256:" + "b" * 64,
        },
        "guard": {"timer": "ENABLED_ACTIVE", "status": "PASSING"},
    }
    assert is_portal3c_real_stage(operation, owner, owner) is True
    managed = persist_portal3c_staged_identity(
        database,
        owner=owner,
        operation=operation,
        worker_result={"status": "SUCCEEDED", "stage": stage},
    )
    database.commit()

    assert owner.unix_username == "origin-al"
    assert owner.resource_onboarding_state == OnboardingState.STAGED
    assert managed.unix_username == "origin-pilot"
    assert (managed.uid, managed.gid, managed.shell) == (20001, 20001, "/usr/sbin/nologin")
    assert managed.ssh_key_count == 0
    assert managed.ssh_key_state == "REQUIRED_BEFORE_ACTIVATION"
    container = database.scalar(
        select(PortalContainer).where(PortalContainer.managed_user_id == managed.id)
    )
    assert container is not None
    assert container.observed_state == "STOPPED"
    assert container.safe_spec["gpu"] == "NONE"
    audit_types = set(database.scalars(select(PortalAuditEvent.event_type)).all())
    assert {
        "user.stage.linux",
        "user.stage.gpu_policy",
        "user.stage.gpu_self_test",
        "user.stage.quota",
        "user.stage.slurm",
        "user.stage.container",
        "user.stage.guard",
        "user.stage.completed",
    } <= audit_types


@pytest.mark.parametrize("failure_kind", ["runtime", "integrity"])
def test_execute_stage_rolls_back_partial_portal_persistence(
    database: Session, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    owner_role = database.scalar(select(PortalRole).where(PortalRole.name == "platform_owner"))
    assert owner_role is not None
    owner = PortalUser(
        login_name="Origin-al",
        normalized_login="origin-al",
        display_name="Origin-al",
        unix_username="origin-al",
        account_state=AccountState.ACTIVE,
        resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        roles=[owner_role],
    )
    database.add(owner)
    database.flush()
    payload = {
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
        "approval_reference": "portal3b-r-lifecycle-revalidated",
    }
    operation = PortalOperation(
        operation_type="user.stage",
        target_type="compute_identity",
        target_id="origin-pilot",
        requested_by=owner.id,
        approved_by=owner.id,
        request_summary="Portal-3C Stage persistence failure test",
        validated_payload=payload,
        idempotency_key=PORTAL3C_STAGE_IDEMPOTENCY_KEY,
        risk_level=RiskLevel.HIGH,
        status=OperationStatus.QUEUED,
    )
    database.add(operation)
    database.commit()

    test_session = sessionmaker(bind=database.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(operations_route, "SessionLocal", test_session)
    monkeypatch.setattr(
        operations_route,
        "call_worker",
        lambda *args, **kwargs: {"status": "SUCCEEDED", "request_id": "test-worker"},
    )

    def persist_then_fail(
        db: Session,
        *,
        owner: PortalUser,
        operation: PortalOperation,
        worker_result: dict[str, object],
    ) -> PortalManagedUser:
        del operation, worker_result
        owner.resource_onboarding_state = OnboardingState.STAGED
        managed = PortalManagedUser(  # noqa: S604 -- ORM shell field, no subprocess shell.
            portal_user_id=owner.id,
            unix_username="origin-pilot",
            uid=20001,
            gid=20001,
            shell="/usr/sbin/nologin",
            host_access_state="DISABLED",
            gpu_isolation_state="VERIFIED",
            onboarding_state=OnboardingState.STAGED,
            ssh_key_state="REQUIRED_BEFORE_ACTIVATION",
            ssh_key_count=0,
        )
        db.add(managed)
        db.flush()
        if failure_kind == "integrity":
            raise IntegrityError("INSERT", {}, RuntimeError("constraint rejected"))
        raise RuntimeError("simulated Portal Stage persistence rejection")

    monkeypatch.setattr(operations_route, "persist_portal3c_staged_identity", persist_then_fail)
    operations_route.execute_operation(operation.id, "origin-al")

    with test_session() as verification:
        failed = verification.get(PortalOperation, operation.id)
        assert failed is not None
        assert failed.status == OperationStatus.FAILED
        assert failed.error_code == "PORTAL_STAGE_STATE_REJECTED"
        assert failed.rollback_status == "REQUIRES_MANUAL_REVIEW"
        assert verification.scalar(select(PortalManagedUser)) is None
        preserved_owner = verification.get(PortalUser, owner.id)
        assert preserved_owner is not None
        assert preserved_owner.resource_onboarding_state == OnboardingState.NOT_ENROLLED
        assert "portal.stage_persist_failed" in set(
            verification.scalars(select(PortalAuditEvent.event_type)).all()
        )
