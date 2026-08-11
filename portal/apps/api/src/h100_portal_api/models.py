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
            "('ACTIVE', 'RECYCLED', 'RESTORE_PENDING', 'SUSPENDED', 'FAILED')",
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
    gid: Mapped[int] = mapped_column(Integer, nullable=False)
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
        CheckConstraint("gpu_count BETWEEN 0 AND 1", name="ck_portal_job_gpu_count"),
        CheckConstraint("requested_cpus BETWEEN 1 AND 32", name="ck_portal_job_cpus"),
        CheckConstraint("memory_mb BETWEEN 256 AND 32768", name="ck_portal_job_memory"),
        CheckConstraint(
            "time_limit_seconds BETWEEN 60 AND 345600", name="ck_portal_job_time_limit"
        ),
        CheckConstraint(
            "state IN ('SUBMITTING', 'PENDING', 'RUNNING', 'COMPLETING', 'COMPLETED', "
            "'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'UNKNOWN')",
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
    script_relative_path: Mapped[str] = mapped_column(String(255), nullable=False)
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
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[str | None] = mapped_column(String(32))


class PortalStorageResource(Base):
    __tablename__ = "portal_storage_resources"
    __table_args__ = (
        CheckConstraint("quota_bytes > 0", name="ck_portal_storage_quota_positive"),
        CheckConstraint(
            "state IN ('ACTIVE', 'PRESERVED', 'RESTORING', 'FAILED')",
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
