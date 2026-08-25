# PORTAL-5A-GPU-CONTAINER-ACCEPTANCE-1

Historical record: this 2026-08-23 fail-closed review predates the current
Python 3.14/Node 24 candidate gates. See `workspace-gpu-test-report.md` for the
current candidate result. It is retained as evidence and is not a current
deployment decision.

Final status: **FAIL_CLOSED**

Date: 2026-08-23 UTC

Repository baseline: `febf53b16cc960e46c6d75091ffc5ff389fef7a8`

Scope: code review, test completion, and deployment-readiness validation only

## Decision

Production deployment is not approved. The candidate contains the intended
fail-closed workspace and GPU Development controls, and the static checks that
could run in this host passed. Acceptance nevertheless cannot pass because the
required live Docker/Slurm/H100 isolation tests were not authorized and the
host does not provide the repository's pinned Python 3.14 and Node 24 test
runtimes.

This is an evidence failure, not a discovered GPU-isolation bypass or
cross-user access. Per the acceptance stop conditions, lack of proof is enough
to produce `FAIL_CLOSED`.

No production deployment, database migration, Docker operation, Slurm
operation, GPU query, GPU allocation, production job, user creation, or manual
database change was performed during this phase.

## Candidate identity

| Item                | Recorded value                                                                               |
| ------------------- | -------------------------------------------------------------------------------------------- |
| Branch              | `master`, 20 commits ahead of `origin/master` at review time                                 |
| Baseline commit     | `febf53b16cc960e46c6d75091ffc5ff389fef7a8`                                                   |
| Candidate state     | Uncommitted workspace/GPU changes in the review worktree                                     |
| Production state    | Not inspected or changed by this acceptance phase                                            |
| Release eligibility | Blocked until the candidate is committed, independently reviewed, and all blocked gates pass |

The baseline value identifies the repository reviewed here; it is not evidence
that the same revision is installed in production.

## Result legend

- **PASS**: executed locally with passing evidence and no H100 consumption.
- **STATIC PASS / LIVE BLOCKED**: source/configuration controls were reviewed,
  but the required production-equivalent behavior was not executed.
- **BLOCKED**: the required suite or observation could not run in the available
  environment.

## Acceptance matrix

| Test                            | Result                     | Evidence and remaining gate                                                                                                                                                                                                                                                                                                |
| ------------------------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. CPU Development regression   | STATIC PASS / LIVE BLOCKED | CPU remains the default `STANDARD_8CPU_32GB` profile with GPU count `0`. Worker and lifecycle postconditions reject NVIDIA runtime, NVIDIA/CUDA visibility variables, DeviceRequests, and direct devices for CPU/stopped definitions. A live CPU-container `nvidia-smi` negative test is still required.                   |
| 2. GPU Development container    | STATIC PASS / LIVE BLOCKED | `GPU_1_8CPU_32GB` requires exactly one GPU and the owner entitlement must equal `1`; other GPU counts are rejected before Worker execution. The runtime definition contains one UUID-specific NVIDIA DeviceRequest and visibility variables. Container start and one-H100 visibility were not executed.                    |
| 3. GPU identity binding         | STATIC PASS / LIVE BLOCKED | Worker verifies a RUNNING `gpu-dev` Slurm allocation, owner, account, QOS, creating-Lease comment, and exactly one GPU index, maps that index to a physical UUID, and supplies only that UUID to Docker. No `--gpus all` path was found. Runtime UUID equality still requires an approved inspect plus in-container probe. |
| 4. Workspace mount contract     | STATIC PASS / LIVE BLOCKED | The canonical path is derived from UID as `/storage/users/{uid}` and mounted once at `/workspace`; job `WORKSPACE` and `--chdir` use the same root. Source validates a real directory owned by exact UID/GID with mode `0700`. Cross-environment file/inode behavior was not executed.                                     |
| 5. Output persistence           | STATIC PASS / LIVE BLOCKED | Job stdout/stderr and application output resolve below the same canonical workspace that is mounted at `/workspace`; no synchronization path was introduced. A Slurm write followed by a container read was not executed.                                                                                                  |
| 6. Multi-user storage isolation | STATIC PASS / LIVE BLOCKED | Workspace selection ignores username and derives only from the managed UID. Schema and Worker validation reject a mismatched path, symlink escape, wrong owner/group, or wrong mode. Kernel-level denial probes between UID 20001 and UID 20002 were not executed.                                                         |
| 7. Multi-user GPU isolation     | STATIC PASS / LIVE BLOCKED | Each container requires a different live owner-bound Slurm allocation and an exact allocation UUID; one UUID-specific DeviceRequest is permitted. Concurrent containers and reciprocal `nvidia-smi` exclusion were not executed.                                                                                           |
| 8. Lease lifecycle              | STATIC PASS / LIVE BLOCKED | Recycle removes the container before cancelling its allocation and preserves workspace state. Restore uses the same canonical workspace and obtains a new allocation. If removal is not proven, allocation release is withheld. Live recycle/restore and inode persistence were not executed.                              |
| 9. Security regression          | STATIC PASS / LIVE BLOCKED | Worker/runtime postconditions reject privileged mode, Docker socket and MUNGE mounts, host PID/network, direct devices, capabilities, host-root mounts, extra cgroup rules, and GPU count above `1`. Runtime negative probes were not executed.                                                                            |
| 10. Automated suite             | BLOCKED                    | Ruff, formatting, shell syntax, TypeScript, ESLint, diff whitespace, runtime-install contract, and protected-script hash checks passed. Full pytest/API/Worker, mypy, Vitest, Playwright, and Next production build did not complete in the available pinned runtimes; details follow.                                     |

