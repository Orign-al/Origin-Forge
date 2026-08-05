from collections.abc import Iterable

from h100_portal_api.enums import OperationStatus, RiskLevel

READ_OPERATION_TYPES = {
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

WRITE_OPERATION_TYPES = {
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

HIGH_RISK_OPERATION_TYPES = {
    "user.stage",
    "user.activate",
    "user.suspend",
    "slurm.drain",
    "slurm.resume",
    "quota.update",
    "container.stop",
    "container.rebuild",
    "ssh_key.revoke",
}

OPERATION_PERMISSIONS = {
    "user.plan": "users.write",
    "user.stage": "users.write",
    "user.activate": "users.write",
    "user.suspend": "users.write",
    "container.start": "containers.write",
    "container.stop": "containers.write",
    "container.restart": "containers.write",
    "container.rebuild": "containers.write",
    "slurm.drain": "slurm.write",
    "slurm.resume": "slurm.write",
    "job.cancel": "jobs.write",
    "quota.update": "users.write",
    "ssh_key.add": "users.write",
    "ssh_key.revoke": "users.write",
}


def risk_for(operation_type: str) -> RiskLevel:
    if operation_type in {"slurm.resume", "user.activate", "user.suspend"}:
        return RiskLevel.CRITICAL
    if operation_type in HIGH_RISK_OPERATION_TYPES:
        return RiskLevel.HIGH
    if operation_type in WRITE_OPERATION_TYPES:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def is_known(operation_type: str) -> bool:
    return operation_type in READ_OPERATION_TYPES or operation_type in WRITE_OPERATION_TYPES


ALLOWED_TRANSITIONS: dict[OperationStatus, set[OperationStatus]] = {
    OperationStatus.DRAFT: {OperationStatus.PENDING_APPROVAL, OperationStatus.CANCELLED},
    OperationStatus.PENDING_APPROVAL: {
        OperationStatus.APPROVED,
        OperationStatus.CANCELLED,
        OperationStatus.EXPIRED,
    },
    OperationStatus.APPROVED: {OperationStatus.QUEUED, OperationStatus.CANCELLED},
    OperationStatus.QUEUED: {OperationStatus.RUNNING, OperationStatus.CANCELLED},
    OperationStatus.RUNNING: {
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.ROLLING_BACK,
    },
    OperationStatus.ROLLING_BACK: {OperationStatus.ROLLED_BACK, OperationStatus.FAILED},
}


def can_transition(current: OperationStatus, target: OperationStatus) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def allowed_operation_types() -> Iterable[str]:
    return sorted(READ_OPERATION_TYPES | WRITE_OPERATION_TYPES)
