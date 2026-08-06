import pytest
from h100_portal_api.config import Settings
from h100_portal_api.enums import AccountState, OnboardingState, PasswordState
from h100_portal_api.models import PortalUser
from h100_portal_api.security import (
    digest_secret,
    hash_password,
    normalize_login,
    random_token,
    safe_metadata,
    validate_password,
    verify_password,
)
from sqlalchemy.exc import IntegrityError


def test_private_and_loopback_origins_are_parsed_without_exposing_api() -> None:
    assert Settings.parse_origins("http://127.0.0.1:18080,http://10.10.10.2:18080") == (
        "http://127.0.0.1:18080",
        "http://10.10.10.2:18080",
    )
    with pytest.raises(ValueError, match="loopback"):
        Settings(api_host="10.10.10.2")


def test_login_normalization_is_case_insensitive() -> None:
    assert normalize_login(" Origin-Al ") == "origin-al"
    assert normalize_login("ORIGIN-AL") == "origin-al"


def test_case_variants_conflict_in_database(database) -> None:  # type: ignore[no-untyped-def]
    database.add(
        PortalUser(
            login_name="Origin-al",
            normalized_login=normalize_login("Origin-al"),
            display_name="Origin-al",
            account_state=AccountState.INVITED,
            password_state=PasswordState.SETUP_REQUIRED,
            resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        )
    )
    database.commit()
    database.add(
        PortalUser(
            login_name="ORIGIN-AL",
            normalized_login=normalize_login("ORIGIN-AL"),
            display_name="duplicate",
            account_state=AccountState.INVITED,
            password_state=PasswordState.SETUP_REQUIRED,
            resource_onboarding_state=OnboardingState.NOT_ENROLLED,
        )
    )
    with pytest.raises(IntegrityError):
        database.commit()


def test_argon2id_hash_and_verify() -> None:
    encoded = hash_password("这是一个合规的长口令-Portal")
    assert encoded.startswith("$argon2id$")
    assert verify_password(encoded, "这是一个合规的长口令-Portal")
    assert not verify_password(encoded, "wrong password value")


def test_password_policy_preserves_spaces_and_rejects_weak() -> None:
    validate_password(" 这是一条足够长的口令 and spaces ", "origin-al")
    with pytest.raises(ValueError):
        validate_password("origin-al", "origin-al")
    with pytest.raises(ValueError):
        validate_password("              ", "origin-al")


def test_random_token_has_48_bytes_entropy() -> None:
    first = random_token(48)
    second = random_token(48)
    assert len(first) >= 64
    assert first != second
    assert len(digest_secret(first)) == 64


def test_recursive_redaction() -> None:
    value = safe_metadata(
        {"password": "do-not-log", "nested": {"token": "do-not-log", "ok": "visible"}}
    )
    assert value == {
        "password": "[REDACTED]",
        "nested": {"token": "[REDACTED]", "ok": "visible"},
    }
