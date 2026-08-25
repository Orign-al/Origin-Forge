"""add UID workspaces and optional one-GPU development profile

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CPU_PROFILE = "STANDARD_8CPU_32GB"


def upgrade() -> None:
    with op.batch_alter_table("portal_compute_resource_requests") as batch:
        batch.drop_constraint("ck_compute_resource_request_standard_container", type_="check")
        batch.create_check_constraint(
            "ck_compute_resource_request_standard_container",
            "requested_container_profile IN "
            "('STANDARD_8CPU_32GB', 'GPU_1_8CPU_32GB')",
        )
        batch.create_check_constraint(
            "ck_compute_resource_request_gpu_profile_entitlement",
            "requested_container_profile != 'GPU_1_8CPU_32GB' OR requested_gpu_max = 1",
        )

    with op.batch_alter_table("portal_provision_plans") as batch:
        batch.drop_constraint("ck_plan_profile", type_="check")
        batch.drop_constraint("ck_provision_plan_container_gpu", type_="check")
        batch.create_check_constraint(
            "ck_plan_profile",
            "container_profile IN ('STANDARD_8CPU_32GB', 'GPU_1_8CPU_32GB')",
        )
        batch.create_check_constraint(
            "ck_provision_plan_container_gpu",
            "(container_profile = 'STANDARD_8CPU_32GB' AND container_gpu = 0) OR "
            "(container_profile = 'GPU_1_8CPU_32GB' AND container_gpu = 1 AND gpu_max = 1)",
        )

    with op.batch_alter_table("portal_containers") as batch:
        batch.add_column(
            sa.Column(
                "development_profile",
                sa.String(length=64),
                nullable=False,
                server_default=CPU_PROFILE,
            )
        )
        batch.add_column(
            sa.Column("gpu_count", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch.add_column(sa.Column("gpu_allocation_job_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("gpu_allocation_uuid", sa.String(length=64), nullable=True))
        batch.create_unique_constraint(
            "uq_portal_containers_gpu_allocation_job_id", ["gpu_allocation_job_id"]
        )
        batch.create_unique_constraint(
            "uq_portal_containers_gpu_allocation_uuid", ["gpu_allocation_uuid"]
        )
        batch.create_check_constraint(
            "ck_portal_container_development_profile",
            "(development_profile = 'STANDARD_8CPU_32GB' AND gpu_count = 0 "
            "AND gpu_allocation_job_id IS NULL AND gpu_allocation_uuid IS NULL) OR "
            "(development_profile = 'GPU_1_8CPU_32GB' AND gpu_count = 1)",
        )
        batch.create_check_constraint(
            "ck_portal_container_running_gpu_allocation",
            "observed_state != 'RUNNING' OR development_profile = 'STANDARD_8CPU_32GB' OR "
            "(gpu_allocation_job_id IS NOT NULL AND gpu_allocation_uuid IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_portal_container_gpu_allocation_pair",
            "(gpu_allocation_job_id IS NULL AND gpu_allocation_uuid IS NULL) OR "
            "(gpu_allocation_job_id IS NOT NULL AND gpu_allocation_uuid IS NOT NULL)",
        )

    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(
            sa.text(
                "UPDATE portal_storage_resources SET root_path = '/storage/users/' || "
                "(SELECT uid FROM portal_managed_users "
                "WHERE portal_managed_users.id = portal_storage_resources.owner_managed_user_id)"
            )
        )
    else:
        op.execute(
            sa.text(
                "UPDATE portal_storage_resources AS storage "
                "SET root_path = '/storage/users/' || managed.uid::text "
                "FROM portal_managed_users AS managed "
                "WHERE managed.id = storage.owner_managed_user_id"
            )
        )


def downgrade() -> None:
    connection = op.get_bind()
    gpu_count = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM portal_containers "
            "WHERE development_profile = 'GPU_1_8CPU_32GB'"
        )
    )
    if gpu_count:
        raise RuntimeError("stop and remove GPU development profiles before schema downgrade")

    if connection.dialect.name == "sqlite":
        op.execute(
            sa.text(
                "UPDATE portal_storage_resources SET root_path = '/srv/gpu-platform/users/' || "
                "(SELECT unix_username FROM portal_managed_users "
                "WHERE portal_managed_users.id = portal_storage_resources.owner_managed_user_id)"
            )
        )
    else:
        op.execute(
            sa.text(
                "UPDATE portal_storage_resources AS storage "
                "SET root_path = '/srv/gpu-platform/users/' || managed.unix_username "
                "FROM portal_managed_users AS managed "
                "WHERE managed.id = storage.owner_managed_user_id"
            )
        )

    with op.batch_alter_table("portal_containers") as batch:
        batch.drop_constraint("ck_portal_container_gpu_allocation_pair", type_="check")
        batch.drop_constraint("ck_portal_container_running_gpu_allocation", type_="check")
        batch.drop_constraint("ck_portal_container_development_profile", type_="check")
        batch.drop_constraint("uq_portal_containers_gpu_allocation_uuid", type_="unique")
        batch.drop_constraint("uq_portal_containers_gpu_allocation_job_id", type_="unique")
        batch.drop_column("gpu_allocation_uuid")
        batch.drop_column("gpu_allocation_job_id")
        batch.drop_column("gpu_count")
        batch.drop_column("development_profile")

    with op.batch_alter_table("portal_provision_plans") as batch:
        batch.drop_constraint("ck_provision_plan_container_gpu", type_="check")
        batch.drop_constraint("ck_plan_profile", type_="check")
        batch.create_check_constraint(
            "ck_plan_profile", "container_profile = 'STANDARD_8CPU_32GB'"
        )
        batch.create_check_constraint("ck_provision_plan_container_gpu", "container_gpu = 0")

    with op.batch_alter_table("portal_compute_resource_requests") as batch:
        batch.drop_constraint(
            "ck_compute_resource_request_gpu_profile_entitlement", type_="check"
        )
        batch.drop_constraint("ck_compute_resource_request_standard_container", type_="check")
        batch.create_check_constraint(
            "ck_compute_resource_request_standard_container",
            "requested_container_profile = 'STANDARD_8CPU_32GB'",
        )
