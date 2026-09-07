"""add per-Job multi-GPU approval workflow

Revision ID: e3f4a5b6c7d8
Revises: e4f5a6b7c8d9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATES = (
    "'APPROVAL_PENDING', 'REJECTED', 'SUBMITTING', 'PENDING', 'CONFIGURING', "
    "'RUNNING', 'SUSPENDED', 'STOPPED', 'COMPLETING', 'REQUEUED', 'RESIZING', "
    "'BOOT_FAIL', 'CANCELLED', 'COMPLETED', 'DEADLINE', 'FAILED', 'NODE_FAIL', "
    "'OUT_OF_MEMORY', 'PREEMPTED', 'REVOKED', 'TIMEOUT', 'UNKNOWN'"
)

LEGACY_JOB_STATES = (
    "'SUBMITTING', 'PENDING', 'RUNNING', 'COMPLETING', 'COMPLETED', "
    "'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'UNKNOWN'"
)
LEGACY_JOB_STATE_VALUES = (
    "SUBMITTING",
    "PENDING",
    "RUNNING",
    "COMPLETING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "UNKNOWN",
)


def upgrade() -> None:
    with op.batch_alter_table("portal_jobs") as batch:
        batch.drop_constraint("ck_portal_job_gpu_count", type_="check")
        batch.drop_constraint("ck_portal_job_state", type_="check")
        batch.create_check_constraint("ck_portal_job_gpu_count", "gpu_count BETWEEN 0 AND 4")
        batch.create_check_constraint("ck_portal_job_state", f"state IN ({JOB_STATES})")

    op.create_table(
        "portal_job_gpu_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portal_job_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("requested_gpu_count", sa.Integer(), nullable=False),
        sa.Column("approved_gpu_count", sa.Integer(), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("model_architecture", sa.String(length=500), nullable=False),
        sa.Column("framework", sa.String(length=100), nullable=False),
        sa.Column("framework_version", sa.String(length=100), nullable=False),
        sa.Column("parameter_count", sa.String(length=100), nullable=False),
        sa.Column("workload_description", sa.Text(), nullable=False),
        sa.Column("dataset_description", sa.Text(), nullable=False),
        sa.Column("parallel_strategy", sa.Text(), nullable=False),
        sa.Column("scaling_justification", sa.Text(), nullable=False),
        sa.Column("script_content", sa.Text(), nullable=False),
        sa.Column("script_sha256", sa.String(length=64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("decision_comment", sa.String(length=1000), nullable=True),
        sa.Column("decision_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="ck_job_gpu_approval_state",
        ),
        sa.CheckConstraint(
            "requested_gpu_count BETWEEN 2 AND 4",
            name="ck_job_gpu_approval_requested_count",
        ),
        sa.CheckConstraint(
            "approved_gpu_count IS NULL OR approved_gpu_count BETWEEN 1 AND requested_gpu_count",
            name="ck_job_gpu_approval_approved_count",
        ),
        sa.CheckConstraint(
            "(state = 'APPROVED' AND approved_gpu_count IS NOT NULL "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL) OR "
            "(state = 'REJECTED' AND approved_gpu_count IS NULL "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL) OR "
            "(state IN ('PENDING', 'CANCELLED') AND approved_gpu_count IS NULL)",
            name="ck_job_gpu_approval_decision",
        ),
        sa.CheckConstraint("length(script_sha256) = 64", name="ck_job_gpu_approval_script_sha256"),
        sa.ForeignKeyConstraint(["portal_job_id"], ["portal_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["portal_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("decision_idempotency_key"),
    )
    op.create_index(
        "ix_portal_job_gpu_approvals_portal_job_id",
        "portal_job_gpu_approvals",
        ["portal_job_id"],
        unique=True,
    )
    op.create_index(
        "ix_portal_job_gpu_approvals_state",
        "portal_job_gpu_approvals",
        ["state"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    unsafe_query = sa.text(
        "SELECT count(*) FROM portal_jobs WHERE gpu_count > 1 OR state NOT IN :legacy_states"
    ).bindparams(sa.bindparam("legacy_states", expanding=True))
    unsafe = connection.execute(
        unsafe_query, {"legacy_states": LEGACY_JOB_STATE_VALUES}
    ).scalar_one()
    approvals = connection.execute(
        sa.text("SELECT count(*) FROM portal_job_gpu_approvals")
    ).scalar_one()
    if unsafe or approvals:
        raise RuntimeError(
            "refusing to downgrade while multi-GPU approval records exist; "
            "preserve or resolve them before rollback"
        )

    op.drop_index("ix_portal_job_gpu_approvals_state", table_name="portal_job_gpu_approvals")
    op.drop_index(
        "ix_portal_job_gpu_approvals_portal_job_id",
        table_name="portal_job_gpu_approvals",
    )
    op.drop_table("portal_job_gpu_approvals")

    with op.batch_alter_table("portal_jobs") as batch:
        batch.drop_constraint("ck_portal_job_gpu_count", type_="check")
        batch.drop_constraint("ck_portal_job_state", type_="check")
        batch.create_check_constraint("ck_portal_job_gpu_count", "gpu_count BETWEEN 0 AND 1")
        batch.create_check_constraint("ck_portal_job_state", f"state IN ({LEGACY_JOB_STATES})")
