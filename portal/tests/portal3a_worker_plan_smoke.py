"""Run the fixed Portal-3A user.plan handler without a Worker socket."""

import json
import uuid

from h100_portal_worker.handlers import handle
from h100_portal_worker.schemas import WorkerRequest


def main() -> int:
    result = handle(
        WorkerRequest(
            protocol_version=1,
            request_id=str(uuid.uuid4()),
            operation_type="user.plan",
            payload={"username": "origin-pilot"},
            requested_by="origin-al",
            approved_by="origin-al",
            idempotency_key="portal3a-worker-plan-smoke-v1",
            dry_run=True,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    if result.get("status") != "DRY_RUN":
        return 1
    if result.get("execution_enabled") is not False:
        return 1
    if result.get("proposed_username") != "origin-pilot":
        return 1
    if result.get("plan_status") != "READY":
        return 1
    stage = handle(
        WorkerRequest(
            protocol_version=1,
            request_id=str(uuid.uuid4()),
            operation_type="user.stage",
            payload={
                "username": "origin-pilot",
                "uid": 20001,
                "gid": 20001,
                "project_id": 30001,
                "ssh_port": 22023,
                "quota_gb": 300,
                "slurm_account": "company",
                "slurm_qos": "general",
                "max_gpus": 1,
                "container_name": "gpu-dev-origin-pilot",
                "cpus": 8,
                "memory_gb": 32,
                "pids_limit": 4096,
                "gpu": "none",
                "expected_state": "DRAFT",
                "approval_reference": "portal3b-r-smoke-v1",
            },
            requested_by="origin-al",
            approved_by="origin-al",
            idempotency_key="portal3b-r-stage-smoke-v1",
            dry_run=True,
        )
    )
    if (
        stage.get("stage_status") != "READY"
        or stage.get("ssh_key_status") != "NOT_REQUIRED_FOR_STAGE"
    ):
        return 1
    missing_key = handle(
        WorkerRequest(
            protocol_version=1,
            request_id=str(uuid.uuid4()),
            operation_type="user.activate",
            payload={
                "managed_user_id": str(uuid.uuid4()),
                "expected_state": "STAGED",
                "approval_reference": "portal3b-r-smoke-v1",
            },
            requested_by="origin-al",
            approved_by="origin-al",
            idempotency_key="portal3b-r-activate-smoke-v1",
            dry_run=True,
        )
    )
    if missing_key.get("error", {}).get("code") != "PUBLIC_KEY_REQUIRED_FOR_ACTIVATION":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
