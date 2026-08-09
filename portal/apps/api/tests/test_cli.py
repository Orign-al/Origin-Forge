import pytest
from h100_portal_api.cli import (
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
    PortalOperation,
    PortalOperationEvent,
    PortalPasswordSetupToken,
    PortalUser,
)
from h100_portal_api.routes.operations import (
    APPROVED_ORIGIN_PILOT_STAGE,
    PORTAL3C_STAGE_APPROVAL_REFERENCE,
    PORTAL3C_STAGE_IDEMPOTENCY_KEY,
)
from sqlalchemy import func, select


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
