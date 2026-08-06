"""store structured, redacted Worker dry-run plans

Revision ID: 3b7f1c2d9e40
Revises: dd26070f46bf
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "3b7f1c2d9e40"
down_revision: str | None = "dd26070f46bf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "portal_operations",
        sa.Column("dry_run_result", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("portal_operations", "dry_run_result")
