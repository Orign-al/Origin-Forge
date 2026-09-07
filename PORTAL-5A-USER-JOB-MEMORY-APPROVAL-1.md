# PORTAL-5A-USER-JOB-MEMORY-APPROVAL-1

## Result

TEST:
`PORTAL-5A-USER-JOB-MEMORY-APPROVAL-1`

RESULT:
`CANDIDATE_READY`

PRODUCTION PARENT:
`afaca8cdd379e3051edeac682b16e6cbdaa59557`

PRODUCTION TREE:
`39c1587ee07f94d1bed92bf4fd87699a0e5f2902`

NEW CANDIDATE:
`81baffd2e0d029d00c36072d6c4bb66a37f69e07`

NEW TREE:
`88d5575ec6672f6611d0b992c914c4210f34e41e`

CLI VERSION:
`h100 1.0.0`

PRODUCTION DB:
`e4f5a6b7c8d9`

CANDIDATE DB HEAD:
`f5a6b7c8d9e0`

PRODUCTION MODIFIED:
`NO`

This phase built and rehearsed a new immutable candidate only. It did not deploy,
migrate production, change services, touch a Job, allocate a GPU, change a Lease,
or modify a Development Container.

## Product contract

- Job memory from 256 MiB through 32768 MiB (32 GiB inclusive) follows the
  existing direct submission path and creates no memory approval record.
- A request above 32768 MiB requires a workload description, estimated memory
  breakdown, and high-memory justification. It remains `APPROVAL_PENDING` with
  no Worker call and no Slurm Job until approved.
- The maximum accepted request is the reviewed single-node Slurm contract,
  `RealMemory=486377 MiB`. The same ceiling is enforced by Web, CLI, API,
  database constraints, and Worker validation.
- Only `platform_owner` and `platform_admin` can read or review memory
  approvals. A decision requires CSRF validation and recent password
  reauthentication. Operator, auditor, and ordinary-user API access is denied;
  frontend visibility follows the backend roles.
- A reviewer may approve any value from 256 MiB through the value requested by
  the user. The reviewer may reduce an unreasonable request but cannot increase
  it. The approved value is the exact `--mem=<value>M` sent through the reviewed
  Worker/Slurm path.
- The request narrative stays in Portal. It is not sent to Worker, Slurm, or the
  Job environment. The Worker receives only the immutable approval binding:
  approval ID, requested and approved memory, script SHA-256, reviewer, and
  review time.

Memory and multi-GPU approvals are independent. If one Job requires both, the
first approval does not call Worker. Only after both are approved does the API
make exactly one call using stable idempotency key
`job-resource-approval-submit:<portal-job-id>`. A rejection closes the Job and
cancels the other still-pending approval. User cancellation before completion
cancels every still-pending approval without creating a Slurm Job.

## Migration and rollback

The candidate migrations are linear from the authoritative production head:

```text
e4f5a6b7c8d9 (production)
  -> e3f4a5b6c7d8 (multi-GPU approval)
  -> f5a6b7c8d9e0 (high-memory approval)
```

An initially detected parallel-branch topology was rejected during rehearsal
because it could not return cleanly to the production head. Before the candidate
was frozen, the two not-yet-deployed migrations were linearized as above.
PostgreSQL 15 then passed the exact sequence:

```text
e4f5a6b7c8d9 -> f5a6b7c8d9e0
f5a6b7c8d9e0 -> e4f5a6b7c8d9
e4f5a6b7c8d9 -> f5a6b7c8d9e0
alembic check: no new upgrade operations
```

Downgrade fails closed while high-memory or multi-GPU approval records, or Jobs
that cannot satisfy the production constraints, remain. It never deletes or
rewrites those records automatically. Deployment and rollback ordering is in
`portal/docs/multigpu-approval-deployment-and-rollback.md`.

## Validation

