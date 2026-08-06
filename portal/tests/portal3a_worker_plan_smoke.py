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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
