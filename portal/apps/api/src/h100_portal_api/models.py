from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from h100_portal_api.database import Base
from h100_portal_api.enums import (
    AccountState,
    OnboardingState,
    OperationStatus,
    PasswordActionPurpose,
    PasswordActionTokenState,
    PasswordState,
    RiskLevel,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Normalize database datetimes for dialects that omit timezone metadata."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


portal_user_roles = Table(
    "portal_user_roles",
    Base.metadata,
    Column("user_id", ForeignKey("portal_users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", ForeignKey("portal_roles.id", ondelete="CASCADE"), primary_key=True),
)


class PortalRole(Base):
    __tablename__ = "portal_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortalUser(Base):
    __tablename__ = "portal_users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    login_name: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_login: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    unix_username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_state: Mapped[AccountState] = mapped_column(
        Enum(AccountState, native_enum=False, length=32), default=AccountState.INVITED
    )
    password_state: Mapped[PasswordState] = mapped_column(
        Enum(PasswordState, native_enum=False, length=32), default=PasswordState.SETUP_REQUIRED
    )
    resource_onboarding_state: Mapped[OnboardingState] = mapped_column(
        Enum(OnboardingState, native_enum=False, length=40), default=OnboardingState.NOT_ENROLLED
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    roles: Mapped[list[PortalRole]] = relationship(secondary=portal_user_roles, lazy="selectin")


class PortalPasswordCredential(Base):
    __tablename__ = "portal_password_credentials"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortalPasswordSetupToken(Base):
    __tablename__ = "portal_password_setup_tokens"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('INITIAL_PASSWORD_SETUP', 'PASSWORD_RESET')",
            name="ck_password_action_purpose",
        ),
        CheckConstraint(
            "state IN ('ACTIVE', 'USED', 'REVOKED', 'EXPIRED')",
            name="ck_password_action_state",
        ),
        Index("ix_password_action_user_purpose_state", "user_id", "purpose", "state"),
        Index("ix_password_action_state_expires", "state", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    purpose: Mapped[PasswordActionPurpose] = mapped_column(
        Enum(PasswordActionPurpose, native_enum=False, length=32),
        default=PasswordActionPurpose.INITIAL_PASSWORD_SETUP,
        nullable=False,
    )
    state: Mapped[PasswordActionTokenState] = mapped_column(
        Enum(PasswordActionTokenState, native_enum=False, length=16),
        default=PasswordActionTokenState.ACTIVE,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portal_users.id"), nullable=True, index=True
    )
    request_ip_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    challenge_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    challenge_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exchanged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalSession(Base):
    __tablename__ = "portal_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_managed_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    session_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reauthenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalCliToken(Base):
    __tablename__ = "portal_cli_tokens"
    __table_args__ = (
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="ck_cli_token_expiry"
        ),
        Index("ix_cli_token_user_active", "user_id", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalDelegatedTestSession(Base):
    """Short-lived, scope-limited bearer delegation for controlled acceptance tests."""

    __tablename__ = "portal_delegated_test_sessions"
    __table_args__ = (
        CheckConstraint(
            "actor_user_id <> effective_user_id", name="ck_delegated_test_distinct_users"
        ),
        CheckConstraint("ttl_seconds BETWEEN 600 AND 900", name="ck_delegated_test_ttl"),
        CheckConstraint("expires_at > created_at", name="ck_delegated_test_expiry_order"),
        Index("ix_delegated_test_expiry", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    effective_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalOperation(Base):
    __tablename__ = "portal_operations"
    __table_args__ = (UniqueConstraint("requested_by", "idempotency_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id"), nullable=False, index=True
    )
    owner_managed_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_users.id"))
    request_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    validated_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(
        Enum(RiskLevel, native_enum=False, length=16), nullable=False
    )
    status: Mapped[OperationStatus] = mapped_column(
        Enum(OperationStatus, native_enum=False, length=32), default=OperationStatus.DRAFT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_execution_id: Mapped[str | None] = mapped_column(String(64))
    dry_run_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result_summary: Mapped[str | None] = mapped_column(String(1000))
    error_code: Mapped[str | None] = mapped_column(String(64))
    rollback_status: Mapped[str | None] = mapped_column(String(32))
    related_report: Mapped[str | None] = mapped_column(String(255))
    related_git_commit: Mapped[str | None] = mapped_column(String(64))


class PortalOperationApproval(Base):
    __tablename__ = "portal_operation_approvals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_operations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    approver_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_users.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    safe_comment: Mapped[str | None] = mapped_column(String(500))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortalOperationEvent(Base):
    __tablename__ = "portal_operation_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_operations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    safe_message: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortalAuditEvent(Base):
    __tablename__ = "portal_audit_events"
    __table_args__ = (Index("ix_audit_timestamp", "timestamp"),)

    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_operations.id"))
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class PortalSystemSnapshot(Base):
    __tablename__ = "portal_system_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    snapshot_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortalManagedUser(Base):
    __tablename__ = "portal_managed_users"
    __table_args__ = (
        CheckConstraint("ssh_key_count BETWEEN 0 AND 5", name="ck_managed_user_ssh_key_count"),
        CheckConstraint(
            "onboarding_state != 'ACTIVE' OR "
            "(ssh_key_count > 0 AND compute_activated_at IS NOT NULL "
            "AND host_access_state IN ('ENABLED', 'DISABLED_BY_PLATFORM_POLICY'))",
            name="ck_managed_user_active_login_contract",
        ),
        CheckConstraint(
            "onboarding_state NOT IN ('DRAFT', 'STAGED') OR ssh_key_state != 'SSH_READY'",
            name="ck_managed_user_no_early_ssh_ready",
        ),
        CheckConstraint(
            "compute_environment_state IN "
            "('STAGED', 'ACTIVE', 'RECYCLED', 'RESTORE_PENDING', 'SUSPENDED', 'FAILED')",
            name="ck_managed_user_compute_environment_state",
        ),
        CheckConstraint(
            "(host_access_state = 'DISABLED_BY_PLATFORM_POLICY' "
            "AND shell = '/usr/sbin/nologin') OR "
            "host_access_state != 'DISABLED_BY_PLATFORM_POLICY'",
            name="ck_managed_user_disabled_host_shell",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portal_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    unix_username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    uid: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    gid: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    shell: Mapped[str] = mapped_column(String(128), nullable=False)
    host_access_state: Mapped[str] = mapped_column(String(32), nullable=False)
    compute_environment_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ACTIVE"
    )
    host_access_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gpu_isolation_state: Mapped[str] = mapped_column(String(32), nullable=False)
    slurm_account: Mapped[str | None] = mapped_column(String(64))
    slurm_qos: Mapped[str | None] = mapped_column(String(64))
    project_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    quota_bytes: Mapped[int | None] = mapped_column(BigInteger)
    lease_renewal_approval_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    container_name: Mapped[str | None] = mapped_column(String(128), unique=True)
    container_port: Mapped[int | None] = mapped_column(Integer, unique=True)
    onboarding_state: Mapped[OnboardingState] = mapped_column(
        Enum(OnboardingState, native_enum=False, length=40), default=OnboardingState.DRAFT
    )
    ssh_key_state: Mapped[str] = mapped_column(
        String(40), nullable=False, default="NOT_REQUIRED_FOR_STAGE"
    )
    ssh_key_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    staged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    compute_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PortalComputeResourceRequest(Base):
    """A human-submitted request for a future managed compute identity.

    The owner is the Portal account because a managed Unix identity deliberately
    does not exist yet.  ``active_slot`` is a nullable singleton used to enforce
    one in-flight first-provisioning request per Portal account on every
    supported database.
    """

    __tablename__ = "portal_compute_resource_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
            "'REJECTED', 'CANCELLED', 'RETRY_AUTHORIZED', 'PROVISION_PLAN_READY', "
            "'PROVISIONING', "
            "'STAGED', 'KEY_ENROLLMENT_PENDING', 'ACTIVE', 'FAILED')",
            name="ck_compute_resource_request_state",
        ),
        CheckConstraint(
            "requested_gpu_max BETWEEN 0 AND 1",
            name="ck_compute_resource_request_gpu_max",
        ),
        CheckConstraint(
            "requested_storage_bytes = 322122547200",
            name="ck_compute_resource_request_standard_storage",
        ),
        CheckConstraint(
            "requested_container_profile IN ('STANDARD_8CPU_32GB', 'GPU_1_8CPU_32GB')",
            name="ck_compute_resource_request_standard_container",
        ),
        CheckConstraint(
            "requested_container_profile != 'GPU_1_8CPU_32GB' OR requested_gpu_max = 1",
            name="ck_compute_resource_request_gpu_profile_entitlement",
        ),
        CheckConstraint(
            "requested_lease_seconds = 345600",
            name="ck_compute_resource_request_initial_lease",
        ),
        CheckConstraint(
            "portal_account_id = requested_by",
            name="ck_compute_resource_request_actor_is_owner",
        ),
        CheckConstraint(
            "(status IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
            "'RETRY_AUTHORIZED', 'PROVISION_PLAN_READY', 'PROVISIONING') "
            "AND active_slot = 1) OR "
            "(status NOT IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
            "'RETRY_AUTHORIZED', 'PROVISION_PLAN_READY', 'PROVISIONING') "
            "AND active_slot IS NULL)",
            name="ck_compute_resource_request_active_slot",
        ),
        UniqueConstraint(
            "portal_account_id",
            "active_slot",
            name="uq_compute_resource_request_active_account",
        ),
        Index("ix_compute_resource_request_account_state", "portal_account_id", "status"),
        Index("ix_compute_resource_request_state_submitted", "status", "submitted_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portal_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    managed_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portal_managed_users.id"), nullable=True, index=True
    )
    username: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    active_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_gpu_max: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_container_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_lease_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(1000), nullable=False)
    user_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_users.id"))
    review_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provision_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "portal_provision_plans.id",
            name="fk_compute_resource_request_provision_plan",
            use_alter=True,
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__: dict[str, Any] = {"version_id_col": version}  # noqa: RUF012


class PortalProvisionPlan(Base):
    """Immutable-attempt resource coordinates for one approved request."""

    __tablename__ = "portal_provision_plans"
    __table_args__ = (
        CheckConstraint(
            "state IN ('RESERVED', 'READY_FOR_PROVISION', 'PROVISIONING', 'STAGED', "
            "'CONSUMED', 'RELEASED', 'EXPIRED', 'FAILED')",
            name="ck_portal_provision_plan_state",
        ),
        CheckConstraint("gpu_max BETWEEN 0 AND 1", name="ck_portal_provision_plan_gpu_max"),
        CheckConstraint("storage_bytes = 322122547200", name="ck_provision_plan_storage"),
        CheckConstraint(
            "container_profile IN ('STANDARD_8CPU_32GB', 'GPU_1_8CPU_32GB')",
            name="ck_plan_profile",
        ),
        CheckConstraint("container_cpus = 8", name="ck_provision_plan_cpus"),
        CheckConstraint("container_memory_gb = 32", name="ck_provision_plan_memory"),
        CheckConstraint("container_pids_limit = 4096", name="ck_provision_plan_pids"),
        CheckConstraint(
            "(container_profile = 'STANDARD_8CPU_32GB' AND container_gpu = 0) OR "
            "(container_profile = 'GPU_1_8CPU_32GB' AND container_gpu = 1 AND gpu_max = 1)",
            name="ck_provision_plan_container_gpu",
        ),
        CheckConstraint("lease_seconds = 345600", name="ck_provision_plan_lease"),
        CheckConstraint("lease_state = 'NOT_STARTED'", name="ck_provision_plan_lease_state"),
        CheckConstraint("host_ssh_enabled = false", name="ck_provision_plan_host_ssh"),
        CheckConstraint("shell = '/usr/sbin/nologin'", name="ck_provision_plan_shell"),
        CheckConstraint("password_state = 'LOCKED'", name="ck_provision_plan_password"),
        CheckConstraint(
            "(state IN ('RESERVED', 'READY_FOR_PROVISION') AND execution_enabled = false) OR "
            "(state IN ('PROVISIONING', 'STAGED') AND execution_enabled = true) OR "
            "state IN ('CONSUMED', 'RELEASED', 'EXPIRED', 'FAILED')",
            name="ck_provision_plan_execution_gate",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_provision_plan_attempt_positive"),
        CheckConstraint(
            "attempt_reason IN ('INITIAL', 'RESERVATION_EXPIRED', 'STAGE_RETRY')",
            name="ck_provision_plan_attempt_reason",
        ),
        CheckConstraint(
            "(attempt_number = 1 AND attempt_reason = 'INITIAL' "
            "AND previous_plan_id IS NULL AND failed_stage_operation_id IS NULL "
            "AND retry_authorization_operation_id IS NULL) OR "
            "(attempt_number > 1 AND previous_plan_id IS NOT NULL "
            "AND ((attempt_reason = 'RESERVATION_EXPIRED' "
            "AND failed_stage_operation_id IS NULL "
            "AND retry_authorization_operation_id IS NULL) OR "
            "(attempt_reason = 'STAGE_RETRY' "
            "AND failed_stage_operation_id IS NOT NULL "
            "AND retry_authorization_operation_id IS NOT NULL)))",
            name="ck_provision_plan_attempt_lineage",
        ),
        CheckConstraint(
            "previous_plan_id IS NULL OR previous_plan_id <> id", name="ck_plan_not_self"
        ),
        UniqueConstraint("request_id", "attempt_number", name="uq_provision_plan_attempt"),
        UniqueConstraint("previous_plan_id", name="uq_provision_plan_previous"),
        UniqueConstraint(
            "failed_stage_operation_id", name="uq_provision_plan_failed_stage_operation"
        ),
        UniqueConstraint(
            "retry_authorization_operation_id",
            name="uq_provision_plan_retry_authorization_operation",
        ),
        Index("ix_portal_provision_plan_account_state", "portal_account_id", "state"),
        Index("ix_portal_provision_plan_reservation_expiry", "state", "reservation_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_compute_resource_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt_reason: Mapped[str] = mapped_column(String(32), nullable=False, default="INITIAL")
    previous_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "portal_provision_plans.id",
            name="fk_provision_plan_previous",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    failed_stage_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "portal_operations.id",
            name="fk_provision_plan_failed_stage_operation",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    retry_authorization_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "portal_operations.id",
            name="fk_provision_plan_retry_authorization_operation",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    portal_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    uid: Mapped[int] = mapped_column(Integer, nullable=False)
    gid: Mapped[int] = mapped_column(Integer, nullable=False)
    project_id: Mapped[int] = mapped_column(Integer, nullable=False)
    container_name: Mapped[str] = mapped_column(String(128), nullable=False)
    container_ssh_port: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    container_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    container_cpus: Mapped[int] = mapped_column(Integer, nullable=False)
    container_memory_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    container_pids_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    container_gpu: Mapped[int] = mapped_column(Integer, nullable=False)
    slurm_account: Mapped[str] = mapped_column(String(64), nullable=False)
    slurm_qos: Mapped[str] = mapped_column(String(64), nullable=False)
    gpu_max: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_state: Mapped[str] = mapped_column(String(32), nullable=False)
    host_ssh_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    shell: Mapped[str] = mapped_column(String(128), nullable=False)
    password_state: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reservation_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    allocator_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    dry_run_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    dry_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__: dict[str, Any] = {"version_id_col": version}  # noqa: RUF012


class PortalResourceReservation(Base):
    """A DB-backed allocator reservation; it never represents host creation."""

    __tablename__ = "portal_resource_reservations"
    __table_args__ = (
        CheckConstraint(
            "resource_type IN ('UID', 'GID', 'PROJECT_ID', 'SSH_PORT', 'CONTAINER_NAME')",
            name="ck_portal_resource_reservation_type",
        ),
        CheckConstraint(
            "state IN ('RESERVED', 'CONSUMED', 'RELEASED', 'EXPIRED', 'FAILED_HOLD')",
            name="ck_portal_resource_reservation_state",
        ),
        CheckConstraint(
            "(state = 'RESERVED' AND active_key IS NOT NULL "
            "AND consumed_at IS NULL AND released_at IS NULL) OR "
            "(state = 'CONSUMED' AND active_key IS NULL "
            "AND consumed_at IS NOT NULL AND released_at IS NULL) OR "
            "(state IN ('RELEASED', 'EXPIRED') AND active_key IS NULL "
            "AND consumed_at IS NULL AND released_at IS NOT NULL) OR "
            "(state = 'FAILED_HOLD' AND active_key IS NOT NULL "
            "AND consumed_at IS NULL AND released_at IS NULL)",
            name="ck_portal_resource_reservation_lifecycle",
        ),
        UniqueConstraint("active_key", name="uq_portal_resource_reservation_active_key"),
        UniqueConstraint(
            "plan_id", "resource_type", name="uq_portal_resource_reservation_plan_type"
        ),
        Index("ix_resource_reservation_state_expiry", "state", "expires_at"),
        Index("ix_resource_reservation_owner_state", "portal_account_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_provision_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_compute_resource_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    portal_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resource_value: Mapped[str] = mapped_column(String(128), nullable=False)
    active_key: Mapped[str | None] = mapped_column(String(192), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalAllocatorLock(Base):
    """Singleton row used to serialize allocation across concurrent approvals."""

    __tablename__ = "portal_allocator_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PortalSshKey(Base):
    __tablename__ = "portal_ssh_keys"
    __table_args__ = (
        CheckConstraint("scope IN ('HOST', 'CONTAINER', 'BOTH')", name="ck_portal_ssh_key_scope"),
        CheckConstraint(
            "state IN ('VALIDATED', 'INSTALLED', 'REVOKED')",
            name="ck_portal_ssh_key_state",
        ),
        CheckConstraint(
            "generation_method IN ('BROWSER_GENERATED', 'IMPORTED')",
            name="ck_portal_ssh_key_generation_method",
        ),
        CheckConstraint(
            "(state = 'REVOKED' AND active = false AND revoked_at IS NOT NULL) OR "
            "(state IN ('VALIDATED', 'INSTALLED') AND active = true AND revoked_at IS NULL)",
            name="ck_portal_ssh_key_active_state",
        ),
        CheckConstraint(
            "state != 'INSTALLED' OR installed_at IS NOT NULL",
            name="ck_portal_ssh_key_installed_at",
        ),
        CheckConstraint(
            "state = 'REVOKED' OR (public_key IS NOT NULL AND validated_at IS NOT NULL "
            "AND staging_file_name IS NOT NULL AND content_sha256 IS NOT NULL "
            "AND enrollment_operation_id IS NOT NULL)",
            name="ck_portal_ssh_key_validated_binding",
        ),
        CheckConstraint(
            "owner_managed_user_id = managed_user_id",
            name="ck_portal_ssh_key_owner_matches_managed_user",
        ),
        CheckConstraint(
            "host_install_state IN ('NOT_INSTALLED', 'INSTALLED', 'REMOVED_BY_POLICY')",
            name="ck_portal_ssh_key_host_install_state",
        ),
        CheckConstraint(
            "container_install_state IN ('NOT_INSTALLED', 'INSTALLED', 'SUSPENDED_BY_RECYCLE')",
            name="ck_portal_ssh_key_container_install_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_type: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint_sha256: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    public_key: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="BOTH")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="VALIDATED")
    host_install_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NOT_INSTALLED"
    )
    container_install_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="NOT_INSTALLED"
    )
    generation_method: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_users.id"), nullable=False)
    enrollment_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portal_operations.id"), nullable=True
    )
    staging_file_name: Mapped[str | None] = mapped_column(String(64), unique=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalContainer(Base):
    __tablename__ = "portal_containers"
    __table_args__ = (
        CheckConstraint(
            "owner_managed_user_id = managed_user_id",
            name="ck_portal_container_owner_matches_managed_user",
        ),
        CheckConstraint(
            "(development_profile = 'STANDARD_8CPU_32GB' AND gpu_count = 0 "
            "AND gpu_allocation_job_id IS NULL AND gpu_allocation_uuid IS NULL) OR "
            "(development_profile = 'GPU_1_8CPU_32GB' AND gpu_count = 1)",
            name="ck_portal_container_development_profile",
        ),
        CheckConstraint(
            "(gpu_allocation_job_id IS NULL AND gpu_allocation_uuid IS NULL) OR "
            "(gpu_allocation_job_id IS NOT NULL AND gpu_allocation_uuid IS NOT NULL)",
            name="ck_portal_container_gpu_allocation_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id"), nullable=False
    )
    owner_managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    ssh_port: Mapped[int | None] = mapped_column(Integer, unique=True)
    desired_state: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_state: Mapped[str] = mapped_column(String(32), nullable=False)
    development_profile: Mapped[str] = mapped_column(
        String(64), nullable=False, default="STANDARD_8CPU_32GB"
    )
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gpu_allocation_job_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    gpu_allocation_uuid: Mapped[str | None] = mapped_column(String(64), unique=True)
    safe_spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_rebuilt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalComputeLease(Base):
    __tablename__ = "portal_compute_leases"
    __table_args__ = (
        UniqueConstraint("previous_lease_id"),
        CheckConstraint(
            "owner_managed_user_id = managed_user_id",
            name="ck_compute_lease_owner_matches_managed_user",
        ),
        CheckConstraint("gpu_count BETWEEN 0 AND 1", name="ck_compute_lease_gpu_count"),
        CheckConstraint("duration_seconds BETWEEN 1 AND 345600", name="ck_compute_lease_duration"),
        CheckConstraint("max_duration_seconds = 345600", name="ck_compute_lease_max_duration"),
        CheckConstraint("renewal_window_seconds = 86400", name="ck_compute_lease_renewal_window"),
        CheckConstraint(
            "state IN ('REQUESTED', 'APPROVED', 'ACTIVE', 'RENEWAL_WINDOW', "
            "'RENEWAL_PENDING', 'EXPIRED', 'RECYCLE_BIN', 'RESTORE_PENDING', "
            "'RESTORING', 'SUSPENDED', 'FAILED')",
            name="ck_compute_lease_state",
        ),
        CheckConstraint("expires_at > starts_at", name="ck_compute_lease_time_order"),
        CheckConstraint(
            "expires_at = starts_at + duration_seconds * INTERVAL '1 second'",
            name="ck_compute_lease_timestamp_duration",
        ).ddl_if(dialect="postgresql"),
        Index("ix_portal_compute_leases_expiry_scan", "state", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=345600)
    renewal_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_users.id"))
    renewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    previous_lease_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portal_compute_leases.id")
    )
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recycled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__: dict[str, Any] = {"version_id_col": version}  # noqa: RUF012


class PortalLeaseRenewalRequest(Base):
    __tablename__ = "portal_lease_renewal_requests"
    __table_args__ = (
        CheckConstraint(
            "state IN ('REQUESTED', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="ck_lease_renewal_request_state",
        ),
        CheckConstraint(
            "requested_duration_seconds BETWEEN 1 AND 345600",
            name="ck_lease_renewal_request_duration",
        ),
        UniqueConstraint("owner_managed_user_id", "idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lease_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_compute_leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requested_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_users.id"))
    resulting_lease_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portal_compute_leases.id")
    )
    decision_comment: Mapped[str | None] = mapped_column(String(500))


class PortalJob(Base):
    __tablename__ = "portal_jobs"
    __table_args__ = (
        CheckConstraint("gpu_count BETWEEN 0 AND 4", name="ck_portal_job_gpu_count"),
        CheckConstraint("requested_cpus BETWEEN 1 AND 32", name="ck_portal_job_cpus"),
        CheckConstraint("memory_mb BETWEEN 256 AND 486377", name="ck_portal_job_memory"),
        CheckConstraint(
            "time_limit_seconds BETWEEN 60 AND 345600", name="ck_portal_job_time_limit"
        ),
        CheckConstraint(
            "state IN ('APPROVAL_PENDING', 'REJECTED', 'SUBMITTING', 'PENDING', "
            "'CONFIGURING', 'RUNNING', 'SUSPENDED', 'STOPPED', 'COMPLETING', "
            "'REQUEUED', 'RESIZING', 'BOOT_FAIL', 'CANCELLED', 'COMPLETED', "
            "'DEADLINE', 'FAILED', 'NODE_FAIL', 'OUT_OF_MEMORY', 'PREEMPTED', "
            "'REVOKED', 'TIMEOUT', 'UNKNOWN')",
            name="ck_portal_job_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lease_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_compute_leases.id"), nullable=False, index=True
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_operations.id"), nullable=False, unique=True
    )
    slurm_job_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    state_reason: Mapped[str | None] = mapped_column(String(512))
    script_relative_path: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(512))
    workdir_relative_path: Mapped[str] = mapped_column(String(255), nullable=False)
    stdout_relative_path: Mapped[str] = mapped_column(String(255), nullable=False)
    stderr_relative_path: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_cpus: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    time_limit_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    image_ref: Mapped[str | None] = mapped_column(String(512))
    lease_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    elapsed_seconds: Mapped[int | None] = mapped_column(Integer)
    exit_code: Mapped[str | None] = mapped_column(String(32))

    gpu_approval: Mapped[PortalJobGpuApproval | None] = relationship(
        "PortalJobGpuApproval", uselist=False, lazy="selectin"
    )
    memory_approval: Mapped[PortalJobMemoryApproval | None] = relationship(
        "PortalJobMemoryApproval", uselist=False, lazy="selectin"
    )


class PortalJobGpuApproval(Base):
    __tablename__ = "portal_job_gpu_approvals"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="ck_job_gpu_approval_state",
        ),
        CheckConstraint(
            "requested_gpu_count BETWEEN 2 AND 4",
            name="ck_job_gpu_approval_requested_count",
        ),
        CheckConstraint(
            "approved_gpu_count IS NULL OR approved_gpu_count BETWEEN 1 AND requested_gpu_count",
            name="ck_job_gpu_approval_approved_count",
        ),
        CheckConstraint(
            "(state = 'APPROVED' AND approved_gpu_count IS NOT NULL "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL) OR "
            "(state IN ('REJECTED') AND approved_gpu_count IS NULL "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL) OR "
            "(state IN ('PENDING', 'CANCELLED') AND approved_gpu_count IS NULL)",
            name="ck_job_gpu_approval_decision",
        ),
        CheckConstraint("length(script_sha256) = 64", name="ck_job_gpu_approval_script_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portal_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_jobs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    requested_gpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_gpu_count: Mapped[int | None] = mapped_column(Integer)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_architecture: Mapped[str] = mapped_column(String(500), nullable=False)
    framework: Mapped[str] = mapped_column(String(100), nullable=False)
    framework_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parameter_count: Mapped[str] = mapped_column(String(100), nullable=False)
    workload_description: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_description: Mapped[str] = mapped_column(Text, nullable=False)
    parallel_strategy: Mapped[str] = mapped_column(Text, nullable=False)
    scaling_justification: Mapped[str] = mapped_column(Text, nullable=False)
    script_content: Mapped[str] = mapped_column(Text, nullable=False)
    script_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_users.id"))
    decision_comment: Mapped[str | None] = mapped_column(String(1000))
    decision_idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__: dict[str, Any] = {"version_id_col": version}  # noqa: RUF012


class PortalJobMemoryApproval(Base):
    __tablename__ = "portal_job_memory_approvals"
    __table_args__ = (
        CheckConstraint(
            "state IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="ck_job_memory_approval_state",
        ),
        CheckConstraint(
            "requested_memory_mb BETWEEN 32769 AND 486377",
            name="ck_job_memory_approval_requested_memory",
        ),
        CheckConstraint(
            "approved_memory_mb IS NULL OR approved_memory_mb BETWEEN 256 AND requested_memory_mb",
            name="ck_job_memory_approval_approved_memory",
        ),
        CheckConstraint(
            "(state = 'APPROVED' AND approved_memory_mb IS NOT NULL "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL) OR "
            "(state = 'REJECTED' AND approved_memory_mb IS NULL "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL) OR "
            "(state IN ('PENDING', 'CANCELLED') AND approved_memory_mb IS NULL)",
            name="ck_job_memory_approval_decision",
        ),
        CheckConstraint("length(script_sha256) = 64", name="ck_job_memory_approval_script_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    portal_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_jobs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    requested_memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_memory_mb: Mapped[int | None] = mapped_column(Integer)
    workload_description: Mapped[str] = mapped_column(Text, nullable=False)
    memory_breakdown: Mapped[str] = mapped_column(Text, nullable=False)
    memory_justification: Mapped[str] = mapped_column(Text, nullable=False)
    script_content: Mapped[str] = mapped_column(Text, nullable=False)
    script_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_users.id"))
    decision_comment: Mapped[str | None] = mapped_column(String(1000))
    decision_idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__: dict[str, Any] = {"version_id_col": version}  # noqa: RUF012


class PortalStorageResource(Base):
    __tablename__ = "portal_storage_resources"
    __table_args__ = (
        CheckConstraint("quota_bytes > 0", name="ck_portal_storage_quota_positive"),
        CheckConstraint(
            "state IN ('STAGED', 'ACTIVE', 'PRESERVED', 'RESTORING', 'FAILED')",
            name="ck_portal_storage_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    root_path: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortalResourceRecycleItem(Base):
    __tablename__ = "portal_resource_recycle_items"
    __table_args__ = (
        CheckConstraint(
            "state IN ('RECYCLE_BIN', 'RESTORE_PENDING', 'RESTORING', 'RESTORED', 'FAILED')",
            name="ck_resource_recycle_item_state",
        ),
        CheckConstraint("data_preserved = true", name="ck_recycle_item_data_preserved"),
        CheckConstraint("auto_permanent_delete = false", name="ck_recycle_item_no_auto_delete"),
        UniqueConstraint("lease_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lease_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_compute_leases.id"), nullable=False, index=True
    )
    container_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_containers.id"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resource_name: Mapped[str] = mapped_column(String(128), nullable=False)
    image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    retained_spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    connection_state: Mapped[str] = mapped_column(String(32), nullable=False)
    data_preserved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_permanent_delete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recycled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalResourceRestoreRequest(Base):
    __tablename__ = "portal_resource_restore_requests"
    __table_args__ = (
        CheckConstraint(
            "state IN ('REQUESTED', 'APPROVED', 'REJECTED', 'CANCELLED', 'RESTORING', "
            "'RESTORED', 'FAILED')",
            name="ck_resource_restore_request_state",
        ),
        CheckConstraint(
            "requested_duration_seconds BETWEEN 1 AND 345600",
            name="ck_resource_restore_request_duration",
        ),
        UniqueConstraint("owner_managed_user_id", "idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recycle_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_resource_recycle_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requested_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_users.id"))
    restored_lease_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("portal_compute_leases.id")
    )
    decision_comment: Mapped[str | None] = mapped_column(String(500))


class PortalImage(Base):
    __tablename__ = "portal_images"
    __table_args__ = (UniqueConstraint("registry", "repository", "digest"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    registry_name: Mapped[str] = mapped_column("registry", String(64), nullable=False)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    tag: Mapped[str | None] = mapped_column(String(128))
    digest: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    architecture: Mapped[str | None] = mapped_column(String(64))
    approval_state: Mapped[str] = mapped_column(String(32), nullable=False)
    imported_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_users.id"))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalRegistryStatus(Base):
    __tablename__ = "portal_registry_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    registry_name: Mapped[str] = mapped_column("registry", String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    safe_detail: Mapped[str] = mapped_column(String(255), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortalSetting(Base):
    __tablename__ = "portal_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
