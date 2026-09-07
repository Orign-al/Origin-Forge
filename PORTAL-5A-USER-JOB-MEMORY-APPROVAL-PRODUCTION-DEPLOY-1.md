# PORTAL-5A User Job Memory Approval Production Deployment

TEST:
`PORTAL-5A-USER-JOB-MEMORY-APPROVAL-PRODUCTION-DEPLOY-1`

RESULT:
`PASS`

DEPLOYED AT:
`2026-09-07T21:46:26+08:00`

## Release identity

PREVIOUS PRODUCTION GIT:
`afaca8cdd379e3051edeac682b16e6cbdaa59557`

PREVIOUS PRODUCTION TREE:
`39c1587ee07f94d1bed92bf4fd87699a0e5f2902`

PRODUCTION GIT:
`81baffd2e0d029d00c36072d6c4bb66a37f69e07`

PRODUCTION TREE:
`88d5575ec6672f6611d0b992c914c4210f34e41e`

DATABASE:
`f5a6b7c8d9e0`

WEB BUILD ID:
`5zLWuQkUmjlfxeuN88Iop`

CLI VERSION:
`h100 1.0.0`

REPORT COMMIT:
`81698399c6d007b2edb608dd467d4dc888d16705`

MANAGEMENT AGENT:
`PASS`

MANAGEMENT USER:
`codexops`

ROOT SSH USED:
`NO`

The approved agent fingerprint was
`SHA256:vRb0Vno7tl/hcmiFeDiekEKTx1zsA/oPHvpsSoG3Vwc`. The independently pinned
H100 host fingerprint was
`SHA256:9TMlLHNTv1g+PxhdP5v7VcakCn7MofahpNpgJGt/vYs`.

## Artifact integrity

WEB ARTIFACT SHA-256:
`f6bb3eb85f64c4efa90c581efc0d546c4983eaa5dd516f926174cb562530f964`

MANIFEST SHA-256:
`02bd1e52e5df58688dfafa54cb5ad92f54c8bba25f8d2c685d87e41a282857fa`

BUNDLE SHA-256:
`0a1a011f19cfd5b7000a95a807dc12ed4e1a0c03c555671f67243b7f2d6af662`

All three hashes matched before the first production write and matched again
after staging. The bundle required exactly the previous production commit and
advertised exactly the deployed candidate. The installed Web runtime passed the
reviewed artifact tree auditor. All 21 installed Worker/runtime files matched
`/etc/h100-portal/worker-scripts.json` byte for byte.

## Rollback

ROLLBACK POINT:
`/srv/gpu-platform/backups/portal5a-memory-approval-pre-afaca8c-myl0WqWw`

The new rollback point contains the previous Git/Web/runtime, PostgreSQL dump
and restore list, database revision, service/timer state, managed script and
configuration archives, container and Docker-network manifests, Slurm/QoS
state, resource counts, listeners, queue and GPU state. Its own SHA-256 manifest
was verified before deployment. The incomplete directory ending in
`LJ2FwZhE` is not an approved rollback point and was not reused.

## Production health

API:
`PASS - READY (worker=true, database=true)`

WORKER:
`PASS - ACTIVE`

WEB:
`PASS - /login HTTP 200; /jobs HTTP 200`

PRIVATE CLI INGRESS:
`PASS`

PORTAL API LOOPBACK ONLY:
`YES - 127.0.0.1:18081`

FAILED SYSTEMD UNITS:
`0`

TIMERS:
`PASS`

The private CLI ingress had 14 listeners, each on a discovered managed
Development Container bridge gateway. It was not bound to `0.0.0.0`, the
EasyTier address, or a physical management address. Eight running Development
Containers each reached the identity endpoint from its own network and received
`H100 Portal / h100.cli.v1`.

## Memory approval contract

DIRECT MEMORY LIMIT:
`32768 MiB inclusive`

HIGH-MEMORY APPROVAL:
`PASS`

REQUIRED DETAILS:
`workload description, estimated memory breakdown, justification`

MAXIMUM REQUEST:
`486377 MiB`

ADMIN MAY REDUCE:
`YES`

