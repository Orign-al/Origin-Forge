import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KNOWN_READS = {
    "platform.health.read",
    "gpu.list",
    "gpu.health.read",
    "slurm.node.read",
    "slurm.jobs.read",
    "slurm.accounts.read",
    "containers.list",
    "containers.inspect",
    "storage.summary.read",
    "quotas.list",
    "systemd.failed.read",
    "monitoring.alerts.read",
    "registry.status.read",
    "gpu_isolation.status.read",
}
KNOWN_WRITES = {
    "user.plan",
    "user.stage",
    "user.activate",
    "user.suspend",
    "container.start",
    "container.stop",
    "container.restart",
    "container.rebuild",
    "slurm.drain",
    "slurm.resume",
    "job.cancel",
    "quota.update",
    "ssh_key.add",
    "ssh_key.revoke",
}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,127}$")
SAFE_USERNAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1]
    request_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    operation_type: str = Field(min_length=3, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    approved_by: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,128}$")
    dry_run: bool = False

    @field_validator("operation_type")
    @classmethod
    def known_operation(cls, value: str) -> str:
        if value not in KNOWN_READS | KNOWN_WRITES:
            raise ValueError("unknown operation type")
        return value


def validate_payload(operation_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if operation_type == "containers.inspect":
        name = payload.get("name")
        if not isinstance(name, str) or not SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError("invalid container name")
        return {"name": name}
    if operation_type in {"job.cancel"}:
        job_id = payload.get("job_id")
        if not isinstance(job_id, int) or not 0 < job_id < 2**63:
            raise ValueError("invalid job id")
        return {"job_id": job_id}
    if operation_type.startswith("user."):
        username = payload.get("username")
        if not isinstance(username, str) or not SAFE_USERNAME.fullmatch(username):
            raise ValueError("invalid username")
        if username in {"root", "origin-al", "codexops"}:
            raise ValueError("protected username")
        return {"username": username}
    if operation_type == "quota.update":
        username = payload.get("username")
        quota_bytes = payload.get("quota_bytes")
        if not isinstance(username, str) or not SAFE_USERNAME.fullmatch(username):
            raise ValueError("invalid username")
        if not isinstance(quota_bytes, int) or not 0 < quota_bytes <= 10 * 1024**4:
            raise ValueError("invalid quota")
        return {"username": username, "quota_bytes": quota_bytes}
    if operation_type.startswith("slurm."):
        node_name = payload.get("node_name", "sagsh100server")
        if not isinstance(node_name, str) or not SAFE_IDENTIFIER.fullmatch(node_name):
            raise ValueError("invalid node name")
        return {"node_name": node_name}
    if operation_type.startswith("container."):
        name = payload.get("name")
        if not isinstance(name, str) or not SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError("invalid container name")
        return {"name": name}
    if operation_type.startswith("ssh_key."):
        username = payload.get("username")
        if not isinstance(username, str) or not SAFE_USERNAME.fullmatch(username):
            raise ValueError("invalid username")
        return {"username": username}
    return {}
