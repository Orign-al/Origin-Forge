# PORTAL-5A Full Platform Release Notes

Release: `PORTAL-5A-FULL-PLATFORM-RELEASE`
Status: `PASS`
Acceptance date: 2026-08-30 UTC

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
- `--gpus 2` is rejected with HTTP 422 before Worker or Slurm creation, matching Web policy.
- `h100 job logs --follow` polls the owner-only Portal endpoint; Ctrl-C stops only the local client.
- Stable JSON and exit-status contracts support shell, Notebook, and VS Code automation.
- Compatibility aliases are `h100 sbatch`, `h100 squeue`, and `h100 scancel`; bare Slurm commands are not replaced.
- A root-owned `0555` CLI is mounted read-only into new containers. Existing running containers receive a hash-verified atomic root-owned copy without restart; stopped containers are updated when they next run.

See `portal/docs/h100-cli-contract.md` and the CLI Job Submission chapter in both user manuals.

## Production identity

- Git: `a707fce314ebe905e37c486d3f94d98b03b0130c`.
- Tree: `8a3b355c87182bee4451a0561a1cce5d7579881d`.
- Database: `b8c9d0e1f2a3`.
- Web build: `Sn5cBlkzuS_kjv8imum7G`.
- User entry point: <http://20.10.10.3:18080/>.

## Released capabilities

- Ordinary-user password setup, sign-in, session security, SSH key enrollment, compute request, activation, container, terminal, storage, Job, log, result, and lifecycle flows.
- CPU Development remains the default, GPU-less profile.
- Explicit `GPU_1_8CPU_32GB` profile with `MaxGPU=1`.
- GPU-decoupled development model: resident containers remain GPU-less; H100 resources are scheduled only for formal GPU Jobs and released automatically.
- Exact single-H100 allocation enforcement through Slurm, Pyxis, and device cgroups.
- GPU=2 rejection before Worker or Slurm creation and per-user total allocation limited to one.
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

## Acceptance highlights

- Dedicated user: `portal-storage-acceptance-b`, role `user`.
- Real ordinary-user Session; delegated auth was not used for the main flow.
- CPU zero-copy Job 148: completed.
- GPU zero-copy Job 149: completed on one NVIDIA H100 PCIe.
- Assigned UUID: `GPU-c8377945-df2c-5761-8798-66385611808b`.
- Other GPU access: denied.
- GPU=2: rejected with no downstream resource creation.
- Per-user GPU total: never exceeded one.
- Home and Workspace bidirectional zero-copy result visibility: passed.
- Cross-user Home and log access: denied.
- Final state: acceptance container stopped, no active acceptance Job, no GPU allocation, persistent data preserved.

## Validation

- Current production backend: 379 passed, 1 warning.
- Bash syntax, ShellCheck, Ruff check, and Ruff format: PASS.
- Home and Workspace alias namespace rehearsals: PASS.
- Worker script integrity: 17/17.
- Existing Web gates remain valid: Vitest 53 passed; Playwright 74 passed, 0 skipped, 0 failed.
- API, Worker, Web, timers, EasyTier ingress, Slurm, GPU release, and rollback manifest: PASS.

## Operational notes

- A first cold import of a large CUDA/Enroot image can exceed a very small memory request. A 512 MiB acceptance Job reached `OUT_OF_MEMORY`; the formal retry with 8 GiB completed. The failed Job remains visible as immutable history.
- Stopping a development container preserves data and does not equal Lease expiry/recycle.
- GPU availability is scheduler-controlled; a second GPU Job for the same user can remain Pending with `QOSMaxGRESPerUser` until the first releases its allocation.

## Deployment and rollback

The release was deployed from an immutable candidate after production preflight and namespace rehearsals.

- Primary rollback: `/srv/gpu-platform/backups/portal5a-home-alias-pre-a707fce-20260830T144050Z`.
- OCI rollback: `/srv/gpu-platform/backups/portal5a-local-image-pre-a707fce-20260830T150000Z`.
- Final SHA-256 rollback manifest: PASS.

## Deferred optional enhancement

TLS/HTTPS is not a release gate. The supported user endpoint remains HTTP inside EasyTier. No certificate or public management-interface exposure is part of this release.
