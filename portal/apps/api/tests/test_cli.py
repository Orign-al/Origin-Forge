from datetime import UTC, datetime, timedelta

import pytest
from h100_portal_api import cli as cli_module
from h100_portal_api.cli import (
    GPU_RUNTIME_DECOUPLE_APPROVAL,
    decouple_gpu_development_containers,
    ensure_origin_al_record,
    portal3f_origin_pilot,
    portal3g_start_production_pilot,
    reopen_portal3c_stage_retry,
    stage_origin_pilot,
)
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordState,
    RiskLevel,
)
from h100_portal_api.models import (
    PortalAuditEvent,
    PortalComputeLease,
    PortalContainer,
    PortalManagedUser,
    PortalOperation,
    PortalOperationEvent,
    PortalPasswordSetupToken,
    PortalSshKey,
    PortalUser,
)
from h100_portal_api.routes.operations import (
    APPROVED_ORIGIN_PILOT_STAGE,
    PORTAL3C_STAGE_APPROVAL_REFERENCE,
    PORTAL3C_STAGE_IDEMPOTENCY_KEY,
)
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


def test_prepare_origin_record_is_idempotent_and_does_not_issue_token(database) -> None:  # type: ignore[no-untyped-def]
    user, created = ensure_origin_al_record(database)
    database.commit()

    assert created is True
    assert user.login_name == "Origin-al"
    assert user.normalized_login == "origin-al"
    assert user.unix_username == "origin-al"
    assert user.account_state == AccountState.INVITED
    assert user.password_state == PasswordState.SETUP_REQUIRED
    assert user.resource_onboarding_state == OnboardingState.NOT_ENROLLED
    assert [role.name for role in user.roles] == ["platform_owner"]

    same_user, created_again = ensure_origin_al_record(database)
    database.commit()
    assert created_again is False
    assert same_user.id == user.id
    assert database.scalar(select(func.count()).select_from(PortalUser)) == 1
    assert database.scalar(select(func.count()).select_from(PortalPasswordSetupToken)) == 0


def test_portal3c_console_stage_requires_exact_approval_text() -> None:
    assert stage_origin_pilot("Stage origin-pilot") == 2


def test_portal3f_console_requires_exact_approval_text() -> None:
    assert portal3f_origin_pilot("run pilot") == 2


def test_portal3g_console_requires_exact_approval_text() -> None:
    assert portal3g_start_production_pilot("resume all nodes") == 2


def test_gpu_runtime_decouple_console_requires_exact_approval_text() -> None:
    assert decouple_gpu_development_containers("decouple GPU containers") == 2


