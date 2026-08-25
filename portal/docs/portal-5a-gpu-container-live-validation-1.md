# PORTAL-5A-GPU-CONTAINER-LIVE-VALIDATION-1

Historical record: this 2026-08-23 run stopped against an older CPU-only
production release. It does not describe the current candidate and is retained
only as prior fail-closed evidence. A new live report is required after the
immutable candidate is deployed.

Final result: **FAIL_CLOSED**

Date: 2026-08-23 UTC

Scope: controlled production live acceptance only; no implementation or
deployment

## Decision

Live execution stopped at the installed-release preflight. The production
Portal is reachable, but its deployed compute-request bundle is the CPU-only
release and does not expose the reviewed `GPU_1_8CPU_32GB` profile. Continuing
would either test the wrong release or require a production deployment, which
this phase explicitly prohibits.

No H100 was allocated or queried. No production container, Slurm job, user,
Lease, database row, workspace file, configuration, or deployed code was
created, modified, recycled, restored, or removed.

## Authorization recorded

The operator explicitly authorized this phase to:

- consume up to two production H100 GPUs concurrently for `origin-pilot` and
  `origin-pilot2`;
- create the two controlled GPU Development containers through Portal APIs;
- submit the scoped Slurm validation jobs; and
- perform Portal-managed recycle/restore lifecycle operations.

The authorization did not permit deployment, manual user/Lease/database
changes, direct Docker or Slurm mutation, or scheduler bypass. Those limits
were preserved.

## Read-only preflight evidence

| Check                          | Observation                                                                                                                                        | Result                            |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| Production Portal reachability | `GET /login` returned HTTP 200 from the documented private Portal endpoint                                                                         | PASS                              |
| API authentication boundary    | Unauthenticated `GET /api/v1/self/environment` returned HTTP 401                                                                                   | PASS                              |
| Deployed compute profile       | Public compute-request JavaScript submits `requested_container_profile:"STANDARD_8CPU_32GB"` as a fixed value                                      | CPU-ONLY RELEASE                  |
| Prior CPU workflow marker      | The deployed bundle contains the prior “GPU-free development container” staged workflow                                                            | CPU-ONLY RELEASE                  |
| Candidate GPU profile marker   | `GPU_1_8CPU_32GB` and the reviewed CPU/GPU profile selector are absent from every JavaScript chunk referenced by the deployed compute-request page | NOT DEPLOYED                      |
| Reviewed local candidate       | Baseline `febf53b16cc960e46c6d75091ffc5ff389fef7a8` plus uncommitted workspace/GPU changes                                                         | NOT AN IMMUTABLE DEPLOYED RELEASE |
| Host shell                     | No authenticated production SSH session was established or used                                                                                    | NO HOST ACTION                    |

The public UI evidence is sufficient to reject the release gate. Even if an
API component had been updated independently, a CPU-only Web component would
make the production release partial and unsuitable for acceptance.

## Live test matrix

| Test                                  | Result  | Reason                                                                                                                                                                                           |
| ------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1. GPU Development container creation | NOT RUN | The installed Portal does not expose the reviewed GPU Development profile. Creating it would require deployment or an unreviewed/manual path.                                                    |
| 2. Concurrent GPU isolation           | NOT RUN | Test 1 did not pass the installed-release gate; no GPU allocation was created.                                                                                                                   |
| 3. Workspace mount and Slurm output   | NOT RUN | The unified-workspace candidate is not proven installed; writing production workspace evidence would test the wrong release.                                                                     |
| 4. Security rejection boundary        | NOT RUN | No authenticated state-changing request was attempted after the release mismatch. The unauthenticated API probe correctly returned 401 but does not substitute for the requested negative tests. |
| 5. Lease recycle/restore              | NOT RUN | No validation container or allocation existed, and lifecycle changes against the old release were outside the authorized candidate test.                                                         |

No unexecuted item is represented as a pass.

## Required evidence disposition

| Evidence                  | Value                                    |
| ------------------------- | ---------------------------------------- |
| Container ID              | Not created                              |
| Slurm allocation/job ID   | Not created                              |
| GPU UUID                  | Not allocated or queried                 |
| CUDA environment          | Not observed                             |
| NVIDIA environment        | Not observed                             |
| cgroup/device restriction | Not observed                             |
| Workspace path/inode      | Not modified or observed live            |
| Cross-user isolation      | Not executed                             |
| Lease cleanup             | Not required; no lifecycle operation ran |

## Stop condition

The installed validation target does not contain the candidate under test.
Because this phase cannot deploy code, it cannot produce valid GPU isolation,
workspace, security-boundary, or lifecycle evidence. The safe action was to
stop before resource allocation.

This is not evidence of a security bypass. It is a release-readiness mismatch
that prevents live acceptance.

## Gate for a future live run

1. Commit and independently review an immutable workspace/GPU release.
2. Complete the blocked Python 3.14 and Node 24 automated suites from
   `PORTAL-5A-GPU-CONTAINER-ACCEPTANCE-1`.
3. Obtain separate production deployment approval and deploy the exact reviewed
   release through the normal procedure.
4. Prove Web, API, Worker, migration, protected scripts, Slurm `gpu-dev`
   partition/epilog, and workspace mount contract all match that release.
5. Reconfirm the bounded H100 live-test authorization, then rerun Tests 1–5
   from a clean preflight and capture the required IDs, UUIDs, environment,
   cgroup, workspace, isolation, lifecycle, and cleanup evidence.

No production rollout is permitted until a future run completes every live
test successfully.

**FAIL_CLOSED**
