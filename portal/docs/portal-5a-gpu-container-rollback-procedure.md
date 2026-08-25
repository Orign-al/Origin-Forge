# Portal-5A GPU Container Rollback Procedure

Status: reviewed Phase 1 rollback procedure; execution requires change-owner
authorization.

## Safety rules

- Preserve user bytes at
  `/srv/gpu-platform/users/{username}/workspace`; never rename, copy, merge,
  overwrite, or delete them during release rollback.
- A possibly live GPU container retains its Slurm allocation. Do not release
  the GPU until UUID-bound container removal and a GPU-less stopped definition
  are proven.
- Use normal migration and fixed lifecycle tooling. Never edit production rows
  or host lifecycle records to manufacture a passing state.
- Keep activation, restore, job, terminal, and container operations closed
  until rollback acceptance passes.

## Contain and inventory

1. Record failure time, candidate/prior revisions, DB revision, completed
   deployment steps, service state, protected hashes, Slurm jobs/allocations,
   container IDs, GPU UUIDs, lifecycle/Lease coordinates, mounts, quotas, and
   workspace inventory.
2. Stop containers only through allocation-aware handlers. Retain/drain an
   allocation if container removal is not authoritative.
3. Require an empty managed-user queue and no live/stored GPU Development
   allocation before scheduler/schema rollback.
4. Take a fresh diagnostic backup while retaining the verified predeployment
   rollback point.

## Restore release components

1. Restore prior Slurm configuration while the queue is empty; verify node,
   partitions, epilog, and account/QOS enforcement.
2. Confirm no environment remains on `GPU_1_8CPU_32GB`, then run
   `alembic downgrade f6a7b8c9d0e1`. Do not bypass the downgrade guard.
3. Restore prior application release, protected scripts/manifest, and service
   units; verify hashes before Worker invocation.
4. Restore prior lifecycle files and stopped Compose definitions from the
   rollback point.

## Remove only candidate workspace aliases

For an alias created by this release, invoke:

```text
h100-workspace-alias remove USER UID GID --confirm-remove USER
```

The fixed handler must prove the exact mount unit, canonical target, backing
device/inode, owner, options, no dependent submount, and an empty unmounted
mountpoint. It removes only the systemd alias and mountpoint. The backing
workspace remains unchanged.

Restore exact `/etc/projects` and `/etc/projid` backups only if this deployment
changed them; reapply/verify project tags, 300 GiB limits, and usage. There is
no global `/storage/users` fstab mount to undo and no
`/srv/gpu-platform/workspaces/{uid}` tree to rename.

## Rollback acceptance

Before reopening, prove:

- installed Git/DB/config/hashes equal the recorded prior release;
- service, Worker socket, database, RBAC/CSRF, and owner-scoping health;
- backing workspace owner/mode, quota, file count/bytes, representative hashes,
  and reciprocal cross-user denial;
- prior CPU Development behavior with no NVIDIA runtime/env/DeviceRequest;
- expected Slurm GPU-job max-1 isolation;
- recycle/restore preservation supported by the prior release; and
- no residual GPU Development job, UUID-bound container, lifecycle allocation,
  or reconciliation record.

If any target, inode, ownership, inventory, allocation, or teardown state is
ambiguous, leave data and allocation in place, keep access closed, and
escalate. Availability never overrides data preservation or GPU isolation.
