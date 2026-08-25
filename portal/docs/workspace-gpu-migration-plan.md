# Workspace and GPU Profile Migration Plan

Status: reviewed Phase 1 execution plan; production execution requires an
immutable candidate, preflight, rollback point, and maintenance authorization.

## Invariants

- User bytes remain at
  `/srv/gpu-platform/users/{username}/workspace`; no rename, copy, merge, or
  synchronization is performed.
- `/storage/users/{uid}` is a per-user systemd bind alias to that backing
  workspace. The two paths must have the same device and inode.
- There is no global `/srv/gpu-platform/workspaces` tree and no global fstab
  bind for `/storage/users`.
- Existing container home, `/shared`, Compose data, Lease, job, and key records
  are preserved unless their normal lifecycle operation changes them.
- Existing environments stay `STANDARD_8CPU_32GB`; GPU Development is opt-in.

## Pre-migration gates

1. Freeze new activation, restore, job, terminal, and container lifecycle
   operations for the maintenance window.
2. Record the installed Git revision, database revision, service and protected
   script hashes, Slurm configuration, mount table, XFS project mappings,
   lifecycle files, Compose definitions, container states, Lease states, and
   active jobs.
3. Take and verify normal database and configuration backups. Never edit
   production rows to make a test pass.
4. Inventory every managed username, UID/GID, backing workspace, canonical
   alias, owner/mode, filesystem identity, project ID, quota use, file count,
   and representative hashes.
5. Require controlled container stops, an empty managed-user Slurm queue, and
   no live GPU Development allocation before scheduler or schema changes.

Any mismatch stops the deployment before a write.

## Workspace alias migration

For each inventoried user, invoke the fixed `h100-workspace-alias prepare`
handler. It must:

1. validate NSS UID/GID and the real backing directory;
2. preserve the backing workspace and create only missing managed subfolders;
3. create an empty root-owned `/storage/users/{uid}` mountpoint;
4. atomically install `storage-users-{uid}.mount` with
   `bind,rw,nosuid,nodev`;
5. start and enable that unit; and
6. prove source and target device/inode, owner, privacy, options, and exact unit
   contents.

The handler refuses symlinks, collisions, non-empty unmounted targets,
cross-user ownership, or a backing path outside the quota filesystem.

XFS project mappings use the canonical `/storage/users/{uid}` path while the
same underlying bytes retain their existing project identity and 300 GiB
limit. Quota usage must match the inventory before and after alias activation.

## Application, database, and Slurm migration

1. Install the exact candidate application, systemd units, fixed scripts, and
   matching `worker-scripts.json` as one release.
2. Rehearse and then run `alembic upgrade a7b8c9d0e1f2` normally. It adds the
   development profile/allocation constraints and maps storage records to the
   canonical UID path.
3. Preserve existing stopped container definitions. The Worker accepts an
   inventoried backing workspace mount only when it is the same inode as the
   canonical alias; new definitions use the canonical path.
4. Upgrade lifecycle files to version 4 through fixed lifecycle handlers,
   binding backing/canonical workspace, profile, Lease, and allocation.
5. Install the non-default `gpu-dev` partition with typed
   `--gres=gpu:h100:1`, `MaxTime=INFINITE`, the fixed hold command, and the
   integrity-pinned epilog.
6. Reload/restart services through normal deployment controls and keep user
   operations closed until postflight passes.

## Offline postflight

- every canonical alias is mounted and matches its backing inode;
- workspace ownership/mode, required folders, quota mapping, usage, file
  counts, and representative hashes match the inventory;
- CPU definitions expose no NVIDIA runtime, environment, DeviceRequest, or
  host device;
- lifecycle and database coordinates match owner, workspace, profile, Lease,
  and any GPU allocation;
- protected script hashes match the immutable manifest;
- `gpu-dev` is non-default, UP, H100-typed, and references the fixed epilog;
- API, Worker, Web, database, RBAC/CSRF, and Worker socket health pass.

Real H100, GPU=2 rejection, dual-user isolation, self-service flow, and
recycle/restore remain separate live gates after deployment.
