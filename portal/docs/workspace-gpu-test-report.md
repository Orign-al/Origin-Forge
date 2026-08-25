# Phase 1 Workspace and GPU Development Candidate Test Report

Date: 2026-08-25 UTC

Scope: candidate implementation and read-only production preflight. No
production database row, user resource, container, Slurm job, GPU allocation,
mount, or user data was created or modified by this report.

Status: **CANDIDATE TESTS PASS; PRODUCTION DEPLOYMENT/LIVE ACCEPTANCE PENDING**

## Candidate behavior covered

- CPU Development remains the default and rejects NVIDIA runtime/environment,
  DeviceRequests, direct devices, privileged mode, host namespaces, Docker
  socket, MUNGE, host root, capabilities, and unapproved mounts.
- GPU Development requests typed `--gres=gpu:h100:1` on non-default
  `gpu-dev`, proves owner/account/QOS/creating-Lease comment and one scheduler
  index, maps it to one physical UUID, and creates one UUID-specific NVIDIA
  DeviceRequest.
- Runtime postflight proves exact UUID visibility, `nvidia-smi`, CUDA Driver API
  initialization, one-device enumeration, and CUDA context
  create/synchronize/destroy.
- GPU count `2` is rejected before Worker execution and cannot create a Slurm
  job.
- GPU teardown removes/recreates the container GPU-less before cancellation.
  Replayed cleanup accepts only an exact owner/account/QOS/partition/H100
  terminal `sacct` record and confirms terminal state after `scancel`.
- The fail-safe Slurm epilog removes a matching UUID-bound container before
  resource release or returns nonzero so Slurm drains the node.
- `/storage/users/{uid}` is a per-user systemd bind alias to the existing
  `/srv/gpu-platform/users/{username}/workspace`; device/inode, UID/GID,
  privacy, `rw,nosuid,nodev`, required directories, and quota are checked.
- Existing backing workspace and `/shared` mounts are accepted only under their
  explicit compatibility contracts; no global fstab bind or data move exists.
- Start/restart use the current bounded Lease. Recycle proves the exact
  lifecycle Lease/expiry/profile/GPU allocation. Restore atomically binds the
  new Lease before start and rolls back keys, container, allocation, and prior
  lifecycle on failure.
- A production V2 lifecycle without embedded Lease coordinates is accepted
  only for the CPU profile after its owner, UID/GID, Slurm account/QOS, key
  fingerprints, and API-owned expired recycle Lease window validate. Restore
  atomically promotes it to V4 with the canonical workspace and new Lease;
  failure reinstates the exact original V2 bytes.
- API postcondition/persistence rollback uses a typed
  `resource.restore.rollback` operation that proves the attempted Lease and
  restore request before reinstating the exact prior recycled Lease; it does
  not fabricate an expiry timestamp or edit production data.
- A failed recycle item is owner-retryable with a new idempotency key and no
  administrator approval. Browser coverage verifies the button and success
  flow.

## Automated results

| Gate                                      | Result                                  |
| ----------------------------------------- | --------------------------------------- |
| `git diff --check`                        | PASS                                    |
| Ruff check / Python format                | PASS                                    |
| Fixed shell syntax                        | PASS                                    |
| API pytest                                | PASS — 144                              |
| Worker pytest                             | PASS — 174                              |
| Runtime/workspace contracts               | PASS — 26                               |
| Web Vitest                                | PASS — 48 across 12 files               |
| ESLint / TypeScript / Prettier            | PASS                                    |
| Next.js 16 production build (`--webpack`) | PASS — 26 routes                        |
| Playwright owner FAILED-retry flow        | PASS — 2 viewports                      |
| Protected script manifest                 | PASS — every checked-in SHA-256 matches |

Pinned runtimes used: Python 3.14, Node 24.19.0, pnpm-compatible checked-in
dependencies, Playwright Chromium 1234.

## PostgreSQL migration rehearsal

An isolated, localhost-only PostgreSQL 16 container was used; it contained only
test fixtures and was removed after the gate.

1. Full migration chain to production baseline `f6a7b8c9d0e1`: PASS.
2. Insert representative managed user, legacy storage row, and stopped CPU
   container: PASS.
3. Upgrade `f6 → a7b8c9d0e1f2`: PASS.
4. Verify storage root `/storage/users/20999`, CPU profile/GPU-zero defaults,
   profile entitlement, allocation-pair, and running-GPU constraints: PASS.
5. Downgrade `a7 → f6`: PASS; legacy storage root restored and four candidate
   container columns removed.
6. Re-upgrade `f6 → a7`: PASS.

## Closed production deployment attempt

Candidate `4cdbf7e4560d297f4ce7e645f94c4ac2598d106e` passed the source tests
but is retired after the production post-install/pre-migration gate found that
`install-runtime.sh` did not publish three changed hash-pinned scripts:
`h100-container-create`, `h100-container-rebuild`, and `h100-user-create`.
Worker integrity failed closed for exactly those three paths.

The attempt had already created and verified the root-only rollback point
`/srv/gpu-platform/backups/portal-5a-phase1-pre-4cdbf7e-20260825T120000Z`.
No Alembic migration, Slurm configuration change, user lifecycle operation,
container action, Lease mutation, or workspace mutation occurred. Rollback
restored exact source/runtime `ea69317fe72b64c3ef428402dce5036260589f3e`,
DB `f6a7b8c9d0e1`, the prior local-image manifest, protected scripts/units, and
the old `14/14` Worker integrity set. API/Worker/Web/expiry, empty Slurm queue,
idle node, `liuyijie` healthy GPU-less CPU container, stopped `umar`, and
workspace device/inodes all passed postflight.

The follow-up adds a contract test requiring every root allowlisted manifest
script to be hash-bound to its source and explicitly installed. A new commit,
bundle, fresh preflight, and fresh rollback point are required before retry.

## Read-only production evidence

- Installed Git: `ea69317fe72b64c3ef428402dce5036260589f3e`.
- Installed DB: `f6a7b8c9d0e1`.
- API, Worker, and Web services are active.
- `gpu-dev-umar` is stopped; its failed restore has
  `CONTAINER_START_FAILED`, no restored Lease, and a suspended container key.
- Production files match the old baseline hashes, proving the candidate and
  restore-order fix are not yet deployed.
- User data, backing workspace, Compose definition, and container are retained.
- Production has one recycled V2 lifecycle (`origin-pilot`), four recycled or
  failed V3 lifecycles, and one active V3 CPU container (`liuyijie`). Candidate
  regression covers the V2-to-V4 restore transition without modifying it
  during preflight.

## Remaining gates

This report does not represent real H100 or release acceptance. Before Phase 1
or Phase 2 can be marked PASS:

1. create and record an immutable candidate plus verified production rollback
   point;
2. complete production preflight and deploy that exact candidate;
3. have `umar` retry through the owner UI with a new idempotency key;
4. execute real GPU Development GPU=1 CUDA/UUID validation;
5. prove GPU=2 API rejection with no Worker/Slurm side effect;
6. run two ordinary users concurrently and prove different UUIDs, reciprocal
   GPU denial, workspace/log isolation, and cleanup; and
7. reconcile database, lifecycle, Slurm, Docker, workspace, and Lease state.

Until these live gates pass, the full platform release remains incomplete and
no user manual, DOCX, Release Notes, or final PASS report may be generated.
