from functools import lru_cache
from ipaddress import AddressValueError, IPv4Address
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PORTAL_", case_sensitive=False)

    environment: str = "production"
    database_url: str = "sqlite+pysqlite:////tmp/h100-portal-dev.db"
    secret_key: str = Field(default="development-only-change-me-000000000000", min_length=32)
    worker_socket: str = "/run/h100-portal/worker.sock"
    public_access_host: str = "20.10.10.3"
    allowed_origins: Annotated[tuple[str, ...], NoDecode] = (
        "http://127.0.0.1:18080",
        "http://10.10.10.2",
        "http://10.10.10.2:18080",
        "http://10.10.10.220:18080",
        "http://20.10.10.3:18080",
        "http://20.10.10.3",
        "https://20.10.10.3",
    )
    cookie_secure: bool = False
    cookie_name: str = "h100_session"
    csrf_cookie_name: str = "h100_csrf"
    session_idle_minutes: int = Field(default=30, ge=5, le=240)
    session_absolute_hours: int = Field(default=12, ge=1, le=48)
    reauthentication_minutes: int = Field(default=10, ge=1, le=30)
    initial_password_setup_token_hours: int = Field(default=24, ge=1, le=48)
    password_reset_token_minutes: int = Field(default=30, ge=5, le=120)
    password_action_challenge_minutes: int = Field(default=10, ge=5, le=30)
    password_action_cookie_name: str = "h100_password_action"  # noqa: S105
    login_failures_before_lock: int = Field(default=5, ge=3, le=20)
    login_lock_minutes: int = Field(default=15, ge=1, le=120)
    api_host: str = "127.0.0.1"
    api_port: int = 18081

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("api_host")
    @classmethod
    def localhost_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1"}:
            raise ValueError("Portal API must listen on loopback")
        return value

    @field_validator("public_access_host")
    @classmethod
    def concrete_public_access_host(cls, value: str) -> str:
        try:
            address = IPv4Address(value)
        except AddressValueError as exc:
            raise ValueError("Portal public access host must be one IPv4 address") from exc
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            raise ValueError("Portal public access host must be a concrete non-loopback address")
        return str(address)


@lru_cache
def get_settings() -> Settings:
    return Settings()
