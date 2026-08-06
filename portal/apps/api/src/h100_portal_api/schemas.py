import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordState,
    RiskLevel,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class LoginRequest(ApiModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class SetupPasswordRequest(ApiModel):
    token: str = Field(min_length=32, max_length=256)
    password: str = Field(min_length=14, max_length=128)
    confirmation: str = Field(min_length=14, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> SetupPasswordRequest:
        if self.password != self.confirmation:
            raise ValueError("password confirmation does not match")
        return self


class ChangePasswordRequest(ApiModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=14, max_length=128)
    confirmation: str = Field(min_length=14, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> ChangePasswordRequest:
        if self.new_password != self.confirmation:
            raise ValueError("password confirmation does not match")
        return self


class ReauthenticateRequest(ApiModel):
    password: str = Field(min_length=1, max_length=128)


class RoleResponse(ApiModel):
    name: str
    description: str


class UserResponse(ApiModel):
    id: uuid.UUID
    login_name: str
    normalized_login: str
    display_name: str
    unix_username: str | None
    account_state: AccountState
    password_state: PasswordState
    resource_onboarding_state: OnboardingState
    created_at: datetime
    activated_at: datetime | None
    last_login_at: datetime | None
    roles: list[RoleResponse]


class SessionResponse(ApiModel):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    current: bool
    source_ip: str


class OperationCreateRequest(ApiModel):
    operation_type: str = Field(min_length=3, max_length=64)
    target_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    target_id: str = Field(min_length=1, max_length=128)
    request_summary: str = Field(min_length=3, max_length=500)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,128}$")
    confirmation: str | None = Field(default=None, max_length=128)


class OperationResponse(ApiModel):
    id: uuid.UUID
    operation_type: str
    target_type: str
    target_id: str
    requested_by: uuid.UUID
    approved_by: uuid.UUID | None
    request_summary: str
    risk_level: RiskLevel
    status: OperationStatus
    created_at: datetime
    approved_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    dry_run_result: dict[str, Any] | None
    result_summary: str | None
    error_code: str | None


class ApprovalRequest(ApiModel):
    decision: str = Field(pattern=r"^(APPROVE|REJECT)$")
    confirmation: str = Field(min_length=1, max_length=128)
    comment: str | None = Field(default=None, max_length=500)


class OperationSubmitRequest(ApiModel):
    confirmation: str = Field(min_length=1, max_length=128)


class AuditEventResponse(ApiModel):
    event_id: uuid.UUID
    event_type: str
    actor: str
    actor_role: str
    source_ip: str
    object_type: str
    object_id: str
    operation_id: uuid.UUID | None
    result: str
    timestamp: datetime
    safe_metadata: dict[str, Any]


class PageAccessRequest(ApiModel):
    path: str = Field(min_length=1, max_length=128, pattern=r"^/[A-Za-z0-9_./?=&%\-]*$")


class WorkerEnvelope(ApiModel):
    status: str
    data: Any | None = None
    error: dict[str, Any] | None = None
    captured_at: datetime | None = None