| Gate | Result |
| --- | --- |
| Python 3.14 full API/Worker/runtime suite | PASS — 445 passed, one upstream deprecation warning |
| Affected Python suite after migration correction | PASS — 220 passed |
| Strict MyPy | PASS — 46 source files |
| Ruff | PASS |
| TypeScript | PASS |
| ESLint | PASS |
| Prettier | PASS |
| Vitest full suite | PASS — 72 passed |
| Playwright full configured suite | PASS — 58 passed, 22 conditional visual snapshots skipped |
| Next.js 16.3 production build under Node 24 | PASS |
| Memory threshold and maximum boundary | PASS |
| Owner/admin RBAC and recent reauthentication | PASS |
| Operator/auditor/ordinary-user denial | PASS |
| Admin reduction and no-increase rule | PASS |
| Pending/rejected/cancelled no-Worker/no-Slurm behavior | PASS |
| Joint GPU and memory approval, either decision order | PASS — one final Worker call |
| Worker immutable memory approval validation | PASS |
| CLI direct/high-memory input contract and JSON behavior | PASS |
| PostgreSQL 15 upgrade/downgrade/re-upgrade | PASS |
| Alembic single head | PASS — `f5a6b7c8d9e0` |
| Relocation A | PASS — `/login` HTTP 200 |
| Relocation B | PASS — `/login` HTTP 200 |
| Production installer rehearsal | PASS — candidate `/login` HTTP 200 |
| Production Web rollback rehearsal | PASS — prior Web `/login` HTTP 200 |
| Final production read-only drift check | PASS — authoritative baseline unchanged |

Relocation A and B used two unrelated random paths. Each archive was mounted by
itself into a network-disabled Node 24 container, read-only, as UID/GID 65534,
with all capabilities dropped and `no-new-privileges`. The build repository was
absent. A binary scan found neither the real build root nor `/src/portal`.

## Immutable artifacts

WEB ARTIFACT:
`/home/origin-al/Code/H100RemoteSSH/portal5a-user-job-memory-approval-81baffd-web.tar.gz`

WEB ARTIFACT SHA-256:
`f6bb3eb85f64c4efa90c581efc0d546c4983eaa5dd516f926174cb562530f964`

MANIFEST:
`/home/origin-al/Code/H100RemoteSSH/portal5a-user-job-memory-approval-81baffd-web.manifest.json`

MANIFEST SHA-256:
`02bd1e52e5df58688dfafa54cb5ad92f54c8bba25f8d2c685d87e41a282857fa`

BUNDLE:
`/home/origin-al/Code/H100RemoteSSH/portal5a-user-job-memory-approval-81baffd.bundle`

BUNDLE SHA-256:
`0a1a011f19cfd5b7000a95a807dc12ed4e1a0c03c555671f67243b7f2d6af662`

The bundle verifies successfully, advertises only candidate
`81baffd2e0d029d00c36072d6c4bb66a37f69e07`, and requires production parent
`afaca8cdd379e3051edeac682b16e6cbdaa59557`.

ARTIFACT VERSION:
`1`

WEB BUILD ID:
`5zLWuQkUmjlfxeuN88Iop`

BUILD TIMESTAMP:
`2026-09-07T12:21:31.278379+00:00`

FILE COUNT:
`2247`

ARCHIVE MEMBER COUNT:
`2789`

WEB ENTRYPOINT:
`.next/standalone/apps/web/server.js`

ABSOLUTE SYMLINKS:
`0`

ESCAPING SYMLINKS:
`0`

ABSOLUTE HARDLINKS:
`0`

ESCAPING HARDLINKS:
`0`

EXTERNAL RUNTIME DEPENDENCIES:
`0`

BUILD ROOT REQUIRED:
`NO`

## Final production read-only state

MANAGEMENT AGENT:
`PASS`

MANAGEMENT USER:
`codexops`

ROOT SSH USED:
`NO`

PRODUCTION GIT:
`afaca8cdd379e3051edeac682b16e6cbdaa59557`

PRODUCTION TREE:
`39c1587ee07f94d1bed92bf4fd87699a0e5f2902`

DATABASE:
`e4f5a6b7c8d9`

API / WORKER / DATABASE:
`READY`

WEB:
`HTTP 200`

FAILED SYSTEMD UNITS:
`0`

SLURM QUEUE:
`0`

GPU PROCESSES:
`0`

NEXT:
An independently authorized production preflight and deployment for candidate
`81baffd2e0d029d00c36072d6c4bb66a37f69e07`.
