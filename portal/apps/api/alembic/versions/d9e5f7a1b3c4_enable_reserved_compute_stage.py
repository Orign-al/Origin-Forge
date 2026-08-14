"""enable reserved compute provisioning stage

Revision ID: d9e5f7a1b3c4
Revises: c8d4e6f810a2
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d9e5f7a1b3c4"
down_revision: str | None = "c8d4e6f810a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    duplicate_gid = op.get_bind().exec_driver_sql(
        "SELECT gid FROM portal_managed_users GROUP BY gid HAVING count(*) > 1 LIMIT 1"
    ).scalar_one_or_none()
    if duplicate_gid is not None:
        raise RuntimeError("cannot enforce managed-user GID uniqueness while duplicates exist")
    op.create_unique_constraint(
        "uq_portal_managed_users_gid", "portal_managed_users", ["gid"]
    )

    op.drop_constraint(
        "ck_compute_resource_request_active_slot",
        "portal_compute_resource_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_compute_resource_request_active_slot",
        "portal_compute_resource_requests",
        "(status IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
        "'PROVISION_PLAN_READY', 'PROVISIONING') AND active_slot = 1) OR "
        "(status NOT IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
        "'PROVISION_PLAN_READY', 'PROVISIONING') AND active_slot IS NULL)",
    )

    op.drop_constraint(
        "ck_managed_user_compute_environment_state",
        "portal_managed_users",
        type_="check",
    )
    op.create_check_constraint(
        "ck_managed_user_compute_environment_state",
        "portal_managed_users",
        "compute_environment_state IN "
        "('STAGED', 'ACTIVE', 'RECYCLED', 'RESTORE_PENDING', 'SUSPENDED', 'FAILED')",
    )

    op.drop_constraint(
        "ck_portal_provision_plan_state", "portal_provision_plans", type_="check"
    )
    op.create_check_constraint(
        "ck_portal_provision_plan_state",
        "portal_provision_plans",
        "state IN ('RESERVED', 'READY_FOR_PROVISION', 'PROVISIONING', 'STAGED', "
        "'CONSUMED', 'RELEASED', 'EXPIRED', 'FAILED')",
    )
    op.drop_constraint(
        "ck_provision_plan_execution_gate", "portal_provision_plans", type_="check"
    )
    op.create_check_constraint(
        "ck_provision_plan_execution_gate",
        "portal_provision_plans",
        "(state IN ('RESERVED', 'READY_FOR_PROVISION') AND execution_enabled = false) OR "
        "(state IN ('PROVISIONING', 'STAGED') AND execution_enabled = true) OR "
        "state IN ('CONSUMED', 'RELEASED', 'EXPIRED', 'FAILED')",
    )

    op.drop_constraint(
        "ck_portal_resource_reservation_state",
        "portal_resource_reservations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_portal_resource_reservation_state",
        "portal_resource_reservations",
        "state IN ('RESERVED', 'CONSUMED', 'RELEASED', 'EXPIRED', 'FAILED_HOLD')",
    )
    op.create_check_constraint(
        "ck_portal_resource_reservation_lifecycle",
        "portal_resource_reservations",
        "(state = 'RESERVED' AND active_key IS NOT NULL "
        "AND consumed_at IS NULL AND released_at IS NULL) OR "
        "(state = 'CONSUMED' AND active_key IS NULL "
        "AND consumed_at IS NOT NULL AND released_at IS NULL) OR "
        "(state IN ('RELEASED', 'EXPIRED') AND active_key IS NULL "
        "AND consumed_at IS NULL AND released_at IS NOT NULL) OR "
        "(state = 'FAILED_HOLD' AND active_key IS NOT NULL "
        "AND consumed_at IS NULL AND released_at IS NULL)",
    )

    op.drop_constraint("ck_portal_storage_state", "portal_storage_resources", type_="check")
    op.create_check_constraint(
        "ck_portal_storage_state",
        "portal_storage_resources",
        "state IN ('STAGED', 'ACTIVE', 'PRESERVED', 'RESTORING', 'FAILED')",
    )


def downgrade() -> None:
    # Downgrade is deliberately blocked while Stage-created rows exist.  This
    # preserves lifecycle truth instead of coercing staged resources to ACTIVE.
    connection = op.get_bind()
    staged_count = sum(
        int(
            connection.exec_driver_sql(f"SELECT count(*) FROM {table} WHERE {column} = 'STAGED'").scalar_one()
        )
        for table, column in (
            ("portal_managed_users", "compute_environment_state"),
            ("portal_provision_plans", "state"),
            ("portal_storage_resources", "state"),
        )
    )
    staged_count += int(
        connection.exec_driver_sql(
            "SELECT count(*) FROM portal_resource_reservations "
            "WHERE state = 'FAILED_HOLD'"
        ).scalar_one()
    )
    staged_count += int(
        connection.exec_driver_sql(
            "SELECT count(*) FROM portal_compute_resource_requests "
            "WHERE status = 'PROVISIONING'"
        ).scalar_one()
    )
    staged_count += int(
        connection.exec_driver_sql(
            "SELECT count(*) FROM portal_provision_plans WHERE state = 'PROVISIONING'"
        ).scalar_one()
    )
    staged_count += int(
        connection.exec_driver_sql(
            "SELECT count(*) FROM portal_provision_plans WHERE execution_enabled = true"
        ).scalar_one()
    )
    if staged_count:
        raise RuntimeError("cannot downgrade while staged compute resources exist")

    op.drop_constraint(
        "uq_portal_managed_users_gid", "portal_managed_users", type_="unique"
    )

    op.drop_constraint(
        "ck_portal_resource_reservation_lifecycle",
        "portal_resource_reservations",
        type_="check",
    )
    op.drop_constraint(
        "ck_portal_resource_reservation_state",
        "portal_resource_reservations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_portal_resource_reservation_state",
        "portal_resource_reservations",
        "state IN ('RESERVED', 'CONSUMED', 'RELEASED', 'EXPIRED')",
    )

    op.drop_constraint("ck_portal_storage_state", "portal_storage_resources", type_="check")
    op.create_check_constraint(
        "ck_portal_storage_state",
        "portal_storage_resources",
        "state IN ('ACTIVE', 'PRESERVED', 'RESTORING', 'FAILED')",
    )

    op.drop_constraint(
        "ck_provision_plan_execution_gate", "portal_provision_plans", type_="check"
    )
    op.create_check_constraint(
        "ck_provision_plan_execution_gate",
        "portal_provision_plans",
        "execution_enabled = false",
    )
    op.drop_constraint(
        "ck_portal_provision_plan_state", "portal_provision_plans", type_="check"
    )
    op.create_check_constraint(
        "ck_portal_provision_plan_state",
        "portal_provision_plans",
        "state IN ('RESERVED', 'READY_FOR_PROVISION', 'CONSUMED', 'RELEASED', "
        "'EXPIRED', 'FAILED')",
    )

    op.drop_constraint(
        "ck_managed_user_compute_environment_state",
        "portal_managed_users",
        type_="check",
    )
    op.create_check_constraint(
        "ck_managed_user_compute_environment_state",
        "portal_managed_users",
        "compute_environment_state IN "
        "('ACTIVE', 'RECYCLED', 'RESTORE_PENDING', 'SUSPENDED', 'FAILED')",
    )

    op.drop_constraint(
        "ck_compute_resource_request_active_slot",
        "portal_compute_resource_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_compute_resource_request_active_slot",
        "portal_compute_resource_requests",
        "(status IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
        "'PROVISION_PLAN_READY') AND active_slot = 1) OR "
        "(status NOT IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
        "'PROVISION_PLAN_READY') AND active_slot IS NULL)",
    )
