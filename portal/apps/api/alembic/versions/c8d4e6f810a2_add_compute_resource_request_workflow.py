"""add compute request, provision plan, and allocator reservations

Revision ID: c8d4e6f810a2
Revises: b7c2d4e6f810
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8d4e6f810a2"
down_revision: str | None = "b7c2d4e6f810"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portal_compute_resource_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portal_account_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("managed_user_id", sa.Uuid(), nullable=True),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("active_slot", sa.Integer(), nullable=True),
        sa.Column("requested_gpu_max", sa.Integer(), nullable=False),
        sa.Column("requested_storage_bytes", sa.BigInteger(), nullable=False),
        sa.Column("requested_container_profile", sa.String(length=64), nullable=False),
        sa.Column("requested_lease_seconds", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=1000), nullable=False),
        sa.Column("user_note", sa.String(length=1000), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("review_note", sa.String(length=1000), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provision_plan_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
            "'REJECTED', 'CANCELLED', 'PROVISION_PLAN_READY', 'PROVISIONING', "
            "'STAGED', 'KEY_ENROLLMENT_PENDING', 'ACTIVE', 'FAILED')",
            name="ck_compute_resource_request_state",
        ),
        sa.CheckConstraint(
            "requested_gpu_max BETWEEN 0 AND 1",
            name="ck_compute_resource_request_gpu_max",
        ),
        sa.CheckConstraint(
            "requested_storage_bytes = 322122547200",
            name="ck_compute_resource_request_standard_storage",
        ),
        sa.CheckConstraint(
            "requested_container_profile = 'STANDARD_8CPU_32GB'",
            name="ck_compute_resource_request_standard_container",
        ),
        sa.CheckConstraint(
            "requested_lease_seconds = 345600",
            name="ck_compute_resource_request_initial_lease",
        ),
        sa.CheckConstraint(
            "portal_account_id = requested_by",
            name="ck_compute_resource_request_actor_is_owner",
        ),
        sa.CheckConstraint(
            "(status IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
            "'PROVISION_PLAN_READY') AND active_slot = 1) OR "
            "(status NOT IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
            "'PROVISION_PLAN_READY') AND active_slot IS NULL)",
            name="ck_compute_resource_request_active_slot",
        ),
        sa.ForeignKeyConstraint(["portal_account_id"], ["portal_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["portal_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["managed_user_id"], ["portal_managed_users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["portal_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "portal_account_id",
            "active_slot",
            name="uq_compute_resource_request_active_account",
        ),
    )
    for column in (
        "portal_account_id",
        "requested_by",
        "managed_user_id",
        "username",
        "status",
    ):
        op.create_index(
            f"ix_portal_compute_resource_requests_{column}",
            "portal_compute_resource_requests",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_compute_resource_request_account_state",
        "portal_compute_resource_requests",
        ["portal_account_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_compute_resource_request_state_submitted",
        "portal_compute_resource_requests",
        ["status", "submitted_at"],
        unique=False,
    )

    op.create_table(
        "portal_provision_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("portal_account_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("uid", sa.Integer(), nullable=False),
        sa.Column("gid", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("container_name", sa.String(length=128), nullable=False),
        sa.Column("container_ssh_port", sa.Integer(), nullable=False),
        sa.Column("storage_bytes", sa.BigInteger(), nullable=False),
        sa.Column("container_profile", sa.String(length=64), nullable=False),
        sa.Column("container_cpus", sa.Integer(), nullable=False),
        sa.Column("container_memory_gb", sa.Integer(), nullable=False),
        sa.Column("container_pids_limit", sa.Integer(), nullable=False),
        sa.Column("container_gpu", sa.Integer(), nullable=False),
        sa.Column("slurm_account", sa.String(length=64), nullable=False),
        sa.Column("slurm_qos", sa.String(length=64), nullable=False),
        sa.Column("gpu_max", sa.Integer(), nullable=False),
        sa.Column("lease_seconds", sa.Integer(), nullable=False),
        sa.Column("lease_state", sa.String(length=32), nullable=False),
        sa.Column("host_ssh_enabled", sa.Boolean(), nullable=False),
        sa.Column("shell", sa.String(length=128), nullable=False),
        sa.Column("password_state", sa.String(length=32), nullable=False),
        sa.Column("execution_enabled", sa.Boolean(), nullable=False),
        sa.Column("reservation_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("allocator_result", sa.JSON(), nullable=False),
        sa.Column("dry_run_result", sa.JSON(), nullable=True),
        sa.Column("dry_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "state IN ('RESERVED', 'READY_FOR_PROVISION', 'CONSUMED', 'RELEASED', "
            "'EXPIRED', 'FAILED')",
            name="ck_portal_provision_plan_state",
        ),
        sa.CheckConstraint("gpu_max BETWEEN 0 AND 1", name="ck_portal_provision_plan_gpu_max"),
        sa.CheckConstraint("storage_bytes = 322122547200", name="ck_provision_plan_storage"),
        sa.CheckConstraint("container_profile = 'STANDARD_8CPU_32GB'", name="ck_plan_profile"),
        sa.CheckConstraint("container_cpus = 8", name="ck_provision_plan_cpus"),
        sa.CheckConstraint("container_memory_gb = 32", name="ck_provision_plan_memory"),
        sa.CheckConstraint("container_pids_limit = 4096", name="ck_provision_plan_pids"),
        sa.CheckConstraint("container_gpu = 0", name="ck_provision_plan_container_gpu"),
        sa.CheckConstraint("lease_seconds = 345600", name="ck_provision_plan_lease"),
        sa.CheckConstraint("lease_state = 'NOT_STARTED'", name="ck_provision_plan_lease_state"),
        sa.CheckConstraint("host_ssh_enabled = false", name="ck_provision_plan_host_ssh"),
        sa.CheckConstraint("shell = '/usr/sbin/nologin'", name="ck_provision_plan_shell"),
        sa.CheckConstraint("password_state = 'LOCKED'", name="ck_provision_plan_password"),
        sa.CheckConstraint("execution_enabled = false", name="ck_provision_plan_execution_gate"),
        sa.ForeignKeyConstraint(
            ["request_id"], ["portal_compute_resource_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["portal_account_id"], ["portal_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["portal_users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_portal_provision_plans_request_id",
        "portal_provision_plans",
        ["request_id"],
        unique=True,
    )
    for column in ("portal_account_id", "state", "reservation_expires_at"):
        op.create_index(
            f"ix_portal_provision_plans_{column}",
            "portal_provision_plans",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_portal_provision_plan_account_state",
        "portal_provision_plans",
        ["portal_account_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_portal_provision_plan_reservation_expiry",
        "portal_provision_plans",
        ["state", "reservation_expires_at"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_compute_resource_request_provision_plan",
        "portal_compute_resource_requests",
        "portal_provision_plans",
        ["provision_plan_id"],
        ["id"],
    )

    op.create_table(
        "portal_resource_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("portal_account_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_value", sa.String(length=128), nullable=False),
        sa.Column("active_key", sa.String(length=192), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "resource_type IN ('UID', 'GID', 'PROJECT_ID', 'SSH_PORT', 'CONTAINER_NAME')",
            name="ck_portal_resource_reservation_type",
        ),
        sa.CheckConstraint(
            "state IN ('RESERVED', 'CONSUMED', 'RELEASED', 'EXPIRED')",
            name="ck_portal_resource_reservation_state",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["portal_provision_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["request_id"], ["portal_compute_resource_requests.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["portal_account_id"], ["portal_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_key", name="uq_portal_resource_reservation_active_key"),
        sa.UniqueConstraint(
            "plan_id", "resource_type", name="uq_portal_resource_reservation_plan_type"
        ),
    )
    for column in ("plan_id", "request_id", "portal_account_id", "resource_type", "state"):
        op.create_index(
            f"ix_portal_resource_reservations_{column}",
            "portal_resource_reservations",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_resource_reservation_state_expiry",
        "portal_resource_reservations",
        ["state", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_resource_reservation_owner_state",
        "portal_resource_reservations",
        ["portal_account_id", "state"],
        unique=False,
    )

    op.create_table(
        "portal_allocator_locks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO portal_allocator_locks (id, version, updated_at) "
            "VALUES (1, 1, CURRENT_TIMESTAMP)"
        )
    )


def downgrade() -> None:
    op.drop_table("portal_allocator_locks")
    op.drop_index("ix_resource_reservation_owner_state", table_name="portal_resource_reservations")
    op.drop_index("ix_resource_reservation_state_expiry", table_name="portal_resource_reservations")
    for column in ("state", "resource_type", "portal_account_id", "request_id", "plan_id"):
        op.drop_index(
            f"ix_portal_resource_reservations_{column}",
            table_name="portal_resource_reservations",
        )
    op.drop_table("portal_resource_reservations")

    op.drop_constraint(
        "fk_compute_resource_request_provision_plan",
        "portal_compute_resource_requests",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_portal_provision_plan_reservation_expiry", table_name="portal_provision_plans"
    )
    op.drop_index("ix_portal_provision_plan_account_state", table_name="portal_provision_plans")
    for column in ("reservation_expires_at", "state", "portal_account_id", "request_id"):
        op.drop_index(f"ix_portal_provision_plans_{column}", table_name="portal_provision_plans")
    op.drop_table("portal_provision_plans")

    op.drop_index(
        "ix_compute_resource_request_state_submitted",
        table_name="portal_compute_resource_requests",
    )
    op.drop_index(
        "ix_compute_resource_request_account_state",
        table_name="portal_compute_resource_requests",
    )
    for column in ("status", "username", "managed_user_id", "requested_by", "portal_account_id"):
        op.drop_index(
            f"ix_portal_compute_resource_requests_{column}",
            table_name="portal_compute_resource_requests",
        )
    op.drop_table("portal_compute_resource_requests")