No test that requires a Docker container, Slurm allocation/job, H100 query, or
production identity was simulated and reported as a live pass.

## Failures and blockers

No locally executed check reported an implementation failure. Acceptance is
still failed closed for two independent reasons:

1. Production-equivalent GPU, cgroup, workspace, cross-user, and lifecycle
   behavior was not run because H100/Docker/Slurm use requires explicit
   approval.
2. The host's Python 3.12 and Node 18 runtimes cannot execute all suites pinned
   for Python 3.14 and Node 24.

Either reason is sufficient to prevent `ACCEPTANCE PASSED`. A later live test
failure must be treated as a security failure, not waived as an environment
blocker.

## Automated evidence

### Passed

| Command/check                                                              | Result        |
| -------------------------------------------------------------------------- | ------------- |
| `ruff check .`                                                             | PASS          |
| `ruff format --check .`                                                    | PASS          |
| `bash -n` on the changed lifecycle/deployment shell scripts                | PASS          |
| `git diff --check`                                                         | PASS          |
| Web `tsc --noEmit`                                                         | PASS          |
| Web ESLint                                                                 | PASS          |
| `pytest -q tests/test_runtime_install_contract.py`                         | PASS, 8 tests |
| Protected script SHA-256 values versus `portal/deploy/worker-scripts.json` | PASS          |

### Blocked or incomplete

| Required gate                      | Reason                                                                                                                | Required completion evidence                                                                                                                                           |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Full Python/API/Worker pytest      | Repository requires Python `>=3.14,<3.15`; available Python is `3.12.3`, and the environment cannot import SQLAlchemy | Clean full-suite run with the locked test dependencies on Python 3.14                                                                                                  |
| mypy                               | SQLAlchemy/Pydantic plugins are unavailable in the local interpreter environment                                      | Clean strict mypy run on Python 3.14 with locked plugins                                                                                                               |
| Vitest                             | Available Node `18.19.1` lacks `node:util.styleText` required by the installed frontend toolchain                     | Clean Vitest run on the pinned Node 24 environment                                                                                                                     |
| Playwright                         | The installed Playwright/Next toolchain requires a newer Node runtime                                                 | Clean Playwright run on Node 24 with the reviewed browser dependencies                                                                                                 |
| Next production build              | Next requires Node `>=20.9`; repository operations pin Node 24                                                        | Clean production build on Node 24                                                                                                                                      |
| Alembic upgrade/downgrade exercise | Deliberately not run against any production database; compatible local test dependencies are unavailable              | Upgrade and downgrade rehearsal against a disposable production-equivalent backup/fixture                                                                              |
| Live Tests 1–9                     | Explicit approval to consume H100/Slurm/Docker resources was not provided                                             | Timestamped commands, scheduler/container inspection, UUIDs, cgroup evidence, file identity/ownership evidence, and cleanup proof from the approved maintenance window |

`pnpm` is also absent locally. No alternate Python 3.14 or Node 24 runtime was
found under the reviewed local installation roots.

## Security review

| Control                     | Reviewed enforcement                                                                                                                                    | Result                                                     |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| Scheduler authority         | GPU Development creates an owner UID/GID Slurm allocation in non-default `gpu-dev`; owner/account/QOS/partition/Lease and RUNNING state are revalidated | STATIC PASS                                                |
| Maximum GPU                 | API/database/Worker profile contracts require exactly `1`; GPU `2`, `3`, and `4` cannot form a valid request                                            | STATIC PASS                                                |
| Physical device binding     | Slurm index is mapped to one UUID; Docker accepts one `Driver=nvidia`, `DeviceIDs=[UUID]`, `Capabilities=[[gpu]]` request                               | STATIC PASS                                                |
| Visibility defense in depth | Exact `NVIDIA_VISIBLE_DEVICES=GPU-...` and `CUDA_VISIBLE_DEVICES=0`; `all` is rejected                                                                  | STATIC PASS                                                |
| Device cgroup               | No direct `HostConfig.Devices` or extra device cgroup rule is accepted; the NVIDIA DeviceRequest is UUID-specific                                       | STATIC PASS / LIVE BLOCKED                                 |
| Root Worker boundary        | API sends typed payloads to fixed Worker handlers; executable paths are allowlisted and protected scripts are hash-pinned                               | PASS for reviewed source/hash; runtime attestation blocked |
| User execution identity     | Slurm submissions use `setpriv --reuid/--regid --clear-groups` with capability clearing and fixed argv; subprocess calls use `shell=False`              | STATIC PASS                                                |
| Workspace owner binding     | Canonical UID path, real-directory requirement, exact UID/GID and `0700`, and descriptor-relative no-follow access                                      | STATIC PASS / LIVE BLOCKED                                 |
| Container escape controls   | Privileged, host network/PID, Docker socket, MUNGE, host root, unapproved mounts, capabilities, and direct devices are rejected                         | STATIC PASS / LIVE BLOCKED                                 |
| Lease binding               | Activation/start/restart/recycle/restore carry and validate owner/Lease/allocation coordinates                                                          | STATIC PASS / LIVE BLOCKED                                 |
| Allocation release safety   | Container removal and GPU-less postcondition precede cancellation; teardown failure retains allocation and the epilog drains the node                   | STATIC PASS / LIVE BLOCKED                                 |
| Existing Portal controls    | RBAC, CSRF, owner scoping, account/QOS limits, and max-GPU validation remain on the reviewed request paths                                              | STATIC PASS; full regression suite blocked                 |

