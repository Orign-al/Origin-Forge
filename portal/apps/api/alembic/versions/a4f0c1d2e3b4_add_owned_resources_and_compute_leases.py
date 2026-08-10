"""add owned resources and renewable compute leases

Revision ID: a4f0c1d2e3b4
Revises: f4a91c3e7b20
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a4f0c1d2e3b4"
down_revision: str | None = "f4a91c3e7b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "portal_managed_users",
        sa.Column(
            "compute_environment_state",
            sa.String(length=32),
            server_default="ACTIVE",
            nullable=False,
        ),
    )
    op.add_column(
        "portal_managed_users",
        sa.Column("host_access_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "ck_managed_user_active_login_contract", "portal_managed_users", type_="check"
    )
    op.create_check_constraint(
        "ck_managed_user_active_login_contract",
        "portal_managed_users",
        "onboarding_state != 'ACTIVE' OR "
        "(ssh_key_count > 0 AND compute_activated_at IS NOT NULL "
        "AND host_access_state IN ('ENABLED', 'DISABLED_BY_PLATFORM_POLICY'))",
    )
    op.create_check_constraint(
        "ck_managed_user_compute_environment_state",
        "portal_managed_users",
        "compute_environment_state IN "
        "('ACTIVE', 'RECYCLED', 'RESTORE_PENDING', 'SUSPENDED', 'FAILED')",
    )
    op.create_check_constraint(
        "ck_managed_user_disabled_host_shell",
        "portal_managed_users",
        "(host_access_state = 'DISABLED_BY_PLATFORM_POLICY' "
        "AND shell = '/usr/sbin/nologin') OR "
        "host_access_state != 'DISABLED_BY_PLATFORM_POLICY'",
    )
    op.alter_column("portal_managed_users", "compute_environment_state", server_default=None)

    for table in ("portal_sessions", "portal_operations"):
        op.add_column(table, sa.Column("owner_managed_user_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_owner_managed_user",
            table,
            "portal_managed_users",
            ["owner_managed_user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index(
            f"ix_{table}_owner_managed_user_id", table, ["owner_managed_user_id"], unique=False
        )

    op.execute(
        """
        UPDATE portal_sessions AS session
           SET owner_managed_user_id = managed.id
          FROM portal_managed_users AS managed
         WHERE managed.portal_user_id = session.user_id
        """
    )
    op.execute(
        """
        UPDATE portal_operations AS operation
           SET owner_managed_user_id = managed.id
          FROM portal_managed_users AS managed
         WHERE managed.portal_user_id = operation.requested_by
            OR operation.target_id = managed.unix_username
            OR operation.validated_payload ->> 'managed_user_id' = managed.id::text
        """
    )

    op.add_column("portal_containers", sa.Column("owner_managed_user_id", sa.Uuid()))
    op.execute(
        "UPDATE portal_containers SET owner_managed_user_id = managed_user_id "
        "WHERE managed_user_id IS NOT NULL"
    )
    if op.get_bind().execute(
        sa.text("SELECT count(*) FROM portal_containers WHERE owner_managed_user_id IS NULL")
    ).scalar_one():
        raise RuntimeError("every managed container requires an owner before Portal-4A-R")
    op.alter_column("portal_containers", "owner_managed_user_id", nullable=False)
    op.alter_column("portal_containers", "managed_user_id", nullable=False)
    op.create_foreign_key(
        "fk_portal_containers_owner_managed_user",
        "portal_containers",
        "portal_managed_users",
        ["owner_managed_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_portal_containers_owner_managed_user_id",
        "portal_containers",
        ["owner_managed_user_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_portal_container_owner_matches_managed_user",
        "portal_containers",
        "owner_managed_user_id = managed_user_id",
    )

    op.add_column("portal_ssh_keys", sa.Column("owner_managed_user_id", sa.Uuid()))
    op.add_column(
        "portal_ssh_keys",
        sa.Column(
            "host_install_state",
            sa.String(length=32),
            server_default="NOT_INSTALLED",
            nullable=False,
        ),
    )
    op.add_column(
        "portal_ssh_keys",
        sa.Column(
            "container_install_state",
            sa.String(length=32),
            server_default="NOT_INSTALLED",
            nullable=False,
        ),
    )
    op.execute("UPDATE portal_ssh_keys SET owner_managed_user_id = managed_user_id")
    op.execute(
        """
        UPDATE portal_ssh_keys
           SET host_install_state = CASE
                   WHEN state = 'INSTALLED' AND scope IN ('HOST', 'BOTH') THEN 'INSTALLED'
                   ELSE 'NOT_INSTALLED'
               END,
               container_install_state = CASE
                   WHEN state = 'INSTALLED' AND scope IN ('CONTAINER', 'BOTH') THEN 'INSTALLED'
                   ELSE 'NOT_INSTALLED'
               END
        """
    )
    op.alter_column("portal_ssh_keys", "owner_managed_user_id", nullable=False)
    op.alter_column("portal_ssh_keys", "host_install_state", server_default=None)
    op.alter_column("portal_ssh_keys", "container_install_state", server_default=None)
    op.create_foreign_key(
        "fk_portal_ssh_keys_owner_managed_user",
        "portal_ssh_keys",
        "portal_managed_users",
        ["owner_managed_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_portal_ssh_keys_owner_managed_user_id",
        "portal_ssh_keys",
        ["owner_managed_user_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_portal_ssh_key_owner_matches_managed_user",
        "portal_ssh_keys",
        "owner_managed_user_id = managed_user_id",
    )
    op.create_check_constraint(
        "ck_portal_ssh_key_host_install_state",
        "portal_ssh_keys",
        "host_install_state IN ('NOT_INSTALLED', 'INSTALLED', 'REMOVED_BY_POLICY')",
    )
    op.create_check_constraint(
        "ck_portal_ssh_key_container_install_state",
        "portal_ssh_keys",
        "container_install_state IN "
        "('NOT_INSTALLED', 'INSTALLED', 'SUSPENDED_BY_RECYCLE')",
    )

    op.create_table(
        "portal_compute_leases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("managed_user_id", sa.Uuid(), nullable=False),
        sa.Column("owner_managed_user_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("max_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("renewal_window_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_lease_id", sa.Uuid(), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recycled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "owner_managed_user_id = managed_user_id",
            name="ck_compute_lease_owner_matches_managed_user",
        ),
        sa.CheckConstraint("gpu_count BETWEEN 0 AND 1", name="ck_compute_lease_gpu_count"),
        sa.CheckConstraint(
            "duration_seconds BETWEEN 1 AND 345600", name="ck_compute_lease_duration"
        ),
        sa.CheckConstraint(
            "max_duration_seconds = 345600", name="ck_compute_lease_max_duration"
        ),
        sa.CheckConstraint(
            "renewal_window_seconds = 86400", name="ck_compute_lease_renewal_window"
        ),
        sa.CheckConstraint(
            "state IN ('REQUESTED', 'APPROVED', 'ACTIVE', 'RENEWAL_WINDOW', "
            "'RENEWAL_PENDING', 'EXPIRED', 'RECYCLE_BIN', 'RESTORE_PENDING', "
            "'RESTORING', 'SUSPENDED', 'FAILED')",
            name="ck_compute_lease_state",
        ),
        sa.CheckConstraint("expires_at > starts_at", name="ck_compute_lease_time_order"),
        sa.CheckConstraint(
            "expires_at = starts_at + duration_seconds * INTERVAL '1 second'",
            name="ck_compute_lease_timestamp_duration",
        ),
        sa.ForeignKeyConstraint(["managed_user_id"], ["portal_managed_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["owner_managed_user_id"], ["portal_managed_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["approved_by"], ["portal_users.id"]),
        sa.ForeignKeyConstraint(["previous_lease_id"], ["portal_compute_leases.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("previous_lease_id"),
    )
    for column in ("managed_user_id", "owner_managed_user_id", "state", "expires_at"):
        op.create_index(
            f"ix_portal_compute_leases_{column}",
            "portal_compute_leases",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_portal_compute_leases_expiry_scan",
        "portal_compute_leases",
        ["state", "expires_at"],
        unique=False,
    )

    op.create_table(
        "portal_lease_renewal_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_managed_user_id", sa.Uuid(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("requested_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("resulting_lease_id", sa.Uuid(), nullable=True),
        sa.Column("decision_comment", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "state IN ('REQUESTED', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="ck_lease_renewal_request_state",
        ),
        sa.CheckConstraint(
            "requested_duration_seconds BETWEEN 1 AND 345600",
            name="ck_lease_renewal_request_duration",
        ),
        sa.ForeignKeyConstraint(
            ["owner_managed_user_id"], ["portal_managed_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["lease_id"], ["portal_compute_leases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["portal_users.id"]),
        sa.ForeignKeyConstraint(["resulting_lease_id"], ["portal_compute_leases.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_managed_user_id", "idempotency_key"),
    )
    for column in ("owner_managed_user_id", "lease_id", "state"):
        op.create_index(
            f"ix_portal_lease_renewal_requests_{column}",
            "portal_lease_renewal_requests",
            [column],
            unique=False,
        )

    op.create_table(
        "portal_storage_resources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_managed_user_id", sa.Uuid(), nullable=False),
        sa.Column("root_path", sa.String(length=255), nullable=False),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quota_bytes > 0", name="ck_portal_storage_quota_positive"),
        sa.CheckConstraint(
            "state IN ('ACTIVE', 'PRESERVED', 'RESTORING', 'FAILED')",
            name="ck_portal_storage_state",
        ),
        sa.ForeignKeyConstraint(
            ["owner_managed_user_id"], ["portal_managed_users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("root_path"),
    )
    op.create_index(
        "ix_portal_storage_resources_owner_managed_user_id",
        "portal_storage_resources",
        ["owner_managed_user_id"],
        unique=True,
    )

    op.create_table(
        "portal_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_managed_user_id", sa.Uuid(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("slurm_job_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("script_relative_path", sa.String(length=255), nullable=False),
        sa.Column("workdir_relative_path", sa.String(length=255), nullable=False),
        sa.Column("stdout_relative_path", sa.String(length=255), nullable=False),
        sa.Column("stderr_relative_path", sa.String(length=255), nullable=False),
        sa.Column("requested_cpus", sa.Integer(), nullable=False),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("time_limit_seconds", sa.Integer(), nullable=False),
        sa.Column("image_ref", sa.String(length=512), nullable=True),
        sa.Column("lease_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_code", sa.String(length=32), nullable=True),
        sa.CheckConstraint("gpu_count BETWEEN 0 AND 1", name="ck_portal_job_gpu_count"),
        sa.CheckConstraint("requested_cpus BETWEEN 1 AND 32", name="ck_portal_job_cpus"),
        sa.CheckConstraint("memory_mb BETWEEN 256 AND 32768", name="ck_portal_job_memory"),
        sa.CheckConstraint(
            "time_limit_seconds BETWEEN 60 AND 345600", name="ck_portal_job_time_limit"
        ),
        sa.CheckConstraint(
            "state IN ('SUBMITTING', 'PENDING', 'RUNNING', 'COMPLETING', 'COMPLETED', "
            "'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'UNKNOWN')",
            name="ck_portal_job_state",
        ),
        sa.ForeignKeyConstraint(
            ["owner_managed_user_id"], ["portal_managed_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["lease_id"], ["portal_compute_leases.id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["portal_operations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id"),
    )
    for column in ("owner_managed_user_id", "lease_id", "slurm_job_id", "state"):
        op.create_index(
            f"ix_portal_jobs_{column}",
            "portal_jobs",
            [column],
            unique=column == "slurm_job_id",
        )

    op.create_table(
        "portal_resource_recycle_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_managed_user_id", sa.Uuid(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("container_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("resource_name", sa.String(length=128), nullable=False),
        sa.Column("image_digest", sa.String(length=255), nullable=False),
        sa.Column("retained_spec", sa.JSON(), nullable=False),
        sa.Column("connection_state", sa.String(length=32), nullable=False),
        sa.Column("data_preserved", sa.Boolean(), nullable=False),
        sa.Column("auto_permanent_delete", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recycled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('RECYCLE_BIN', 'RESTORE_PENDING', 'RESTORING', 'RESTORED', 'FAILED')",
            name="ck_resource_recycle_item_state",
        ),
        sa.CheckConstraint("data_preserved = true", name="ck_recycle_item_data_preserved"),
        sa.CheckConstraint(
            "auto_permanent_delete = false", name="ck_recycle_item_no_auto_delete"
        ),
        sa.ForeignKeyConstraint(
            ["owner_managed_user_id"], ["portal_managed_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["lease_id"], ["portal_compute_leases.id"]),
        sa.ForeignKeyConstraint(["container_id"], ["portal_containers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lease_id"),
    )
    for column in ("owner_managed_user_id", "lease_id", "container_id", "state"):
        op.create_index(
            f"ix_portal_resource_recycle_items_{column}",
            "portal_resource_recycle_items",
            [column],
            unique=False,
        )

    op.create_table(
        "portal_resource_restore_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_managed_user_id", sa.Uuid(), nullable=False),
        sa.Column("recycle_item_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("requested_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("restored_lease_id", sa.Uuid(), nullable=True),
        sa.Column("decision_comment", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "state IN ('REQUESTED', 'APPROVED', 'REJECTED', 'CANCELLED', 'RESTORING', "
            "'RESTORED', 'FAILED')",
            name="ck_resource_restore_request_state",
        ),
        sa.CheckConstraint(
            "requested_duration_seconds BETWEEN 1 AND 345600",
            name="ck_resource_restore_request_duration",
        ),
        sa.ForeignKeyConstraint(
            ["owner_managed_user_id"], ["portal_managed_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["recycle_item_id"], ["portal_resource_recycle_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["decided_by"], ["portal_users.id"]),
        sa.ForeignKeyConstraint(["restored_lease_id"], ["portal_compute_leases.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_managed_user_id", "idempotency_key"),
    )
    for column in ("owner_managed_user_id", "recycle_item_id", "state"):
        op.create_index(
            f"ix_portal_resource_restore_requests_{column}",
            "portal_resource_restore_requests",
            [column],
            unique=False,
        )


def downgrade() -> None:
    active_nologin = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM portal_managed_users "
            "WHERE onboarding_state = 'ACTIVE' AND shell = '/usr/sbin/nologin'"
        )
    ).scalar_one()
    if active_nologin:
        raise RuntimeError(
            "Portal-4A-R host-access policy must be explicitly reversed before downgrade"
        )

    for table, indexes in (
        (
            "portal_resource_restore_requests",
            ("owner_managed_user_id", "recycle_item_id", "state"),
        ),
        (
            "portal_resource_recycle_items",
            ("owner_managed_user_id", "lease_id", "container_id", "state"),
        ),
        ("portal_jobs", ("owner_managed_user_id", "lease_id", "slurm_job_id", "state")),
        ("portal_storage_resources", ("owner_managed_user_id",)),
        (
            "portal_lease_renewal_requests",
            ("owner_managed_user_id", "lease_id", "state"),
        ),
        (
            "portal_compute_leases",
            ("managed_user_id", "owner_managed_user_id", "state", "expires_at"),
        ),
    ):
        if table == "portal_compute_leases":
            op.drop_index("ix_portal_compute_leases_expiry_scan", table_name=table)
        for column in indexes:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)

    for constraint in (
        "ck_portal_ssh_key_container_install_state",
        "ck_portal_ssh_key_host_install_state",
        "ck_portal_ssh_key_owner_matches_managed_user",
    ):
        op.drop_constraint(constraint, "portal_ssh_keys", type_="check")
    op.drop_index(
        "ix_portal_ssh_keys_owner_managed_user_id", table_name="portal_ssh_keys"
    )
    op.drop_constraint(
        "fk_portal_ssh_keys_owner_managed_user", "portal_ssh_keys", type_="foreignkey"
    )
    for column in ("container_install_state", "host_install_state", "owner_managed_user_id"):
        op.drop_column("portal_ssh_keys", column)

    op.drop_constraint(
        "ck_portal_container_owner_matches_managed_user",
        "portal_containers",
        type_="check",
    )
    op.drop_index(
        "ix_portal_containers_owner_managed_user_id", table_name="portal_containers"
    )
    op.drop_constraint(
        "fk_portal_containers_owner_managed_user",
        "portal_containers",
        type_="foreignkey",
    )
    op.drop_column("portal_containers", "owner_managed_user_id")
    op.alter_column("portal_containers", "managed_user_id", nullable=True)

    for table in ("portal_operations", "portal_sessions"):
        op.drop_index(f"ix_{table}_owner_managed_user_id", table_name=table)
        op.drop_constraint(f"fk_{table}_owner_managed_user", table, type_="foreignkey")
        op.drop_column(table, "owner_managed_user_id")

    op.drop_constraint(
        "ck_managed_user_compute_environment_state", "portal_managed_users", type_="check"
    )
    op.drop_constraint(
        "ck_managed_user_disabled_host_shell", "portal_managed_users", type_="check"
    )
    op.drop_constraint(
        "ck_managed_user_active_login_contract", "portal_managed_users", type_="check"
    )
    op.create_check_constraint(
        "ck_managed_user_active_login_contract",
        "portal_managed_users",
        "onboarding_state != 'ACTIVE' OR "
        "(ssh_key_count > 0 AND shell != '/usr/sbin/nologin' "
        "AND compute_activated_at IS NOT NULL)",
    )
    op.drop_column("portal_managed_users", "host_access_revoked_at")
    op.drop_column("portal_managed_users", "compute_environment_state")
