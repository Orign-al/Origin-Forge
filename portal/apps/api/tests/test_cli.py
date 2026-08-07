from h100_portal_api.cli import ensure_origin_al_record, stage_origin_pilot
from h100_portal_api.enums import AccountState, OnboardingState, PasswordState
from h100_portal_api.models import PortalPasswordSetupToken, PortalUser
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
