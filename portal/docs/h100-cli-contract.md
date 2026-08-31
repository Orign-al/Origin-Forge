# `h100` ordinary-user CLI contract

Release: `PORTAL-5A-USER-CLI-JOBS-1`
CLI version: `1.0.0`
JSON schema: `h100.cli.v1`

## Scope and trust boundary

`h100` is the supported Job client inside a managed Development Container, including Container SSH and the Web Terminal. It is an ordinary-user Portal client, not a Slurm client.

```text
ordinary user h100 CLI
  -> platform-managed per-container endpoint
  -> private bridge-only CLI ingress (:18082)
  -> Portal API (127.0.0.1:18081)
  -> Portal /api/v1/self/*
  -> owner, Lease, quota, and policy validation
  -> root-owned Worker socket (not exposed to the container)
  -> Slurm
```

The CLI never connects to `slurmctld`, `slurmrestd`, the Worker socket, a database, a Docker socket, host SSH, or MUNGE. It contains no platform credential or delegated administrator identity. The Web UI and CLI use the same `/self/jobs` records and authoritative Slurm projection; there is no parallel `/cli/jobs` backend.

## Installation and versioning

- Host artifact: `/usr/local/lib/h100-platform/h100-cli`.
- Container path: `/usr/local/bin/h100`.
- Owner/mode: `root:root`, `0555`.
- New and rebuilt containers receive a Compose read-only bind mount.
- Existing running containers receive an atomic `root:root` mode `0555` copy through `h100-cli-rollout`; the rollout verifies the staged and installed hashes and does not restart or rebuild them. This is the hot-update path because a host-only source cannot be safely live-bound into an already-created private mount namespace without pre-existing propagation.
- The same rollout writes `/etc/h100/cli.json` as `root:root 0444`. Its endpoint is generated from the container's validated Docker network gateway rather than a compiled-in address. Stopped containers are updated through `docker cp` without starting or recreating them, and the copied bytes and mode are verified.
- The source, rollout helper, and container-create helper are SHA-256 bound in `portal/deploy/worker-scripts.json`.

`h100 --version` returns the installed semantic CLI version. The executable is Python 3.10+ compatible and uses only the standard library.

## Authentication

Before sending an Authorization header, every CLI operation fetches `/api/v1/cli/identity` without credentials and requires service `H100 Portal`, API compatibility `h100.cli.v1`, and CLI version `1.0.0`. Redirects are not followed. A missing, user-owned, symlinked, or non-`0444` platform endpoint configuration fails closed. Normal users neither select nor configure an endpoint.

The private ingress binds only the real gateway addresses of Docker bridge networks belonging to `h100.dev.user` containers. It does not bind `0.0.0.0`, the EasyTier addresses, or the physical management address. It forwards only the CLI identity route and the method/path allowlist needed by `/self/cli-auth` and `/self/jobs`; admin, internal, Worker, debug, docs, OpenAPI, metrics, and storage routes are not exposed. It has no access log and never forwards cookies or proxy headers.

An ordinary user creates a Personal CLI Token under **Account Security → CLI Tokens** after recent password reauthentication.

- Credential format: public discriminator `h100_cli_` plus 64 URL-safe random characters (48 random bytes before URL-safe base64 encoding).
- Server persistence: SHA-256 digest only; plaintext is returned only by the successful create response.
- Owner: exact ordinary role set `user`; administrator and mixed-role accounts cannot create or use one.
- Scope: `self.jobs.submit`, `self.jobs.read`, and `self.jobs.cancel` only.
- Metadata: label, created time, optional absolute expiry, last-used time, and revocation time.
- Server-side revocation is immediate.
- Password change/reset and login lock revoke all active CLI Tokens. An expired, revoked, inactive, non-ordinary, or non-`SET` password account is rejected.
- Bearer credentials are accepted only in the `Authorization` header. A `token` query parameter is rejected globally.
- API responses use `Cache-Control: no-store`; request/exception logging does not log headers or bodies.
- Raw CLI Token patterns and token-named metadata are redacted from audit metadata.
- The API never includes the token in Worker payloads, Job scripts, Job environment, Slurm arguments, or diagnostics.

Login is interactive and does not echo the token:

```bash
h100 auth login
h100 auth login --token-stdin
```

There is intentionally no `h100 auth login --token TOKEN` option. The validated credential is atomically stored at `~/.config/h100/credentials`, with parent mode `0700` and file mode `0600`; symlink and ownership checks fail closed. `H100_CONFIG_HOME` is supported for controlled testing/automation and must be absolute.

```bash
h100 auth status [--json]
h100 auth logout [--json]
```

Logout removes only the local credential file. Portal revocation is required to invalidate a token everywhere. HTTP redirects are never followed while a Bearer credential is present.

## Commands

### Submit

```text
h100 job submit SCRIPT [--name NAME] [--cpus N] [--memory SIZE]
                         [--gpus 0|1] [--time [D-]HH:MM:SS] [--json]
```

- `SCRIPT` can be relative or absolute. The CLI resolves symlinks and requires the final regular path to be inside `/workspace` or `/home/<authenticated-unix-user>`.
- The backend independently accepts only a canonical absolute `source_path` under the same owner-bound roots.
- The CLI reads at most the Portal-advertised UTF-8 script limit. The backend and Worker independently validate content and hash.
- The Portal sends an immutable, server-generated `.portal/job-scripts/<portal-job-id>.sh` snapshot to Worker. Slurm never reads a mutable client-selected script path.
- Job working directory is the original script's parent under the owner Workspace or Home mount.
- `/etc`, `/root`, `/proc`, `/sys`, other users' Home, traversal, non-canonical paths, and arbitrary host paths are rejected before Worker.
- CLI v1 rejects every line whose left-trimmed form begins with `#SBATCH`. Resource-changing directives are not forwarded to Slurm.
- Omitted resource values come from `GET /self/jobs/config`; the CLI does not own default resource policy.
- API and Worker both enforce CPU, memory, time, Lease, approved image, account/QoS, and GPU policy.
- `gpu_count=2` reaches the API schema but is rejected by the route with HTTP 422 before any Worker call or Slurm Job.

