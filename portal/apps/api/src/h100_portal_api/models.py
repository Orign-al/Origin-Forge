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

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortalSession(Base):
    __tablename__ = "portal_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_users.id", ondelete="CASCADE"), nullable=False, index=True
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
            "(ssh_key_count > 0 AND shell != '/usr/sbin/nologin' "
            "AND compute_activated_at IS NOT NULL)",
            name="ck_managed_user_active_login_contract",
        ),
        CheckConstraint(
            "onboarding_state NOT IN ('DRAFT', 'STAGED') OR ssh_key_state != 'SSH_READY'",
            name="ck_managed_user_no_early_ssh_ready",
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
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    managed_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portal_managed_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_type: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint_sha256: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    public_key: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="BOTH")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="VALIDATED")
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

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    managed_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_managed_users.id"))
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    image_digest: Mapped[str] = mapped_column(String(255), nullable=False)
    ssh_port: Mapped[int | None] = mapped_column(Integer, unique=True)
    desired_state: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_state: Mapped[str] = mapped_column(String(32), nullable=False)
    safe_spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_rebuilt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