def test_gpu_runtime_decouple_resumes_running_operation_and_restarts_gpu_less(
    database, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    owner, _created = ensure_origin_al_record(database)
    user_role = database.scalar(
        select(cli_module.PortalRole).where(cli_module.PortalRole.name == "user")
    )
    assert user_role is not None
    now = datetime.now(UTC)
    user = PortalUser(
        login_name="gpu-decouple-user",
        normalized_login="gpu-decouple-user",
        display_name="GPU Decouple User",
        unix_username="gpu-decouple-user",
        account_state=AccountState.ACTIVE,
        password_state=PasswordState.SET,
        resource_onboarding_state=OnboardingState.ACTIVE,
        activated_at=now,
        roles=[user_role],
    )
    database.add(user)
    database.flush()
    managed = PortalManagedUser(  # noqa: S604 -- ORM shell field, not subprocess execution.
        portal_user_id=user.id,
        unix_username="gpu-decouple-user",
        uid=20101,
        gid=20101,
        shell="/usr/sbin/nologin",
        host_access_state="DISABLED_BY_PLATFORM_POLICY",
        compute_environment_state="ACTIVE",
        gpu_isolation_state="VERIFIED",
        slurm_account="company",
        slurm_qos="general",
        project_id=30101,
        quota_bytes=300 * 1024**3,
        container_name="gpu-dev-gpu-decouple-user",
        container_port=22101,
        onboarding_state=OnboardingState.ACTIVE,
        ssh_key_state="SSH_READY",
        ssh_key_count=1,
        compute_activated_at=now,
    )
    database.add(managed)
    database.flush()
    enrollment = PortalOperation(
        operation_type="ssh_key.enroll.fixture",
        target_type="ssh_public_key",
        target_id=managed.unix_username,
        requested_by=user.id,
        owner_managed_user_id=managed.id,
        approved_by=user.id,
        request_summary="fixture SSH key enrollment",
        validated_payload={},
        idempotency_key="fixture-key:gpu-decouple-user",
        risk_level=RiskLevel.MEDIUM,
        status=OperationStatus.SUCCEEDED,
    )
    database.add(enrollment)
    database.flush()
    database.add(
        PortalSshKey(
            managed_user_id=managed.id,
            owner_managed_user_id=managed.id,
            key_type="ssh-ed25519",
            fingerprint_sha256="SHA256:gpu-decouple-fixture",
            public_key="ssh-ed25519 fixture",
            comment="gpu decouple fixture",
            scope="CONTAINER",
            state="INSTALLED",
            host_install_state="REMOVED_BY_POLICY",
            container_install_state="INSTALLED",
            generation_method="IMPORTED",
            created_by=user.id,
            enrollment_operation_id=enrollment.id,
            staging_file_name="gpu-decouple-fixture.pub",
            content_sha256="1" * 64,
            approved_by=user.id,
            approved_at=now,
            validated_at=now,
            installed_at=now,
            active=True,
        )
    )
    container = PortalContainer(
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        name=managed.container_name,
        image_digest="sha256:" + "2" * 64,
        ssh_port=managed.container_port,
        desired_state="RUNNING",
        observed_state="RUNNING",
        development_profile="GPU_1_8CPU_32GB",
        gpu_count=1,
        gpu_allocation_job_id=8801,
        gpu_allocation_uuid="GPU-11111111-2222-3333-4444-555555555555",
        safe_spec={"gpu": "SLURM_ALLOCATED_1", "privileged": False},
    )
    lease = PortalComputeLease(
        managed_user_id=managed.id,
        owner_managed_user_id=managed.id,
        state="ACTIVE",
        gpu_count=1,
        starts_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=95),
        duration_seconds=345600,
        max_duration_seconds=345600,
        renewal_window_seconds=86400,
        approved_at=now - timedelta(hours=1),
        approved_by=owner.id,
    )
    database.add_all([container, lease])
    database.flush()
    operation = PortalOperation(
        operation_type="container.runtime.decouple_gpu",
        target_type="container",
        target_id=container.name,
        requested_by=owner.id,
        owner_managed_user_id=managed.id,
        approved_by=owner.id,
        request_summary="fixture interrupted GPU runtime decoupling",
        validated_payload={},
        idempotency_key=f"gpu-runtime-decouple:{managed.id}:v1",
        risk_level=RiskLevel.HIGH,
        status=OperationStatus.RUNNING,
        approved_at=now,
        started_at=now,
    )
    database.add(operation)
    database.commit()

    calls: list[tuple[str, bool, int | None]] = []

    def worker(operation_type: str, **kwargs: object) -> dict[str, object]:
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        dry_run = kwargs["dry_run"] is True
        calls.append((operation_type, dry_run, payload.get("gpu_allocation_job_id")))
        if dry_run:
            return {"status": "DRY_RUN", "target_runtime_gpu": "NONE"}
        if operation_type == "container.stop_after_profile_upgrade":
            return {
                "status": "SUCCEEDED",
                "container_state": "STOPPED",
                "container_gpu": "NONE",
                "gpu_allocation_job_id": None,
                "gpu_allocation_uuid": None,
            }
        return {
            "status": "SUCCEEDED",
            "container_state": "RUNNING",
            "container_gpu": "NONE",
            "gpu_allocation_job_id": None,
            "gpu_allocation_uuid": None,
        }

    factory = sessionmaker(bind=database.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(cli_module, "SessionLocal", factory)
    monkeypatch.setattr(cli_module, "call_worker", worker)

    assert decouple_gpu_development_containers(GPU_RUNTIME_DECOUPLE_APPROVAL) == 0

    database.expire_all()
    migrated = database.get(PortalContainer, container.id)
    resumed = database.get(PortalOperation, operation.id)
    assert migrated is not None and resumed is not None
    assert migrated.observed_state == migrated.desired_state == "RUNNING"
    assert migrated.gpu_allocation_job_id is None
    assert migrated.gpu_allocation_uuid is None
    assert migrated.safe_spec["gpu"] == "NONE"
    assert resumed.status == OperationStatus.SUCCEEDED
    assert calls == [
        ("container.stop_after_profile_upgrade", True, 8801),
        ("container.stop_after_profile_upgrade", False, 8801),
        ("container.start_after_profile_upgrade", True, None),
        ("container.start_after_profile_upgrade", False, None),
    ]


def test_portal3c_retry_reopens_only_the_exact_rolled_back_operation(database) -> None:  # type: ignore[no-untyped-def]
    owner, _created = ensure_origin_al_record(database)
    operation = PortalOperation(
        operation_type="user.stage",
        target_type="compute_identity",
        target_id="origin-pilot",
        requested_by=owner.id,
        approved_by=owner.id,
        request_summary="Portal-3C failed Stage",
        validated_payload={
            **APPROVED_ORIGIN_PILOT_STAGE,
            "approval_reference": PORTAL3C_STAGE_APPROVAL_REFERENCE,
        },
        idempotency_key=PORTAL3C_STAGE_IDEMPOTENCY_KEY,
        risk_level=RiskLevel.HIGH,
        status=OperationStatus.ROLLED_BACK,
        error_code="STAGE_EXECUTION_FAILED",
        rollback_status="ROLLED_BACK",
    )
    database.add(operation)
    database.flush()

    reopen_portal3c_stage_retry(database, operation=operation, owner=owner)
    database.flush()

    assert operation.status == OperationStatus.DRAFT
    assert operation.approved_by is None
    assert operation.error_code is None
    assert operation.rollback_status is None
    assert database.scalar(
        select(PortalOperationEvent).where(
            PortalOperationEvent.operation_id == operation.id,
            PortalOperationEvent.to_status == OperationStatus.DRAFT,
        )
    )
    assert database.scalar(
        select(PortalAuditEvent).where(
            PortalAuditEvent.operation_id == operation.id,
            PortalAuditEvent.event_type == "user.stage.retry_reopened",
        )
    )

    operation.status = OperationStatus.ROLLED_BACK
    operation.approved_by = owner.id
    operation.error_code = "DIFFERENT_FAILURE"
    operation.rollback_status = "ROLLED_BACK"
    with pytest.raises(RuntimeError, match="not eligible"):
        reopen_portal3c_stage_retry(database, operation=operation, owner=owner)
