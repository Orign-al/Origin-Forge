"""separate staged and activated SSH key lifecycle

Revision ID: 8c2b6f1a4d70
Revises: 3b7f1c2d9e40
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "8c2b6f1a4d70"
down_revision: str | None = "3b7f1c2d9e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "portal_managed_users",
        sa.Column(
            "ssh_key_state",
            sa.String(length=40),
            server_default="NOT_REQUIRED_FOR_STAGE",
            nullable=False,
        ),
    )
    op.add_column(
        "portal_managed_users",
        sa.Column("ssh_key_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "portal_managed_users", sa.Column("staged_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "portal_managed_users",
        sa.Column("compute_activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_managed_user_ssh_key_count", "portal_managed_users", "ssh_key_count >= 0"
    )
    op.create_check_constraint(
        "ck_managed_user_active_login_contract",
        "portal_managed_users",
        "onboarding_state != 'ACTIVE' OR "
        "(ssh_key_count > 0 AND shell != '/usr/sbin/nologin' "
        "AND compute_activated_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_managed_user_no_early_ssh_ready",
        "portal_managed_users",
        "onboarding_state NOT IN ('DRAFT', 'STAGED') OR ssh_key_state != 'SSH_READY'",
    )
    op.add_column(
        "portal_ssh_keys", sa.Column("staging_file_name", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "portal_ssh_keys", sa.Column("content_sha256", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "portal_ssh_keys", sa.Column("approved_by", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "portal_ssh_keys", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "portal_ssh_keys", sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "portal_ssh_keys", sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_portal_ssh_keys_approved_by_portal_users",
        "portal_ssh_keys",
        "portal_users",
        ["approved_by"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_portal_ssh_keys_staging_file_name", "portal_ssh_keys", ["staging_file_name"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_portal_ssh_keys_staging_file_name", "portal_ssh_keys", type_="unique"
    )
    op.drop_constraint(
        "fk_portal_ssh_keys_approved_by_portal_users", "portal_ssh_keys", type_="foreignkey"
    )
    for column in (
        "installed_at",
        "validated_at",
        "approved_at",
        "approved_by",
        "content_sha256",
        "staging_file_name",
    ):
        op.drop_column("portal_ssh_keys", column)
    op.drop_constraint(
        "ck_managed_user_no_early_ssh_ready", "portal_managed_users", type_="check"
    )
    op.drop_constraint(
        "ck_managed_user_active_login_contract", "portal_managed_users", type_="check"
    )
    op.drop_constraint(
        "ck_managed_user_ssh_key_count", "portal_managed_users", type_="check"
    )
    for column in ("compute_activated_at", "staged_at", "ssh_key_count", "ssh_key_state"):
        op.drop_column("portal_managed_users", column)
