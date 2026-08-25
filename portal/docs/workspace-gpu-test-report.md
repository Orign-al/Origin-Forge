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
- Ordinary-user dashboard, connection, container, and help views distinguish
  the default CPU profile, a stopped GPU profile, and a running GPU profile
  with a complete Slurm allocation. The connection API reports runtime GPU
  access only when the container is running, the Lease/key/environment gates
  pass, and the exact Slurm job/UUID pair is present.
- STAGED users are told to complete owner self-activation after registering
  their Container key; the UI no longer claims that a second administrator
  action is required or that every staged development profile is GPU-free.

## Automated results

| Gate                                      | Result                                   |
| ----------------------------------------- | ---------------------------------------- |
| `git diff --check`                        | PASS                                     |
| Ruff check / Python format                | PASS                                     |
| Fixed shell syntax                        | PASS                                     |
| API pytest                                | PASS — 145                               |
| Worker pytest                             | PASS — 174                               |
| Runtime/workspace contracts               | PASS — 27                                |
| Web Vitest                                | PASS — 52 across 13 files                |
| ESLint / TypeScript / Prettier            | PASS                                     |
| Next.js 16 production build (`--webpack`) | PASS — 26 static pages; 29 route entries |
| Playwright owner FAILED-retry flow        | PASS — 2 viewports                       |
| Protected script manifest                 | PASS — every checked-in SHA-256 matches  |

Pinned runtimes used: Python 3.14, Node 24.19.0, pnpm 11.20.0 with checked-in
dependencies, Playwright Chromium 1234. The final standalone artifact contains
the same BUILD_ID in the root and packaged trees and a non-empty server entry.

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

The next immutable candidate `e49754df6fe54282ac8da4f0a75a821288c1eb2f`
then passed source tests, bundle verification, fresh frozen preflight, a new
root-only rollback point, production runtime installation, Worker integrity
`16/16`, Alembic `f6 → a7`, and exact Slurm `gpu-dev`/epilog installation.
Postflight found one fail-closed compatibility defect before live GPU work:
legacy `origin-pilot` has a private `0750` owner-group workspace, accepted by
the authoritative alias verifier, while the shared managed-user helper required
exactly `0700`. Services were closed again; no lifecycle operation, Slurm job,
GPU process, Lease mutation, or user-data metadata change was used to bypass
the check. The follow-up centralizes the existing other-bits-zero predicate in
the shared helper and tests `0700`/`0750` acceptance plus `0755`/`0701`
rejection.

Candidate `5cc0421243a31f66f34d271db80a89225fd740ba` fixed that
workspace-mode mismatch and was installed with tree
`3bfc1feedc47b5dc013a5104bc8bc1ada50e7861`. Source/runtime, Worker integrity
`16/16`, database `a7b8c9d0e1f2`, exact Slurm configuration, all six workspace
aliases, and existing CPU-container security invariants passed. It is now
retired as a release candidate because the ordinary-user Web still hard-coded
GPU `NONE` and claimed that development containers never receive a GPU. The
follow-up candidate fixes that product/API visibility defect; it does not alter
the migration, Worker scripts, Slurm configuration, runtime handlers, or user
data.

## Read-only production evidence

- Last verified installed source/runtime Git:
  `5cc0421243a31f66f34d271db80a89225fd740ba`, tree
  `3bfc1feedc47b5dc013a5104bc8bc1ada50e7861`.
- Installed DB: `a7b8c9d0e1f2`; exact candidate Slurm config is live.
- API, Worker, Web, and expiry start commands completed, but their final
  active/ready/HTTP health has not been re-read because the production
  approval service returned `503`; reconcile remains disabled.
- Slurm queue and Portal active-job/restore/operation sets are empty, the node
  is idle, and no GPU compute process exists.
- `gpu-dev-umar` is stopped; its failed restore has
  `CONTAINER_START_FAILED`, no restored Lease, and a suspended container key.
- Worker integrity is `16/16`; source/runtime/tree and Slurm hashes match
  candidate `5cc0421`.
- User data, backing workspace, Compose definition, and container are retained.
- The root-only rollback point
  `portal-5a-phase1-pre-e49754d-20260825T130000Z` verifies completely and
  preserves the prior `ea69317 / f6` release plus the latest business state.
- Production has one recycled V2 lifecycle (`origin-pilot`), four recycled or
  failed V3 lifecycles, and one active V3 CPU container (`liuyijie`). Candidate
  regression covers the V2-to-V4 restore transition without modifying it
  during preflight.

## Remaining gates

This report does not represent real H100 or release acceptance. Before Phase 1
or Phase 2 can be marked PASS:

1. commit, bundle, and independently verify the dynamic GPU visibility
   follow-up candidate;
2. deploy that exact follow-up, require Worker integrity `16/16`, and pass the
   full static postflight including the legacy `0750` private workspace;
3. after service health is proven, have `umar` retry through the ordinary-user
   owner UI with a new idempotency key; the historical failed restore is not
   replayed automatically;
4. execute real GPU Development GPU=1 CUDA/UUID validation;
5. prove GPU=2 API rejection with no Worker/Slurm side effect;
6. run two ordinary users concurrently and prove different UUIDs, reciprocal
   GPU denial, workspace/log isolation, and cleanup; and
7. reconcile database, lifecycle, Slurm, Docker, workspace, and Lease state.

Until these live gates pass, the full platform release remains incomplete and
no user manual, DOCX, Release Notes, or final PASS report may be generated.
