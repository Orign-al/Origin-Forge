from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class PasswordActionExchangeRequest(ApiModel):
    token: str = Field(min_length=32, max_length=256)


class SetupPasswordRequest(ApiModel):
    password: str = Field(min_length=14, max_length=128)
    confirmation: str = Field(min_length=14, max_length=128)

    @model_validator(mode="after")
    def passwords_match(self) -> SetupPasswordRequest:
        if self.password != self.confirmation:
            raise ValueError("password confirmation does not match")
        return self


class PortalUserCreateRequest(ApiModel):
    login_name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    role: str = Field(default="user", pattern=r"^[a-z][a-z_]{2,31}$")
    note: str | None = Field(default=None, max_length=500)

    @field_validator("display_name")
    @classmethod
    def display_name_is_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display name must not be blank")
        return normalized

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ComputeResourceRequestCreate(ApiModel):
    """Closed ordinary-user contract for the standard first compute profile."""

    requested_gpu_max: Literal[0, 1] = 0
    requested_storage_bytes: Literal[322122547200] = 322122547200
    requested_container_profile: Literal["STANDARD_8CPU_32GB"] = "STANDARD_8CPU_32GB"
    requested_lease_seconds: Literal[345600] = 345600
    purpose: str = Field(min_length=1, max_length=1000)
    user_note: str | None = Field(default=None, max_length=1000)
    idempotency_key: uuid.UUID

    @field_validator("purpose")
    @classmethod
    def purpose_is_plain_nonblank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("purpose must not be blank")
        return normalized

    @field_validator("user_note")
    @classmethod
    def normalize_user_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ComputeResourceRequestCancel(ApiModel):
    idempotency_key: uuid.UUID


