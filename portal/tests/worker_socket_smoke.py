#!/usr/bin/env python3
"""Exercise the installed Root Worker through its production Unix socket."""

import json
import sys
import uuid
from typing import Any

from h100_portal_api.worker_client import call_worker

READ_OPERATIONS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("platform.health.read", {}),
    ("gpu.list", {}),
    ("gpu.health.read", {}),
    ("slurm.node.read", {}),
    ("slurm.jobs.read", {}),
    ("slurm.accounts.read", {}),
    ("containers.list", {}),
    ("storage.summary.read", {}),
    ("quotas.list", {}),
    ("systemd.failed.read", {}),
    ("monitoring.alerts.read", {}),
    ("registry.status.read", {}),
    ("gpu_isolation.status.read", {}),
)

DRY_RUN_OPERATIONS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("user.plan", {"username": "origin-pilot"}),
    (
        "user.stage",
        {
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
    ),
    ("user.suspend", {"username": "example-user"}),
    ("container.start", {"name": "gpu-dev-example-user"}),
    ("container.stop", {"name": "gpu-dev-example-user"}),
    ("container.restart", {"name": "gpu-dev-example-user"}),
    ("container.rebuild", {"name": "gpu-dev-example-user"}),
    ("slurm.drain", {"node_name": "sagsh100server"}),
    ("slurm.resume", {"node_name": "sagsh100server"}),
    ("job.cancel", {"job_id": 1}),
    ("quota.update", {"username": "example-user", "quota_bytes": 322_122_547_200}),
    ("ssh_key.add", {"username": "example-user"}),
    ("ssh_key.revoke", {"username": "example-user"}),
)


def _safe_result(operation: str, response: dict[str, Any]) -> dict[str, Any]:
    error = response.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    if error_code is None and isinstance(error, dict):
        error_code = error.get("error_code")
    return {
        "operation_type": operation,
        "status": response.get("status"),
        "error_code": error_code,
    }


def main() -> int:
    results: list[dict[str, Any]] = []
    failed = False

    for operation, payload in READ_OPERATIONS:
        response = call_worker(operation, payload=payload, requested_by="portal-smoke")
        results.append(_safe_result(operation, response))
        if response.get("status") != "OK":
            failed = True

    container_response = call_worker("containers.list", payload={}, requested_by="portal-smoke")
    containers = container_response.get("containers", [])
    inspect_name = next(
        (
            item.get("Names")
            for item in containers
            if isinstance(item, dict)
            and isinstance(item.get("Names"), str)
            and item["Names"].startswith(("gpu-dev-", "h100-"))
        ),
        None,
    )
    if inspect_name is not None:
        response = call_worker(
            "containers.inspect",
            payload={"name": inspect_name},
            requested_by="portal-smoke",
        )
        results.append(_safe_result("containers.inspect", response))
        if response.get("status") != "OK":
            failed = True
    else:
        results.append(
            {
                "operation_type": "containers.inspect",
                "status": "SKIPPED_NO_ALLOWLISTED_CONTAINER",
                "error_code": None,
            }
        )

    for operation, payload in DRY_RUN_OPERATIONS:
        response = call_worker(
            operation,
            payload=payload,
            requested_by="portal-smoke",
            dry_run=True,
        )
        results.append(_safe_result(f"{operation}:dry-run", response))
        if response.get("status") != "DRY_RUN" or response.get("execution_enabled") is not False:
            failed = True

    activate_missing_key = call_worker(
        "user.activate",
        payload={
            "managed_user_id": str(uuid.uuid4()),
            "expected_state": "STAGED",
            "approval_reference": "portal3b-r-smoke-v1",
        },
        requested_by="portal-smoke",
        dry_run=True,
    )
    results.append(_safe_result("user.activate:missing-key", activate_missing_key))
    if activate_missing_key.get("error", {}).get("code") != "PUBLIC_KEY_REQUIRED_FOR_ACTIVATION":
        failed = True

    denied = call_worker(
        "user.plan",
        payload={"username": "origin-pilot"},
        requested_by="portal-smoke",
        dry_run=False,
    )
    results.append(_safe_result("user.plan:execution", denied))
    if denied.get("error", {}).get("code") != "WRITE_EXECUTION_DISABLED":
        failed = True

    arbitrary_path = call_worker(
        "containers.inspect",
        payload={"name": "../../etc/shadow"},
        requested_by="portal-smoke",
    )
    results.append(_safe_result("containers.inspect:arbitrary-path", arbitrary_path))
    if arbitrary_path.get("error", {}).get("code") != "PAYLOAD_REJECTED":
        failed = True

    unknown = call_worker("command.execute", payload={}, requested_by="portal-smoke")
    results.append(_safe_result("command.execute:unknown", unknown))
    if unknown.get("error", {}).get("code") != "PROTOCOL_REJECTED":
        failed = True

    print(json.dumps({"passed": not failed, "results": results}, indent=2, sort_keys=True))
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
