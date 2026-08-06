import uuid

import pytest
from h100_portal_api.enums import OnboardingState, OperationStatus, RiskLevel
from h100_portal_api.models import PortalManagedUser, PortalSshKey, PortalUser, utcnow
from h100_portal_api.operations import can_transition, can_transition_onboarding, risk_for
from h100_portal_api.routes.operations import (
    validate_activate_database_bindings,
    validate_operation_payload,
)
from sqlalchemy.orm import Session


def test_operation_state_machine() -> None:
    assert can_transition(OperationStatus.DRAFT, OperationStatus.PENDING_APPROVAL)
    assert can_transition(OperationStatus.PENDING_APPROVAL, OperationStatus.APPROVED)
    assert can_transition(OperationStatus.APPROVED, OperationStatus.QUEUED)
    assert can_transition(OperationStatus.QUEUED, OperationStatus.RUNNING)
    assert can_transition(OperationStatus.RUNNING, OperationStatus.SUCCEEDED)
    assert not can_transition(OperationStatus.SUCCEEDED, OperationStatus.RUNNING)
    assert can_transition_onboarding(OnboardingState.DRAFT, OnboardingState.STAGED)
    assert can_transition_onboarding(OnboardingState.STAGED, OnboardingState.ACTIVE)
    assert not can_transition_onboarding(OnboardingState.DRAFT, OnboardingState.ACTIVE)


def test_risk_classification() -> None:
    assert risk_for("slurm.resume") == RiskLevel.CRITICAL
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
    key = PortalSshKey(
        id=key_id,
        managed_user_id=managed.id,
        key_type="ssh-ed25519",
        fingerprint="SHA256:portal3br-test-fingerprint",
        comment_summary="test record without key body",
        public_key_ciphertext=None,
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
