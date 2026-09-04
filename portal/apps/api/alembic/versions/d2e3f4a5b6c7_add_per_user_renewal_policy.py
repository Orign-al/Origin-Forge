"""add per-user Lease renewal approval policy

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("portal_managed_users") as batch:
        batch.add_column(
            sa.Column(
                "lease_renewal_approval_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    with op.batch_alter_table("portal_lease_renewal_requests") as batch:
        batch.add_column(
            sa.Column(
                "approval_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("portal_lease_renewal_requests") as batch:
        batch.drop_column("approval_required")
    with op.batch_alter_table("portal_managed_users") as batch:
        batch.drop_column("lease_renewal_approval_required")
