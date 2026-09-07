# PORTAL-5A Full Platform Release Notes

Release: `PORTAL-5A-FULL-PLATFORM-RELEASE`
Status: `PASS`
Initial acceptance date: 2026-08-30 UTC
Current production deployment: 2026-09-07 UTC

## High-memory approval addendum

Scope: `PORTAL-5A-USER-JOB-MEMORY-APPROVAL-1`
Status: `DEPLOYED / PASS`

- Job memory through 32 GiB remains direct. Requests above 32 GiB require workload, memory-breakdown, and necessity fields and remain `APPROVAL_PENDING` without a Worker call or Slurm Job.
- The current single-node Slurm `RealMemory=486377 MiB` contract is the maximum accepted value at the Web, CLI, API, database, and Worker boundaries.
- Only platform owners and platform administrators can review after recent password reauthentication. Reviewers may lower memory but cannot exceed the user's request.
- Memory and multi-GPU approvals are independent. A combined request makes one Worker call and creates one Slurm Job only after both approvals pass; rejection of either dimension prevents submission.
- Approval narratives remain in Portal and are not forwarded to Worker, Slurm, or the Job environment.
- Production deployment preserved all existing container identities and images, caused zero container restarts, and retained the loopback-only API and private CLI ingress boundaries.

## Multi-GPU approval addendum

Scope: `PORTAL-5A-USER-MULTIGPU-APPROVAL-1`
Status: `DEPLOYED / PASS`

- GPU 0 or 1 remains a direct ordinary-user submission; GPU 2, 3, or 4 requires complete model, architecture, framework/version, parameter-count, workload, dataset, parallel-strategy, and scaling-justification fields.
- A complete multi-GPU request is persisted as `APPROVAL_PENDING` without calling Worker, creating a Slurm Job, allocating a GPU, or exposing runtime logs.
- Only `platform_owner` and `platform_admin` can review. They must recently reauthenticate and may approve 1 through the requested number of GPUs or reject with a comment; operators, auditors, and ordinary users cannot review.
- Worker requires an immutable approval contract and submits the exact approved count. Direct jobs use the existing one-GPU policy; approved 2-4 GPU jobs use a dedicated scheduling policy.
- Slurm association and QoS controls cap one Job and the user's aggregate concurrent allocation at four H100s. Unrelated Slurm users are excluded from the dedicated policy.
- Development containers remain GPU-less and unprivileged. GPU access remains limited to the exact Slurm Job allocation.

## Renewal, restore, and controlled-sudo addendum

Scope: `PORTAL-5A-RENEWAL-RESTORE-APPROVAL-1`
Status: `DEPLOYED / PASS`

- Platform owners and administrators can select an independent renewal approval policy for each user. The default requires review; automatic approval still enforces the renewal window, duration ceiling, Lease lock, duplicate-request checks, and resource policy.
- A restore request from Recycle Bin follows the same per-user policy. It is never implicitly approved merely because the original renewal request predates expiry.
- Renewal and restore decisions remain backend-authoritative, require recent reauthentication, and record actor, effective user, object, and result in the audit trail.
- Controlled passwordless sudo can be enabled per user. Enabling it for an existing user rebuilds only that user's container once while preserving workspace, home, Lease, SSH access, quota, image identity, and the GPU-less security boundary.
- Container sudo grants root only inside the container. It does not expose host root, Docker, MUNGE, the Worker socket, host namespaces, or persistent GPUs.

## Ordinary-user CLI Job addendum

Addendum: `PORTAL-5A-USER-CLI-JOBS-1`
CLI: `h100 1.0.0`
JSON contract: `h100.cli.v1`

- Development Container SSH and Web Terminal users can submit, list, inspect, follow logs, and cancel their own Jobs without opening the Jobs page.
- `h100` reuses the released `/self/jobs` API, owner/Lease/quota validation, Worker, and Slurm path. No `/cli/jobs`, direct Slurm access, host credential, MUNGE, Docker socket, or Worker socket is exposed.
- Account Security now creates owner-bound ordinary-user CLI Tokens after password reauthentication. Only a SHA-256 token digest is stored; plaintext is shown once, can expire, records last use, and is revocable.
- Password changes/resets and login lock revoke CLI Tokens. Inactive, expired, revoked, non-ordinary, and wrong-scope credentials fail closed.
- CLI credentials are accepted only in the Bearer header; token query strings are rejected, redirects are not followed, and token plaintext is redacted from audit metadata and never sent to Worker, Slurm, or Job environments.
- CLI resource defaults are fetched from the Portal. Scripts resolve only under `/workspace` or the current user's `/home`; Portal continues to create an immutable script snapshot.
- CLI v1 rejects `#SBATCH` resource directives. CPU, memory, GPU, time, Lease, image, account, and QoS remain backend-authoritative.
- `--gpus 2` through `--gpus 4` use the same approval contract as Web. Incomplete requests are rejected before Worker or Slurm creation.
- `h100 job logs --follow` polls the owner-only Portal endpoint; Ctrl-C stops only the local client.
- Stable JSON and exit-status contracts support shell, Notebook, and VS Code automation.
- Compatibility aliases are `h100 sbatch`, `h100 squeue`, and `h100 scancel`; bare Slurm commands are not replaced.
- A root-owned `0555` CLI is mounted read-only into new containers. Existing running containers receive a hash-verified atomic root-owned copy without restart; stopped containers are updated when they next run.

