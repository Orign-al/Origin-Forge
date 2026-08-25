# Workspace and GPU Profile Deployment and Rollback

Status: reviewed Phase 1 procedure; not itself an authorization to deploy.

## Deployment order

1. Freeze new user lifecycle/job/terminal operations and satisfy every
   preflight gate in `workspace-gpu-migration-plan.md`.
2. Record the immutable candidate commit/tree, migration head, source and
   installed protected-script hashes, database/configuration backup IDs, and
   production inventory.
3. Install the release and fixed runtime scripts without starting a GPU
   container.
4. Prepare/verify each per-user `/storage/users/{uid}` systemd bind alias to
   `/srv/gpu-platform/users/{username}/workspace`. Never move or copy user
   bytes and never add a global fstab bind.
5. Run the reviewed Alembic upgrade through normal deployment tooling.
6. Install/reconfigure the non-default H100-typed `gpu-dev` partition and its
   integrity-pinned fail-safe epilog while the managed-user queue is empty.
7. Restart API, Worker, and Web; verify database revision, service health,
   socket permissions, hashes, mounts, quotas, lifecycle records, stopped
   container definitions, and CPU GPU-less postconditions.
8. Reopen only the accounts required for the approved live acceptance. Run CPU
   regression, real GPU=1, GPU=2 API rejection, dual-user GPU isolation,
   workspace/Slurm flow, and recycle/restore in that order.
9. Prove cleanup and reconcile Docker, Slurm, lifecycle, database, filesystem,
   and Lease coordinates before normal user operations reopen.

Abort on the first ambiguous mount, ownership, quota, migration, protected
hash, Lease, scheduler/runtime UUID, CUDA, cross-user isolation, or cleanup
postcondition.

## Rollback point

The rollback point consists of:

- prior application Git revision and immutable release directory;
- verified PostgreSQL backup at `f6a7b8c9d0e1`;
- exact Slurm configuration and node/partition state;
- protected scripts and `worker-scripts.json`;
- systemd units, mount inventory, `/etc/projects`, and `/etc/projid`;
- lifecycle files, Compose definitions, container state, and Lease/job
  inventory; and
- per-user backing/canonical device/inode, owner/mode, quota usage, file count,
  and representative hashes.

No rollback step deletes a backing workspace.

## Reverse-order rollback

1. Close user operations. Stop GPU containers through allocation-aware
   handlers. If removal is unproven, retain the allocation and drain the node.
2. Require no live GPU Development profile/allocation before schema downgrade.
3. Restore the prior Slurm configuration while the queue is empty.
4. Run `alembic downgrade f6a7b8c9d0e1` normally. Do not bypass its GPU-profile
   guard or edit rows.
5. Restore the prior application, fixed scripts, manifest, and service units;
   verify hashes before Worker use.
6. Restore prior lifecycle and stopped Compose definitions.
7. For aliases created by this release, call the fixed
   `h100-workspace-alias remove USER UID GID --confirm-remove USER` handler.
   It must prove the exact unit, mount, backing inode, no submounts, and an empty
   unmounted target before removing only the alias/mountpoint.
8. Restore exact project mapping files if changed and revalidate XFS project
   tags, limits, and usage against both inventories.
9. Restart the prior release and run its CPU workspace, isolation, quota,
   lifecycle, GPU job, RBAC/CSRF, and service regression gates before reopening.

The authoritative backing path remains
`/srv/gpu-platform/users/{username}/workspace` throughout deployment and
rollback. If any identity or inventory comparison is ambiguous, leave the
alias/data in place, keep access closed, and escalate rather than copying,
renaming, merging, or deleting data.
