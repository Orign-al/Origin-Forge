"""add controlled delegated test sessions

Revision ID: f6a7b8c9d0e1
Revises: e2a4c6f810b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e2a4c6f810b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portal_delegated_test_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("effective_user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "actor_user_id <> effective_user_id",
            name="ck_delegated_test_distinct_users",
        ),
        sa.CheckConstraint(
            "ttl_seconds BETWEEN 600 AND 900",
            name="ck_delegated_test_ttl",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_delegated_test_expiry_order",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["portal_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["effective_user_id"], ["portal_users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_portal_delegated_test_sessions_actor_user_id",
        "portal_delegated_test_sessions",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_portal_delegated_test_sessions_effective_user_id",
        "portal_delegated_test_sessions",
        ["effective_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_delegated_test_expiry",
        "portal_delegated_test_sessions",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_delegated_test_expiry", table_name="portal_delegated_test_sessions")
    op.drop_index(
        "ix_portal_delegated_test_sessions_effective_user_id",
        table_name="portal_delegated_test_sessions",
    )
    op.drop_index(
        "ix_portal_delegated_test_sessions_actor_user_id",
        table_name="portal_delegated_test_sessions",
    )
    op.drop_table("portal_delegated_test_sessions")
