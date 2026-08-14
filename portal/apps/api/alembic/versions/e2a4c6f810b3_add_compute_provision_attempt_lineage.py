"""add immutable compute provision attempt lineage

Revision ID: e2a4c6f810b3
Revises: d9e5f7a1b3c4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2a4c6f810b3"
down_revision: str | None = "d9e5f7a1b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "portal_provision_plans",
        sa.Column("attempt_number", sa.Integer(), nullable=True),
    )
    op.add_column(
        "portal_provision_plans",
        sa.Column("attempt_reason", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "portal_provision_plans",
        sa.Column("previous_plan_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "portal_provision_plans",
        sa.Column("failed_stage_operation_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "portal_provision_plans",
        sa.Column("retry_authorization_operation_id", sa.Uuid(), nullable=True),
    )
    op.execute(
        "UPDATE portal_provision_plans "
        "SET attempt_number = 1, attempt_reason = 'INITIAL'"
    )
    op.alter_column("portal_provision_plans", "attempt_number", nullable=False)
    op.alter_column("portal_provision_plans", "attempt_reason", nullable=False)

    op.drop_index(
        "ix_portal_provision_plans_request_id",
        table_name="portal_provision_plans",
    )
    op.create_unique_constraint(
        "uq_provision_plan_attempt",
        "portal_provision_plans",
        ["request_id", "attempt_number"],
    )
    op.create_unique_constraint(
        "uq_provision_plan_previous",
        "portal_provision_plans",
        ["previous_plan_id"],
    )
    op.create_unique_constraint(
        "uq_provision_plan_failed_stage_operation",
        "portal_provision_plans",
        ["failed_stage_operation_id"],
    )
    op.create_unique_constraint(
        "uq_provision_plan_retry_authorization_operation",
        "portal_provision_plans",
        ["retry_authorization_operation_id"],
    )
    op.create_foreign_key(
        "fk_provision_plan_previous",
        "portal_provision_plans",
        "portal_provision_plans",
        ["previous_plan_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_provision_plan_failed_stage_operation",
        "portal_provision_plans",
        "portal_operations",
        ["failed_stage_operation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_provision_plan_retry_authorization_operation",
        "portal_provision_plans",
        "portal_operations",
        ["retry_authorization_operation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_provision_plan_attempt_positive",
        "portal_provision_plans",
        "attempt_number >= 1",
    )
    op.create_check_constraint(
        "ck_provision_plan_attempt_reason",
        "portal_provision_plans",
        "attempt_reason IN ('INITIAL', 'RESERVATION_EXPIRED', 'STAGE_RETRY')",
    )
    op.create_check_constraint(
        "ck_provision_plan_attempt_lineage",
        "portal_provision_plans",
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
    )
    op.create_check_constraint(
        "ck_plan_not_self",
        "portal_provision_plans",
        "previous_plan_id IS NULL OR previous_plan_id <> id",
    )

    op.drop_constraint(
        "ck_compute_resource_request_state",
        "portal_compute_resource_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_compute_resource_request_state",
        "portal_compute_resource_requests",
        "status IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
        "'REJECTED', 'CANCELLED', 'RETRY_AUTHORIZED', 'PROVISION_PLAN_READY', "
        "'PROVISIONING', 'STAGED', 'KEY_ENROLLMENT_PENDING', 'ACTIVE', 'FAILED')",
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
        "'RETRY_AUTHORIZED', 'PROVISION_PLAN_READY', 'PROVISIONING') "
        "AND active_slot = 1) OR "
        "(status NOT IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
        "'RETRY_AUTHORIZED', 'PROVISION_PLAN_READY', 'PROVISIONING') "
        "AND active_slot IS NULL)",
    )


def downgrade() -> None:
    connection = op.get_bind()
    retry_rows = int(
        connection.exec_driver_sql(
            "SELECT count(*) FROM portal_provision_plans "
            "WHERE attempt_number > 1 OR previous_plan_id IS NOT NULL "
            "OR failed_stage_operation_id IS NOT NULL "
            "OR retry_authorization_operation_id IS NOT NULL"
        ).scalar_one()
    )
    retry_rows += int(
        connection.exec_driver_sql(
            "SELECT count(*) FROM portal_compute_resource_requests "
            "WHERE status = 'RETRY_AUTHORIZED'"
        ).scalar_one()
    )
    if retry_rows:
        raise RuntimeError("cannot downgrade while compute retry lineage exists")

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
        "ck_compute_resource_request_state",
        "portal_compute_resource_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_compute_resource_request_state",
        "portal_compute_resource_requests",
        "status IN ('DRAFT', 'REQUESTED', 'UNDER_REVIEW', 'APPROVED', "
        "'REJECTED', 'CANCELLED', 'PROVISION_PLAN_READY', 'PROVISIONING', "
        "'STAGED', 'KEY_ENROLLMENT_PENDING', 'ACTIVE', 'FAILED')",
    )

    for name in (
        "ck_plan_not_self",
        "ck_provision_plan_attempt_lineage",
        "ck_provision_plan_attempt_reason",
        "ck_provision_plan_attempt_positive",
    ):
        op.drop_constraint(name, "portal_provision_plans", type_="check")
    for name in (
        "fk_provision_plan_retry_authorization_operation",
        "fk_provision_plan_failed_stage_operation",
        "fk_provision_plan_previous",
    ):
        op.drop_constraint(name, "portal_provision_plans", type_="foreignkey")
    for name in (
        "uq_provision_plan_retry_authorization_operation",
        "uq_provision_plan_failed_stage_operation",
        "uq_provision_plan_previous",
        "uq_provision_plan_attempt",
    ):
        op.drop_constraint(name, "portal_provision_plans", type_="unique")
    op.create_index(
        "ix_portal_provision_plans_request_id",
        "portal_provision_plans",
        ["request_id"],
        unique=True,
    )
    for column in (
        "retry_authorization_operation_id",
        "failed_stage_operation_id",
        "previous_plan_id",
        "attempt_reason",
        "attempt_number",
    ):
        op.drop_column("portal_provision_plans", column)
