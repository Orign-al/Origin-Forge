"""add ordinary-user CLI tokens and complete Job projection

Revision ID: c1d2e3f4a5b6
Revises: b8c9d0e1f2a3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portal_cli_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="ck_cli_token_expiry"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["portal_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_portal_cli_tokens_user_id", "portal_cli_tokens", ["user_id"], unique=False
    )
    op.create_index(
        "ix_cli_token_user_active",
        "portal_cli_tokens",
        ["user_id", "revoked_at"],
        unique=False,
    )

    with op.batch_alter_table("portal_jobs") as batch:
        batch.add_column(sa.Column("state_reason", sa.String(length=512), nullable=True))
        batch.add_column(sa.Column("source_path", sa.String(length=512), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("elapsed_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("portal_jobs") as batch:
        batch.drop_column("elapsed_seconds")
        batch.drop_column("started_at")
        batch.drop_column("source_path")
        batch.drop_column("state_reason")

    op.drop_index("ix_cli_token_user_active", table_name="portal_cli_tokens")
    op.drop_index("ix_portal_cli_tokens_user_id", table_name="portal_cli_tokens")
    op.drop_table("portal_cli_tokens")
