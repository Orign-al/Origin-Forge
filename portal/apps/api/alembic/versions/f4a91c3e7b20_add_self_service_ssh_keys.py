"""add self-service SSH key records

Revision ID: f4a91c3e7b20
Revises: 8c2b6f1a4d70
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f4a91c3e7b20"
down_revision: str | None = "8c2b6f1a4d70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    active_legacy_keys = op.get_bind().execute(
        sa.text("SELECT count(*) FROM portal_ssh_keys WHERE active = true")
    ).scalar_one()
    if active_legacy_keys:
        raise RuntimeError(
            "active legacy SSH keys require explicit migration before self-service enrollment"
        )
    op.drop_constraint(
        "ck_managed_user_ssh_key_count", "portal_managed_users", type_="check"
    )
    op.create_check_constraint(
        "ck_managed_user_ssh_key_count",
        "portal_managed_users",
        "ssh_key_count BETWEEN 0 AND 5",
    )
    op.alter_column(
        "portal_ssh_keys", "fingerprint", new_column_name="fingerprint_sha256"
    )
    op.alter_column(
        "portal_ssh_keys", "comment_summary", new_column_name="comment"
    )
    op.alter_column(
        "portal_ssh_keys", "public_key_ciphertext", new_column_name="public_key"
    )
    op.add_column(
        "portal_ssh_keys",
        sa.Column("scope", sa.String(length=16), server_default="BOTH", nullable=False),
    )
    op.add_column(
        "portal_ssh_keys",
        sa.Column("state", sa.String(length=16), server_default="VALIDATED", nullable=False),
    )
    op.add_column(
        "portal_ssh_keys",
        sa.Column(
            "generation_method",
            sa.String(length=32),
            server_default="IMPORTED",
            nullable=False,
        ),
    )
    op.add_column("portal_ssh_keys", sa.Column("created_by", sa.Uuid(), nullable=True))
    op.add_column(
        "portal_ssh_keys", sa.Column("enrollment_operation_id", sa.Uuid(), nullable=True)
    )
    op.execute(
        """
        UPDATE portal_ssh_keys AS key
           SET created_by = COALESCE(key.approved_by, managed.portal_user_id),
               state = CASE
                   WHEN key.revoked_at IS NOT NULL OR key.active = false THEN 'REVOKED'
                   WHEN key.installed_at IS NOT NULL THEN 'INSTALLED'
                   ELSE 'VALIDATED'
               END,
               revoked_at = CASE
                   WHEN key.active = false AND key.revoked_at IS NULL THEN key.created_at
                   ELSE key.revoked_at
               END,
               active = CASE
                   WHEN key.revoked_at IS NOT NULL OR key.active = false THEN false
                   ELSE true
               END
          FROM portal_managed_users AS managed
         WHERE managed.id = key.managed_user_id
        """
    )
    op.alter_column("portal_ssh_keys", "created_by", nullable=False)
    op.create_foreign_key(
        "fk_portal_ssh_keys_created_by_portal_users",
        "portal_ssh_keys",
        "portal_users",
        ["created_by"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_portal_ssh_keys_enrollment_operation",
        "portal_ssh_keys",
        "portal_operations",
        ["enrollment_operation_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_portal_ssh_key_scope",
        "portal_ssh_keys",
        "scope IN ('HOST', 'CONTAINER', 'BOTH')",
    )
    op.create_check_constraint(
        "ck_portal_ssh_key_state",
        "portal_ssh_keys",
        "state IN ('VALIDATED', 'INSTALLED', 'REVOKED')",
    )
    op.create_check_constraint(
        "ck_portal_ssh_key_generation_method",
        "portal_ssh_keys",
        "generation_method IN ('BROWSER_GENERATED', 'IMPORTED')",
    )
    op.create_check_constraint(
        "ck_portal_ssh_key_active_state",
        "portal_ssh_keys",
        "(state = 'REVOKED' AND active = false AND revoked_at IS NOT NULL) OR "
        "(state IN ('VALIDATED', 'INSTALLED') AND active = true AND revoked_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_portal_ssh_key_installed_at",
        "portal_ssh_keys",
        "state != 'INSTALLED' OR installed_at IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_portal_ssh_key_validated_binding",
        "portal_ssh_keys",
        "state = 'REVOKED' OR (public_key IS NOT NULL AND validated_at IS NOT NULL "
        "AND staging_file_name IS NOT NULL AND content_sha256 IS NOT NULL "
        "AND enrollment_operation_id IS NOT NULL)",
    )


def downgrade() -> None:
    self_service_keys = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM portal_ssh_keys "
            "WHERE enrollment_operation_id IS NOT NULL OR state != 'REVOKED'"
        )
    ).scalar_one()
    if self_service_keys:
        raise RuntimeError(
            "self-service SSH key records must be handled before lifecycle downgrade"
        )
    op.drop_constraint(
        "ck_managed_user_ssh_key_count", "portal_managed_users", type_="check"
    )
    op.create_check_constraint(
        "ck_managed_user_ssh_key_count", "portal_managed_users", "ssh_key_count >= 0"
    )
    for constraint in (
        "ck_portal_ssh_key_validated_binding",
        "ck_portal_ssh_key_installed_at",
        "ck_portal_ssh_key_active_state",
        "ck_portal_ssh_key_generation_method",
        "ck_portal_ssh_key_state",
        "ck_portal_ssh_key_scope",
    ):
        op.drop_constraint(constraint, "portal_ssh_keys", type_="check")
    op.drop_constraint(
        "fk_portal_ssh_keys_enrollment_operation", "portal_ssh_keys", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_portal_ssh_keys_created_by_portal_users", "portal_ssh_keys", type_="foreignkey"
    )
    op.drop_column("portal_ssh_keys", "enrollment_operation_id")
    op.drop_column("portal_ssh_keys", "created_by")
    op.drop_column("portal_ssh_keys", "generation_method")
    op.drop_column("portal_ssh_keys", "state")
    op.drop_column("portal_ssh_keys", "scope")
    op.alter_column("portal_ssh_keys", "public_key", new_column_name="public_key_ciphertext")
    op.alter_column("portal_ssh_keys", "comment", new_column_name="comment_summary")
    op.alter_column(
        "portal_ssh_keys", "fingerprint_sha256", new_column_name="fingerprint"
    )
