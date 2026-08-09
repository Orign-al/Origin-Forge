from collections.abc import Iterable

from fastapi import HTTPException, status

from h100_portal_api.models import PortalUser

PERMISSIONS: dict[str, set[str]] = {
    "platform_owner": {
        "platform.read",
        "users.read",
        "users.write",
        "containers.read",
        "containers.write",
        "slurm.read",
        "slurm.write",
        "jobs.read",
        "jobs.write",
        "gpu.read",
        "storage.read",
        "images.read",
        "images.write",
        "monitoring.read",
        "operations.read",
        "operations.write",
        "audit.read",
        "settings.read",
        "settings.write",
        "owner.manage",
    },
    "platform_admin": {
        "platform.read",
        "users.read",
        "users.write",
        "containers.read",
        "containers.write",
        "slurm.read",
        "slurm.write",
        "jobs.read",
        "jobs.write",
        "gpu.read",
        "storage.read",
        "images.read",
        "images.write",
        "monitoring.read",
        "operations.read",
        "operations.write",
        "audit.read",
    },
    "operator": {
        "platform.read",
        "containers.read",
        "containers.write",
        "slurm.read",
        "jobs.read",
        "jobs.write",
        "gpu.read",
        "storage.read",
        "images.read",
        "monitoring.read",
        "operations.read",
    },
    "auditor": {
        "platform.read",
        "users.read",
        "containers.read",
        "slurm.read",
        "jobs.read",
        "gpu.read",
        "storage.read",
        "images.read",
        "monitoring.read",
        "operations.read",
        "audit.read",
    },
    "user": {
        "self.environment.read",
        "self.lease.read",
        "self.lease.renew.request",
        "self.recycle.read",
        "self.restore.request",
        "self.jobs.submit",
        "self.jobs.read",
        "self.jobs.cancel",
        "self.container.read",
        "self.container.start",
        "self.container.stop",
        "self.container.restart",
        "self.ssh_keys.read",
        "self.ssh_keys.write",
        "self.storage.read",
        "self.connection.read",
    },
}


def role_names(user: PortalUser) -> list[str]:
    return sorted(role.name for role in user.roles)


def has_permission(user: PortalUser, permission: str) -> bool:
    return any(permission in PERMISSIONS.get(role, set()) for role in role_names(user))


def require_permission(user: PortalUser, permission: str) -> None:
    if not has_permission(user, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "当前账号无权执行此操作"},
        )


def highest_role(user: PortalUser) -> str:
    order = ["platform_owner", "platform_admin", "operator", "auditor", "user"]
    names = set(role_names(user))
    return next((role for role in order if role in names), "user")


def assignable_roles(actor: PortalUser) -> Iterable[str]:
    role = highest_role(actor)
    if role == "platform_owner":
        return PERMISSIONS.keys()
    if role == "platform_admin":
        return ("operator", "auditor", "user")
    return ()
