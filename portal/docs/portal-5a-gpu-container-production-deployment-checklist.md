# Portal-5A GPU Container Production Deployment Checklist

Status: Phase 1 change checklist; execution requires an immutable candidate,
verified rollback point, and maintenance authorization.

Migration head: `a7b8c9d0e1f2`

## Candidate gate

- [ ] Record full candidate commit/tree and confirm a clean worktree.
- [x] Ruff, Python formatting, shell syntax, and diff checks pass.
- [x] API tests pass: 144.
- [x] Worker tests pass: 174.
- [x] Runtime/workspace contracts pass: 26.
- [x] Web Vitest passes: 48; ESLint, TypeScript, and Prettier pass.
- [x] Next production build and owner FAILED-retry Playwright flow pass.
- [x] PostgreSQL 16 rehearsal passes for `f6 → a7 → f6 → a7` with a
      representative workspace/container row.
- [x] Source scripts match `worker-scripts.json`.
- [ ] Record peer/security review and maintenance authorization.

Historical `FAIL_CLOSED` reports document earlier non-deployed attempts. They
do not replace the current candidate evidence or authorize live testing.

## Closed deployment attempt 1

- Candidate `4cdbf7e4560d297f4ce7e645f94c4ac2598d106e` is retired and must not
  be deployed again.
- Preflight and root-only rollback point
  `portal-5a-phase1-pre-4cdbf7e-20260825T120000Z` passed before installation.
- The post-install/pre-migration Worker integrity gate rejected three changed
  scripts that the installer had not published: `h100-container-create`,
  `h100-container-rebuild`, and `h100-user-create`.
- No database migration, Slurm change, user lifecycle operation, container
  start/stop, Lease mutation, or workspace mutation occurred.
- Exact rollback to Git/runtime `ea69317fe72b64c3ef428402dce5036260589f3e`
  and DB `f6a7b8c9d0e1` passed; legacy integrity was `14/14`, services reopened,
  Slurm remained empty/idle, `liuyijie` remained healthy and GPU-less, and
  workspace inodes remained unchanged.
- The follow-up candidate must install every root allowlisted manifest script
  and pass the production `script_integrity()` gate before migration.

## Production preflight and rollback point

- [ ] Freeze new activation, restore, job, terminal, and container operations.
- [ ] Record installed Git `ea69317fe72b64c3ef428402dce5036260589f3e` and DB
      `f6a7b8c9d0e1` unless read-only preflight proves otherwise.
- [ ] Back up and verify PostgreSQL, application release, service units,
      protected scripts/manifest, Slurm config, lifecycle files, Compose,
      `/etc/projects`, `/etc/projid`, and per-user mount units.
- [ ] Inventory managed users, UID/GID, profile, Lease, container, jobs,
      allocation, backing/canonical workspace inode, owner/mode, quota, file
      count, bytes, and representative hashes.
- [ ] Prove the managed-user Slurm queue and GPU Development allocation set are
      empty before scheduler/schema changes.
- [ ] Confirm no global `/srv/gpu-platform/workspaces` or fstab bind is used.
- [x] Read-only preflight identified one recycled V2 lifecycle and five V3
      lifecycles. The candidate proves the V2 owner/key and original expired
      Lease payload, promotes it atomically to V4 before start, and restores
      the exact V2 file on failure.

## Install and postflight

- [ ] Install the exact candidate and protected scripts as one release.
- [ ] Prepare/verify each per-user systemd bind alias:
      `/srv/gpu-platform/users/{username}/workspace` → `/storage/users/{uid}`
      with same device/inode and `rw,nosuid,nodev`.
- [ ] Preserve existing user bytes, container home, `/shared`, Compose, Lease,
      job, and key records; never rename/copy/merge workspace data.
- [ ] Run the normal Alembic upgrade and verify `a7b8c9d0e1f2`, CPU defaults,
      UID storage roots, and GPU profile/allocation constraints.
- [ ] Install/reconfigure `gpu-dev`: `Default=NO`, `State=UP`,
      `MaxTime=INFINITE`, typed H100 GRES, fixed sleep hold, exact epilog path.
- [ ] Restart services and verify health, Worker socket, installed hashes,
      database connectivity, RBAC/CSRF, lifecycle, mounts, quotas, and stopped
      CPU definitions with no NVIDIA surface.

## Live Phase 1/2 acceptance

- [ ] A real ordinary user selects GPU Development and receives exactly one
      Slurm H100 allocation and exact Docker UUID binding.
- [ ] Container is running/healthy; CUDA Driver API initializes, enumerates one
      device, creates/synchronizes/destroys a context, and `nvidia-smi` reports
      the allocated UUID.
- [ ] CPU Development remains default, unprivileged, GPU-less, socket-less,
      and outside host namespaces.
- [ ] GPU=2 is rejected by API; Worker is not called and no Slurm job exists.
- [ ] Two ordinary users concurrently receive different UUIDs, cannot observe
      the other GPU, and see only owner-scoped logs/workspaces.
- [ ] Stop/recycle removes the UUID-bound container before allocation release;
      terminal/replayed Slurm cleanup is owner/account/QOS/profile proven.
- [ ] Restore binds the new Lease before start; failure returns to the exact
      prior recycled Lease without fabricated timestamps or DB edits.
- [ ] Cleanup leaves no residual job, allocation, Docker DeviceRequest,
      lifecycle allocation coordinate, or cross-user artifact.

## Abort conditions

Abort and keep user operations closed for any mismatch among User → Portal →
Worker → Slurm → GPU UUID → Container; any privileged/host namespace/socket or
extra-device exposure; ambiguous workspace identity; migration/hash failure;
or incomplete cleanup. If container removal is uncertain, retain the Slurm
allocation and drain/reconcile the node before any rollback.