ADMIN MAY INCREASE:
`NO`

OWNER CAN REVIEW:
`YES`

PLATFORM ADMIN CAN REVIEW:
`YES`

OPERATOR CAN REVIEW:
`NO`

AUDITOR CAN REVIEW:
`NO`

ORDINARY USER CAN REVIEW:
`NO`

The production backend exposes both reviewed memory-approval routes, rejects an
unauthenticated list request with HTTP 401, and loads memory review permissions
only for `platform_owner` and `platform_admin`. The production migration is at
the reviewed head and the Web release contains the high-memory request and
review UI. The exact deployed source/runtime had already passed the immutable
candidate behavior suite: 32 GiB direct submission, required details above the
threshold, no Worker/Slurm call while pending, reviewer reduction, rejection of
increases, and one Worker call only after both GPU and memory approvals complete.
No real user's policy, approval, Job, or requested memory was changed for this
deployment validation.

PENDING MEMORY APPROVALS AT VALIDATION:
`0`

PENDING GPU APPROVALS AT VALIDATION:
`0`

## GPU and container invariants

MULTI-GPU QOS POLICY:
`PASS - 13 managed users`

GENERAL QOS:
`gres/gpu=1`

APPROVED MULTI-GPU QOS:
`portal-approved-multigpu, gres/gpu=4`

EXISTING CONTAINER IDS CHANGED:
`0`

EXISTING CONTAINER IMAGES CHANGED:
`0`

EXISTING CONTAINER RESTART COUNTS INCREASED:
`0`

CLI AVAILABLE IN RUNNING DEVELOPMENT CONTAINERS:
`YES - h100 1.0.0`

CONTAINER CLI CONFIG:
`PASS - root-owned mode 0444, per-container managed gateway`

PRIVILEGED:
`FALSE`

HOST NETWORK / PID / IPC:
`ABSENT`

PERSISTENT GPU IN DEVELOPMENT CONTAINER:
`NONE`

DOCKER SOCKET / MUNGE / WORKER SOCKET:
`ABSENT`

Fourteen managed container definitions passed the isolation check. One existing
container, `gpu-dev-wangheyi`, changed from stopped to running during the
deployment window. It retained the same ID and image and still had restart count
zero; its new Lease and start time were concurrent normal user activity, not a
deployment restart or rebuild.

## Concurrent Jobs

JOB 414:
`COMPLETED / exit 0:0 / 2026-09-07 21:05:08+08 to 21:20:12+08`

Portal Job `bcd45dae-eef5-4f01-8ca3-6cd303949178` converged to `COMPLETED`.
Slurm and Portal agree. It started and completed naturally and was not modified
by deployment.

During and after validation, users independently submitted later Jobs. At the
last queue check Job 418, owned by `shengxiuqi`, was `PENDING` with exact Slurm
reason `(PartitionTimeLimit)` after requesting 12 hours. It was not cancelled,
requeued, edited, or otherwise modified. No GPU compute process was present at
the final check.

SLURM JOBS MODIFIED BY DEPLOYMENT:
`0`

GPU POLICY MODIFIED OUTSIDE REVIEWED QOS INSTALL:
`NO`

USER DATA MODIFIED BY DEPLOYMENT:
`NO`

## Validation provenance

The immutable candidate gates remain applicable because production installed
the exact reviewed Git tree, artifacts and runtime hashes. They recorded:

- Python 3.14 API/Worker/runtime: 445 passed.
- Affected Python suite after linear migration correction: 220 passed.
- Vitest: 72 passed.
- Playwright: 58 passed, 22 conditional visual snapshots skipped.
- Strict MyPy, Ruff, TypeScript, ESLint and Prettier: PASS.
- PostgreSQL 15 upgrade/downgrade/re-upgrade and Alembic check: PASS.
- Relocation A/B, production installer rehearsal and Web rollback: PASS.

DOCUMENTATION:
`UPDATED IN DEPLOYED CANDIDATE`

The Chinese and English manuals and release notes document the high-memory
approval workflow without exposing internal management topology.

FULL PLATFORM RELEASE:
`PASS`