class ComputeResourceReviewRequest(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    review_note: str | None = Field(default=None, max_length=1000)
    idempotency_key: uuid.UUID

    @field_validator("review_note")
    @classmethod
    def normalize_review_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def rejection_requires_a_note(self) -> ComputeResourceReviewRequest:
        if self.decision == "REJECT" and not self.review_note:
            raise ValueError("review note is required when rejecting a request")
        return self


class ComputeProvisionActionRequest(ApiModel):
    """Plan and dry-run actions accept no allocator or host parameters."""

    idempotency_key: uuid.UUID


class ComputeProvisionRetryAuthorizationRequest(ApiModel):
    """Administrator attestation required before a fresh Stage attempt."""

    idempotency_key: uuid.UUID
    failure_classification: Literal["NO_SIDE_EFFECT", "PARTIAL_ROLLED_BACK"]
    safe_root_cause: str = Field(min_length=1, max_length=500)
    authorization_reason: str = Field(min_length=1, max_length=500)
    remediation_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")

    @field_validator("safe_root_cause", "authorization_reason")
    @classmethod
    def normalize_safe_evidence(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("retry evidence must not be blank")
        return normalized


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


class UserStagePayload(ApiModel):
    """Closed Portal/Worker contract for the approved Stage plan."""

    username: Literal["origin-pilot"]
    uid: int = Field(ge=20_000, le=60_000)
    gid: int = Field(ge=20_000, le=60_000)
    project_id: int = Field(ge=30_000, le=39_999)
    ssh_port: int = Field(ge=1024, le=65_535)
    quota_gb: Literal[300]
    slurm_account: Literal["company"]
    slurm_qos: Literal["general"]
    max_gpus: Literal[1]
    container_name: Literal["gpu-dev-origin-pilot"]
    cpus: Literal[8]
    memory_gb: Literal[32]
    pids_limit: Literal[4096]
    gpu: Literal["none"]
    expected_state: Literal["DRAFT"]
    approval_reference: str | None = Field(
        default=None, min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"
    )


class UserActivatePayload(ApiModel):
    """Activate accepts approved record IDs, never key bytes or host paths."""

    managed_user_id: uuid.UUID
    approved_ssh_key_record_ids: list[uuid.UUID] = Field(min_length=1, max_length=5)
    expected_state: Literal["STAGED"]
    approval_reference: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    dry_run_operation_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def key_records_are_unique(self) -> UserActivatePayload:
        if len(set(self.approved_ssh_key_record_ids)) != len(self.approved_ssh_key_record_ids):
            raise ValueError("duplicate SSH key record ID")
        return self


class SshKeyEnrollRequest(ApiModel):
    """The browser may submit only one public key and non-secret metadata."""

    key_type: Literal["ssh-ed25519", "ecdsa-sha2-nistp256", "sk-ssh-ed25519@openssh.com"]
    public_key: str = Field(min_length=32, max_length=16 * 1024)
    comment: str = Field(default="", max_length=128)
    scope: Literal["HOST", "CONTAINER", "BOTH"] = "BOTH"
    generation_method: Literal["BROWSER_GENERATED", "IMPORTED"]
    client_fingerprint_sha256: str | None = Field(
        default=None, min_length=16, max_length=128, pattern=r"^SHA256:[A-Za-z0-9+/]+$"
    )
    confirmed_private_key_saved: bool = False
    confirmed_public_key: bool = False

    @model_validator(mode="after")
    def enrollment_is_confirmed(self) -> SshKeyEnrollRequest:
        if self.generation_method == "BROWSER_GENERATED":
            if not self.confirmed_private_key_saved:
                raise ValueError("browser-generated private key must be saved before enrollment")
        elif not self.confirmed_public_key:
            raise ValueError("imported public key must be confirmed")
        return self


class SshKeyResponse(ApiModel):
    id: uuid.UUID
    managed_user_id: uuid.UUID
    key_type: str
    fingerprint_sha256: str
    comment: str
    scope: Literal["HOST", "CONTAINER", "BOTH"]
    state: Literal["VALIDATED", "INSTALLED", "REVOKED"]
    generation_method: Literal["BROWSER_GENERATED", "IMPORTED"]
    created_at: datetime
    created_by: uuid.UUID
    validated_at: datetime | None
    installed_at: datetime | None
    revoked_at: datetime | None


class ManagedContainerStartRequest(ApiModel):
    """Closed self-service contract for starting the caller's managed container."""

    idempotency_key: uuid.UUID
    expected_compute_state: Literal["ACTIVE"]
    expected_container_state: Literal["STOPPED"]
    expected_ssh_key_state: Literal["INSTALLED"]


class SelfContainerActionRequest(ApiModel):
    idempotency_key: uuid.UUID


class SelfTerminalCreateRequest(ApiModel):
    idempotency_key: uuid.UUID
    cols: int = Field(default=120, ge=20, le=300)
    rows: int = Field(default=32, ge=5, le=120)


class SelfTerminalInputRequest(ApiModel):
    data: str = Field(min_length=1, max_length=8192)


class SelfTerminalResizeRequest(ApiModel):
    cols: int = Field(ge=20, le=300)
    rows: int = Field(ge=5, le=120)


class SelfJobSubmitRequest(ApiModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    script_path: str = Field(min_length=1, max_length=255)
    workdir: str = Field(default="workspace", min_length=1, max_length=255)
    cpus: int = Field(ge=1, le=8)
    memory_mb: int = Field(ge=256, le=32768)
    gpu_count: Literal[0, 1]
    time_limit_seconds: int = Field(ge=60, le=345600)
    image_ref: str | None = Field(default=None, max_length=512)
    idempotency_key: uuid.UUID


class LeaseRenewalCreateRequest(ApiModel):
    duration_seconds: int = Field(ge=1, le=345600)
    idempotency_key: uuid.UUID


class LeaseDecisionRequest(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    comment: str | None = Field(default=None, max_length=500)


class RestoreCreateRequest(ApiModel):
    duration_seconds: int = Field(ge=1, le=345600)
    idempotency_key: uuid.UUID


class RestoreDecisionRequest(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    comment: str | None = Field(default=None, max_length=500)


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