No privileged-container requirement, Docker-socket mount, host-root mount,
`--gpus all`, root execution of a user script, or alternate unscheduled GPU path
was found in the candidate review.

## Stop-condition assessment

| Stop condition                   | Finding                                                                                                                                     |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| GPU isolation cannot be proven   | **Triggered.** Static design is coherent, but physical/cgroup and concurrent-user behavior lack live proof. Acceptance stopped fail-closed. |
| Workspace ownership is ambiguous | Not found in source; the contract is UID-derived and requires exact UID/GID plus `0700`. Live filesystem state remains unverified.          |
| Cross-user access exists         | Not observed. Live reciprocal-denial evidence is still required.                                                                            |
| Privileged mode is required      | Not found; it is explicitly rejected.                                                                                                       |
| Host filesystem exposure exists  | No prohibited mount path was found in the reviewed contract; live mount inspection remains required.                                        |
| Slurm allocation is bypassed     | No bypass path was found; GPU start requires a validated allocation. Live scheduler/runtime identity proof remains required.                |

## Known limitations

- This acceptance did not observe installed production state, actual
  `/storage/users` ownership, XFS project quota mappings, Docker runtime state,
  Slurm configuration, cgroup devices, NVIDIA UUIDs, or active Lease cleanup.
- The current candidate is an uncommitted worktree, not an immutable release
  artifact. Its hashes and test evidence must be regenerated after commit.
- GPU Development depends on the dedicated `gpu-dev` partition and the
  integrity-pinned epilog being deployed together. Partial rollout is not
  supported.
- A user can cancel their Slurm allocation; the epilog is therefore a required
  safety mechanism, not optional hardening. A teardown failure intentionally
  drains the node and can reduce available capacity.
- The Portal reports workspace usage but does not currently provide a file
  browser.
- Bare Slurm jobs use `$WORKSPACE/outputs/result.txt` (or a relative path from
  the default `projects` workdir), not an absolute `/workspace` alias.
  Containerized Slurm jobs additionally receive `/workspace`. The implemented
  directory is `outputs` (plural), matching the workspace contract; the test
  plan's literal `/workspace/output/result.txt` path must be normalized when
  live evidence is collected.
- Restore preserves the workspace but may receive a different scheduled GPU
  UUID; GPU identity is not a persistent user entitlement.
- Environment variables aid usability and defense in depth. Acceptance of
  physical isolation depends on the UUID-specific NVIDIA DeviceRequest and
  observed cgroup/runtime state, which are still untested live.

## Gates required to change this decision

1. Commit and independently review an immutable candidate revision.
2. Run all blocked Python and frontend suites in the pinned Python 3.14 and
   Node 24 environment, including migration rehearsal, with zero failures.
3. Obtain explicit approval for a maintenance-window Docker/Slurm/H100
   acceptance run.
4. Execute Tests 1–9 using UID/GID 20001 and 20002, capture assigned GPU UUIDs,
   container DeviceRequests/env/mounts/namespaces/cgroup evidence, reciprocal
   storage denials, same-workspace file evidence, and recycle/restore cleanup.
5. Confirm that all test containers/jobs and allocation coordinates are
   removed without residual GPU bindings.

Until every gate passes, the only valid release decision is:

**FAIL_CLOSED**

## Related release documents

- [Workspace mount contract](portal-5a-workspace-mount-contract.md)
- [GPU Development profile](gpu-development-container-profile.md)
- [Migration plan](workspace-gpu-migration-plan.md)
- [Combined deployment and rollback plan](workspace-gpu-deployment-and-rollback.md)
- [Implementation test report](workspace-gpu-test-report.md)
- [Production deployment checklist](portal-5a-gpu-container-production-deployment-checklist.md)
- [Rollback procedure](portal-5a-gpu-container-rollback-procedure.md)
