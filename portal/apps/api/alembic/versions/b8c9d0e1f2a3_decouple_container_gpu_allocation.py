"""decouple persistent development containers from GPU allocation

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("portal_containers") as batch:
        batch.drop_constraint("ck_portal_container_running_gpu_allocation", type_="check")


def downgrade() -> None:
    with op.batch_alter_table("portal_containers") as batch:
        batch.create_check_constraint(
            "ck_portal_container_running_gpu_allocation",
            "observed_state != 'RUNNING' OR development_profile = 'STANDARD_8CPU_32GB' OR "
            "(gpu_allocation_job_id IS NOT NULL AND gpu_allocation_uuid IS NOT NULL)",
        )
