"""add Portal user invitations and password action lifecycle

Revision ID: b7c2d4e6f810
Revises: a4f0c1d2e3b4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c2d4e6f810"
down_revision: str | None = "a4f0c1d2e3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("portal_users", sa.Column("note", sa.String(length=500), nullable=True))

    op.add_column(
        "portal_password_setup_tokens",
        sa.Column(
            "purpose",
            sa.String(length=32),
            server_default="INITIAL_PASSWORD_SETUP",
            nullable=False,
        ),
    )
    op.add_column(
        "portal_password_setup_tokens",
        sa.Column("state", sa.String(length=16), server_default="ACTIVE", nullable=False),
    )
    op.add_column("portal_password_setup_tokens", sa.Column("created_by", sa.Uuid(), nullable=True))
    op.add_column(
        "portal_password_setup_tokens",
        sa.Column("request_ip_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "portal_password_setup_tokens",
        sa.Column("challenge_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "portal_password_setup_tokens",
        sa.Column("challenge_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "portal_password_setup_tokens",
        sa.Column("exchanged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE portal_password_setup_tokens
           SET state = CASE
               WHEN used_at IS NOT NULL THEN 'USED'
               WHEN revoked_at IS NOT NULL THEN 'REVOKED'
               WHEN expires_at <= CURRENT_TIMESTAMP THEN 'EXPIRED'
               ELSE 'ACTIVE'
           END
        """
    )
    op.alter_column("portal_password_setup_tokens", "purpose", server_default=None)
    op.alter_column("portal_password_setup_tokens", "state", server_default=None)
    op.create_foreign_key(
        "fk_password_action_created_by",
        "portal_password_setup_tokens",
        "portal_users",
        ["created_by"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_password_action_purpose",
        "portal_password_setup_tokens",
        "purpose IN ('INITIAL_PASSWORD_SETUP', 'PASSWORD_RESET')",
    )
    op.create_check_constraint(
        "ck_password_action_state",
        "portal_password_setup_tokens",
        "state IN ('ACTIVE', 'USED', 'REVOKED', 'EXPIRED')",
    )
    op.create_unique_constraint(
        "uq_password_action_challenge_hash",
        "portal_password_setup_tokens",
        ["challenge_hash"],
    )
    op.create_index(
        "ix_portal_password_setup_tokens_created_by",
        "portal_password_setup_tokens",
        ["created_by"],
        unique=False,
    )
    op.create_index(
        "ix_password_action_user_purpose_state",
        "portal_password_setup_tokens",
        ["user_id", "purpose", "state"],
        unique=False,
    )
    op.create_index(
        "ix_password_action_state_expires",
        "portal_password_setup_tokens",
        ["state", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_password_action_state_expires", table_name="portal_password_setup_tokens")
    op.drop_index(
        "ix_password_action_user_purpose_state", table_name="portal_password_setup_tokens"
    )
    op.drop_index(
        "ix_portal_password_setup_tokens_created_by",
        table_name="portal_password_setup_tokens",
    )
    op.drop_constraint(
        "uq_password_action_challenge_hash",
        "portal_password_setup_tokens",
        type_="unique",
    )
    op.drop_constraint("ck_password_action_state", "portal_password_setup_tokens", type_="check")
    op.drop_constraint("ck_password_action_purpose", "portal_password_setup_tokens", type_="check")
    op.drop_constraint(
        "fk_password_action_created_by", "portal_password_setup_tokens", type_="foreignkey"
    )
    for column in (
        "exchanged_at",
        "challenge_expires_at",
        "challenge_hash",
        "request_ip_digest",
        "created_by",
        "state",
        "purpose",
    ):
        op.drop_column("portal_password_setup_tokens", column)
    op.drop_column("portal_users", "note")
