"""link restore requests to the per-user renewal approval policy

Revision ID: e4f5a6b7c8d9
Revises: d2e3f4a5b6c7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("portal_resource_restore_requests") as batch:
        batch.add_column(
            sa.Column(
                "approval_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    op.execute(
        sa.text(
            "UPDATE portal_resource_restore_requests "
            "SET approval_required = false "
            "WHERE decision_comment = "
            "'Owner self-service restore; administrator approval not required'"
        )
    )


def downgrade() -> None:
    pending = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM portal_resource_restore_requests "
            "WHERE state = 'REQUESTED' AND approval_required = true"
        )
    ).scalar_one()
    if pending:
        raise RuntimeError(
            "cannot downgrade while policy-bound restore requests are pending approval"
        )
    with op.batch_alter_table("portal_resource_restore_requests") as batch:
        batch.drop_column("approval_required")