Human success output reports the distinct Portal and Slurm IDs, state, GPU count, and source script. It never assumes those IDs are equal.

### List

```text
h100 job list [--limit 1..200] [--state STATE] [--json]
```

The human table contains full Portal Job ID, name, authoritative state, GPU, CPU, memory, submitted time, and elapsed time. Only the current owner's rows are queried. Non-terminal rows must refresh successfully from Slurm; an unavailable authoritative projection fails closed instead of presenting stale state. Persisted terminal history remains readable.

### Status

```text
h100 job status JOB_ID [--json]
```

`JOB_ID` can be a Portal UUID or an owned numeric Slurm ID. Numeric lookup is performed through the owner's Portal list; the CLI never queries Slurm. Status reports Portal ID, Slurm ID, name, state, reason, CPU, memory, GPU, submitted/started/finished timestamps, elapsed seconds in JSON, and exit code.

### Logs

```text
h100 job logs JOB_ID [--stderr | --all] [--tail N] [--follow] [--json]
```

The default stream is stdout. Follow mode polls the owner-only Portal logs endpoint every two seconds and exits after an authoritative terminal state. It never opens host SSH or tails a host file. Ctrl-C returns success from the local log client and never calls cancel.

### Cancel

```text
h100 job cancel JOB_ID [--wait] [--json]
```

Cancel calls the existing Portal cancel endpoint. The API owner check and delegated-session binding check run before authoritative status or Worker access. Only a non-terminal owned Job can be cancelled. Worker performs the controlled internal `scancel`, then projects Slurm state; if the immediate projection is unavailable, the response remains `CANCELLATION_REQUESTED` with `authoritative=false` and never fabricates `CANCELLED`. `--wait` polls Portal detail until a terminal state.

### Compatibility aliases

```text
h100 sbatch SCRIPT ...   == h100 job submit SCRIPT ...
h100 squeue ...          == h100 job list ...
h100 scancel JOB_ID ...  == h100 job cancel JOB_ID ...
```

The release does not place bare `sbatch`, `squeue`, or `scancel` replacements in `PATH`.

## JSON contract

Every successful object contains:

```json
{ "schema_version": "h100.cli.v1", "command": "job.status", "ok": true }
```

Command payloads are additive within `h100.cli.v1`:

- `auth.login`: `authenticated`, `portal_url`, `user`.
- `auth.status`: `authenticated`, `portal_url`, `user`, `credential`.
- `auth.logout`: `credential_removed`.
- `job.submit`: `job`.
- `job.list`: `count`, `jobs`.
- `job.status`: `job`.
- `job.logs`: `job_id`, `stream`, `sequence`, `stdout`, `stderr`, `selected`, `terminal` on the initial object; follow deltas retain `job_id`, `stream`, `sequence`, `selected`, and `terminal`.
- `job.cancel`: `cancellation_requested`, `job`.

Normal commands emit exactly one compact JSON object to stdout. `job logs --follow --json` emits one JSON object per line. In JSON mode stdout contains JSON/JSON Lines only; human diagnostics go to stderr.

Errors use:

```json
{
  "schema_version": "h100.cli.v1",
  "ok": false,
  "error": {
    "code": "GPU_LIMIT_EXCEEDED",
    "message": "GPU request must be 0 or 1",
    "http_status": 422
  }
}
```

`http_status` is present for an HTTP/API error. Error codes and fields can be added without changing the schema version; existing field meaning is stable for the `h100.cli.v1` lifetime.

## Exit status

| Code | Meaning                                                             |
| ---: | ------------------------------------------------------------------- |
|    0 | Success, including Ctrl-C from log follow                           |
|    1 | Generic client, transport, Portal, or server failure                |
|    2 | Usage or invalid CLI argument/local script                          |
|    3 | Authentication required or credential rejected                      |
|    4 | Authorization denied or resource not found                          |
|    5 | Policy, quota, conflict, or validation rejection (HTTP 409/422/429) |

Cross-owner Job access deliberately maps to the same resource-not-found behavior as an absent Job and does not disclose ownership.

## State contract

Recognized terminal states are `BOOT_FAIL`, `CANCELLED`, `COMPLETED`, `DEADLINE`, `FAILED`, `NODE_FAIL`, `OUT_OF_MEMORY`, `PREEMPTED`, `REVOKED`, and `TIMEOUT`. Follow/wait terminate on any of these states. Portal persists reason, start, finish, elapsed seconds, and exit code from Worker's live `scontrol` or terminal `sacct` projection.

## Security invariants

CLI deployment does not change these release properties:

- Development Containers have no GPU device; GPUs are Slurm Job only.
- Maximum GPU allocation per ordinary user remains one.
- GPU=2 is rejected before Worker.
- Containers remain unprivileged, without Docker socket, MUNGE, Worker socket, host network/PID/IPC, or `SYS_ADMIN`.
- Host SSH remains unavailable to ordinary users.
- Job, log, and storage operations remain owner-bound.
- `/workspace` and `/home/<username>` remain zero-copy persistent paths shared by Development Container, CPU Job, and GPU Job.
