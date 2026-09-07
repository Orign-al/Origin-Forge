"""add per-Job high-memory approval

Revision ID: f5a6b7c8d9e0
Revises: e3f4a5b6c7d8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5a6b7c8d9e0"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("portal_jobs") as batch:
        batch.drop_constraint("ck_portal_job_memory", type_="check")
        batch.create_check_constraint(
            "ck_portal_job_memory", "memory_mb BETWEEN 256 AND 486377"
        )

    op.create_table(
        "portal_job_memory_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portal_job_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("requested_memory_mb", sa.Integer(), nullable=False),
        sa.Column("approved_memory_mb", sa.Integer(), nullable=True),
        sa.Column("workload_description", sa.Text(), nullable=False),
        sa.Column("memory_breakdown", sa.Text(), nullable=False),
        sa.Column("memory_justification", sa.Text(), nullable=False),
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
            name="ck_job_memory_approval_state",
        ),
        sa.CheckConstraint(
            "requested_memory_mb BETWEEN 32769 AND 486377",
            name="ck_job_memory_approval_requested_memory",
        ),
        sa.CheckConstraint(
            "approved_memory_mb IS NULL OR approved_memory_mb BETWEEN 256 "
            "AND requested_memory_mb",
            name="ck_job_memory_approval_approved_memory",
        ),
        sa.CheckConstraint(
            "(state = 'APPROVED' AND approved_memory_mb IS NOT NULL "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL) OR "
            "(state = 'REJECTED' AND approved_memory_mb IS NULL "
            "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL) OR "
            "(state IN ('PENDING', 'CANCELLED') AND approved_memory_mb IS NULL)",
            name="ck_job_memory_approval_decision",
        ),
        sa.CheckConstraint(
            "length(script_sha256) = 64", name="ck_job_memory_approval_script_sha256"
        ),
        sa.ForeignKeyConstraint(["portal_job_id"], ["portal_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["portal_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("decision_idempotency_key"),
    )
    op.create_index(
        "ix_portal_job_memory_approvals_portal_job_id",
        "portal_job_memory_approvals",
        ["portal_job_id"],
        unique=True,
    )
    op.create_index(
        "ix_portal_job_memory_approvals_state",
        "portal_job_memory_approvals",
        ["state"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    unsafe = connection.execute(
        sa.text("SELECT count(*) FROM portal_jobs WHERE memory_mb > 32768")
    ).scalar_one()
    approvals = connection.execute(
        sa.text("SELECT count(*) FROM portal_job_memory_approvals")
    ).scalar_one()
    if unsafe or approvals:
        raise RuntimeError(
            "refusing to downgrade while high-memory Jobs or approval records exist"
        )

    op.drop_index(
        "ix_portal_job_memory_approvals_state", table_name="portal_job_memory_approvals"
    )
    op.drop_index(
        "ix_portal_job_memory_approvals_portal_job_id",
        table_name="portal_job_memory_approvals",
    )
    op.drop_table("portal_job_memory_approvals")
    with op.batch_alter_table("portal_jobs") as batch:
        batch.drop_constraint("ck_portal_job_memory", type_="check")
        batch.create_check_constraint(
            "ck_portal_job_memory", "memory_mb BETWEEN 256 AND 32768"
        )
