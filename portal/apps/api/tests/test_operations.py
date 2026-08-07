import uuid

import pytest
from h100_portal_api.enums import AccountState, OnboardingState, OperationStatus, RiskLevel
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalRole,
    PortalSshKey,
    PortalUser,
    utcnow,
)
from h100_portal_api.operations import can_transition, can_transition_onboarding, risk_for
from h100_portal_api.routes import operations as operations_route
from h100_portal_api.routes.operations import (
    PORTAL3C_STAGE_IDEMPOTENCY_KEY,
    is_portal3c_real_stage,
    persist_portal3c_staged_identity,
    validate_activate_database_bindings,
    validate_operation_payload,
)
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