See `portal/docs/h100-cli-contract.md` and the CLI Job Submission chapter in both user manuals.

## Production identity

- Git: `81baffd2e0d029d00c36072d6c4bb66a37f69e07`.
- Tree: `88d5575ec6672f6611d0b992c914c4210f34e41e`.
- Database: `f5a6b7c8d9e0`.
- Web build: `5zLWuQkUmjlfxeuN88Iop`.
- CLI: `h100 1.0.0`.
- User entry point: <http://20.10.10.3:18080/>.

## Released capabilities

- Ordinary-user password setup, sign-in, session security, SSH key enrollment, compute request, activation, container, terminal, storage, Job, log, result, and lifecycle flows.
- CPU Development remains the default, GPU-less profile.
- Explicit `GPU_1_8CPU_32GB` profile with `MaxGPU=1`.
- GPU-decoupled development model: resident containers remain GPU-less; H100 resources are scheduled only for formal GPU Jobs and released automatically.
- Exact approved H100 allocation enforcement through Slurm, Pyxis, and device cgroups.
- Direct single-H100 Jobs plus approval-gated 2-4 H100 Jobs, with a four-GPU aggregate user ceiling.
- Direct memory requests through 32 GiB plus approval-gated high-memory Jobs up to 486377 MiB.
- Per-user renewal and restore approval policy and per-user controlled container sudo.
- EasyTier Portal and managed container SSH without exposing the physical management interface.
- 96-hour Lease, recoverable recycle, preserved data, and owner restore.

## Unified private storage

- `/workspace` and `/home/<username>` are both persistent, owner-bound, read/write, zero-copy storage.
- Development containers, Slurm CPU Jobs, and Slurm GPU Jobs use the same paths and the same underlying data.
- Existing Home data remains in place; no migration, copy, rsync, or re-upload is needed.
- Slurm Jobs cannot request an arbitrary host path or mount another user's Home.
- The Portal storage scope includes both persistent paths.
- One 300 GiB XFS project quota covers the complete user storage root, with no bind-alias double counting.

## User-visible GPU behavior

`H100 / NOT ALLOCATED` on a running GPU Development container is expected. It means the persistent development container is not reserving an H100. Submit a Job with GPU=`1` to obtain one scheduler-assigned H100 for that Job.

The reliable visibility check inside a GPU Job is:

```bash
nvidia-smi -L
nvidia-smi --query-gpu=uuid,name,pci.bus_id --format=csv,noheader
```

The base image may retain a default `NVIDIA_VISIBLE_DEVICES` string; actual device access is enforced by the scheduler and device namespace.

## Initial acceptance highlights

- Dedicated user: `portal-storage-acceptance-b`, role `user`.
- Real ordinary-user Session; delegated auth was not used for the main flow.
- CPU zero-copy Job 148: completed.
- GPU zero-copy Job 149: completed on one NVIDIA H100 PCIe.
- Assigned UUID: `GPU-c8377945-df2c-5761-8798-66385611808b`.
- Other GPU access: denied.
- GPU=2 was rejected under the initial one-GPU release policy. The current release replaces that rule with backend-authoritative 2-4 GPU approval.
- Per-user GPU total did not exceed the policy active during that acceptance run.
- Home and Workspace bidirectional zero-copy result visibility: passed.
- Cross-user Home and log access: denied.
- Final state: acceptance container stopped, no active acceptance Job, no GPU allocation, persistent data preserved.

## Validation

- Current candidate backend/Worker/runtime suite: 445 passed, 1 warning.
- Affected Python suite after migration linearization: 220 passed.
- Bash syntax, ShellCheck, Ruff check, and Ruff format: PASS.
- Home and Workspace alias namespace rehearsals: PASS.
- Production Worker/runtime script integrity: 21/21.
- Current Web gates: Vitest 72 passed; Playwright 58 passed with 22 conditional visual snapshots skipped.
- PostgreSQL 15 upgrade/downgrade/re-upgrade and Alembic single-head check: PASS.
- Existing Development Container IDs and images unchanged; restart-count increases: 0.
- API, Worker, Web, timers, EasyTier ingress, Slurm, GPU release, and rollback manifest: PASS.

## Operational notes

- A first cold import of a large CUDA/Enroot image can exceed a very small memory request. A 512 MiB acceptance Job reached `OUT_OF_MEMORY`; the formal retry with 8 GiB completed. The failed Job remains visible as immutable history.
- Stopping a development container preserves data and does not equal Lease expiry/recycle.
- GPU availability is scheduler-controlled; a second GPU Job for the same user can remain Pending with `QOSMaxGRESPerUser` until the first releases its allocation.

## Deployment and rollback

The release was deployed from an immutable candidate after production preflight and namespace rehearsals.

- Current rollback: `/srv/gpu-platform/backups/portal5a-memory-approval-pre-afaca8c-myl0WqWw`.
- Final SHA-256 rollback manifest: PASS.

See `PORTAL-5A-USER-JOB-MEMORY-APPROVAL-PRODUCTION-DEPLOY-1.md` for the current production deployment evidence.

## Deferred optional enhancement

TLS/HTTPS is not a release gate. The supported user endpoint remains HTTP inside EasyTier. No certificate or public management-interface exposure is part of this release.
