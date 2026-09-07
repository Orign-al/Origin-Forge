# H100 GPU Platform User Manual

Version: `PORTAL-5A-FULL-PLATFORM-RELEASE + PORTAL-5A-USER-CLI-JOBS-1`
Production entry point: <http://20.10.10.3:18080/>
Audience: ordinary users

## 1. Platform overview

The H100 GPU Platform provides self-service development containers, private persistent storage, a web terminal, container SSH, and Slurm CPU/GPU jobs.

- `CPU Development / STANDARD_8CPU_32GB` is the default profile: 8 CPUs, 32 GiB memory, and no GPU device in the development container.
- `GPU Development / GPU_1_8CPU_32GB` must be selected explicitly: 8 CPUs, 32 GiB memory, and permission to run H100 Slurm jobs.
- Development containers are persistent and GPU-less. An H100 is assigned only while a GPU job runs and is released automatically when the job finishes.
- One GPU can be submitted directly. Requests for 2, 3, or 4 GPUs require complete model and scaling details plus administrator approval. A user can hold at most four GPUs concurrently.
- Each user has 300 GiB of private persistent storage. Both `/workspace` and `/home/<username>` belong to the same quota.
- A lease lasts 96 hours. On expiry, the environment enters a recoverable recycle flow and persistent data is retained.
- Host SSH is disabled. Users connect only to their own development container.

## 2. EasyTier access

The Portal and container SSH are accessed through EasyTier. Do not use or store a physical server management address.

1. Install the EasyTier client supplied by the platform administrator.
2. Import the supplied network configuration or join information. Do not share it.
3. Confirm that EasyTier reports a connected state.
4. Open <http://20.10.10.3:18080/> in a browser.

Connectivity check:

```bash
curl -I http://20.10.10.3:18080/
```

This release uses HTTP inside the private EasyTier network. TLS/HTTPS is an optional later enhancement and is not required for this release.

## 3. Sign-in and initial setup

### 3.1 Set a password

1. Open the one-time setup or reset link issued by the platform.
2. Choose a strong password of at least 14 characters.
3. The link expires after one use. Do not forward it or store a screenshot containing the complete link.
4. A password reset revokes previous sessions and every CLI Token. Sign in and create a new token afterward.

### 3.2 Daily sign-in

Enter your username and password at the production entry point. An ordinary user can access only their own environment, storage, jobs, logs, and keys.

## 4. Create a compute environment

1. Open **My Environment** and choose **Request Compute Resources**.
2. Select a profile:

   - `STANDARD_8CPU_32GB`: the default CPU Development profile.
   - `GPU_1_8CPU_32GB`: explicit GPU Development selection; single-H100 jobs are direct and multi-GPU jobs use a separate approval flow.

3. Enter the purpose and submit.
4. If approval is required, wait for one normal administrator approval. The system automatically performs the Attempt, Reservation, Plan, Dry-run, Stage, and safe rollback checks.
5. When key enrollment is pending, enroll an SSH public key and activate the environment.

Users never need to enter Attempt IDs, container IDs, Slurm IDs, or Git SHAs, and an administrator does not create the container manually.

## 5. SSH public key and activation

Create a dedicated ED25519 key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/h100_portal
```

1. Open **SSH Keys**.
2. Paste the contents of `~/.ssh/h100_portal.pub` or upload the `.pub` file.
3. Select the `CONTAINER` scope.
4. Verify the fingerprint, validate the key, and activate the environment.

Upload only the public key. Keep the private key on your device. The platform never asks you to upload it. Host `authorized_keys` is not used for ordinary-user container access.

## 6. Development container

The **Development Container** page provides Start, Stop, Restart, Web Terminal, and SSH connection information.

Under the released operating model, CPU and GPU Development containers are both GPU-less while resident. For a GPU Development profile, the following display is expected:

```text
H100 · NOT ALLOCATED
No GPU device; H100 jobs are scheduled on demand
```

It does not indicate a provisioning failure. Submit GPU=`1` directly from the Jobs page. Requests for 2-4 GPUs require complete details and approval. Slurm assigns only the approved number of H100s while the job runs and releases them after completion, cancellation, or timeout.

Stopping the development container does not delete `/workspace` or `/home/<username>` and does not end the lease early.

## 7. Unified private storage and zero copy

Both persistent paths are directly read/write accessible from the development container, Slurm CPU jobs, and Slurm GPU jobs:

```text
/workspace
/home/<your-username>
```

For example, files stored by the development container at:

```text
/home/<your-username>/models/model-a
/home/<your-username>/datasets/train
/workspace/projects/demo
```

use the same paths in your Slurm job. No `cp`, `rsync`, upload, or migration step is needed. Results written by a job to either path are immediately visible in the development container.

Suggested layout:

```text
/home/<your-username>/models/       models and caches
/home/<your-username>/datasets/     datasets
/home/<your-username>/projects/     personal projects
/workspace/projects/                shared working tree
/workspace/outputs/                 job outputs
```

The Storage page treats both paths as one private logical scope. The 300 GiB usage comes from the authoritative XFS project quota and bind aliases are not counted twice. Another user's Home or Workspace is not mounted, searchable, or listable.

Only data under `/workspace` or `/home/<your-username>` is part of persistent private storage. Image-layer files and temporary paths such as `/tmp` may not persist.

## 8. Upload and download data

### 8.1 SCP

Copy the current Host, Port, and Username from the Portal connection page:

```bash
scp -i ~/.ssh/h100_portal -P <PORT_FROM_PORTAL> ./dataset.tar \
  <USERNAME>@20.10.10.3:/home/<USERNAME>/datasets/
```

Upload a project directory:

```bash
scp -r -i ~/.ssh/h100_portal -P <PORT_FROM_PORTAL> ./project \
  <USERNAME>@20.10.10.3:/workspace/projects/
```

Download a result:

```bash
scp -i ~/.ssh/h100_portal -P <PORT_FROM_PORTAL> \
  <USERNAME>@20.10.10.3:/workspace/outputs/result.txt ./
```

### 8.2 SFTP and VS Code

SFTP, Remote SSH, and VS Code use the same EasyTier host, dynamic port, username, and private key shown by the Portal. Do not guess the port; check the connection page again after a rebuild or restore.

## 9. Terminal access

### 9.1 Web Terminal

The Web Terminal is available while the container is `RUNNING` and the lease is active. The platform validates the session, ownership, key binding, and container state.

Use it to edit code, download data, install user-level dependencies, and inspect files. GPU compute must be submitted from the Jobs page. It is expected that `nvidia-smi` does not show a GPU in the resident terminal.

### 9.2 SSH

Copy the command from the connection page. It has this form:

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/h100_portal \
  -p <PORT_FROM_PORTAL> <USERNAME>@20.10.10.3
```

## 10. Create a job

Open **Jobs** → **New Job** and enter:

- name;
- script;
- CPU count;
- memory;
- GPU count: `0`, `1`, `2`, `3`, or `4`; counts from `2` through `4` require approval;
- time limit;
- an approved runtime image.

For GPU=`0` or `1`, the Portal creates an immutable script snapshot and submits it directly to Slurm as your ordinary Linux identity. For GPU=`2` through `4`, you must also provide the model name, architecture, framework and version, parameter count, training or inference workload, dataset, parallel strategy, and scaling justification. The request first enters **Awaiting Approval** and does not call Worker, create a Slurm job, allocate a GPU, or produce runtime logs. A platform owner or administrator can approve the requested count, approve a lower count, or reject it with a comment. You can see only your own jobs and logs and may cancel your own request before approval.

The first import of a large CUDA/Enroot image needs extra memory. If a low-memory job reaches `OUT_OF_MEMORY` during a cold image import, retry with at least 8 GiB. Do not bypass the Portal with host commands.

## 11. CLI Job Submission

Container SSH and the Web Terminal provide the platform-managed, ordinary-user-immutable `h100` command. New or rebuilt containers use a read-only bind. Existing running containers receive an atomic root:root mode 0555 hot update without restart, while stopped containers are reconciled the next time they run. The CLI uses the same Portal `/self/jobs` API, ordinary-user identity, Lease, quota, Worker, and Slurm path as the Web Jobs page. It never connects directly to host Slurm.

### 11.1 Create and store a CLI Token

1. Open **Account Security** → **CLI Tokens** in the Portal.
2. Enter a token label and your current web password, select an expiration, and create the token.
3. Copy the token immediately. Its plaintext is shown only once.
4. In the development container, run:

   ```bash
   h100 auth login
   ```

5. Non-interactive automation can provide a token through secure standard input with `h100 auth login --token-stdin`. There is intentionally no `--token TOKEN` argument. Never put a token in a command line, script, Notebook, Git repository, or `/workspace`.

The CLI stores credentials in `~/.config/h100/credentials`. The directory mode is `0700` and the file mode is `0600`. Inspect or remove the local credential with:

```bash
h100 auth status
h100 auth logout
```

`logout` removes only the local copy. Revoke the token under **Account Security** to invalidate it on the server. A token is owner-bound, can access only its creator's jobs, and cannot be created by an administrator account. Authentication is rejected after expiration or revocation, while the account is not ACTIVE, after a password change/reset, or after the account is locked by failed sign-ins. Token plaintext is never sent to Worker, Slurm, or the job environment.

### 11.2 Submit a job

Submit directly from the project directory; no copy or upload is required:

```bash
cd /workspace/projects/demo

h100 job submit train.sh \
  --gpus 1 \
  --cpus 8 \
  --memory 32G \
  --time 01:00:00
```

A script under your Home is also supported:

```bash
cd /home/<your-username>/projects/demo
h100 job submit train.sh
```

Relative and absolute paths are accepted, but the resolved script must be under `/workspace/...` or `/home/<your-username>/...`. `/etc`, `/root`, `/proc`, `/sys`, another user's Home, and arbitrary host paths are rejected. The CLI reads the content and the Portal still creates an immutable script snapshot. Editing the original file later cannot change the submitted job's audit content.

When resource options are omitted, the CLI fetches the same production defaults used by the Web form; it does not hard-code client defaults. `--gpus 0` and `--gpus 1` are submitted directly. A request from `--gpus 2` through `--gpus 4` must include all approval details, for example:

```bash
h100 job submit train.sh --gpus 4 \
  --model-name 'Llama 3.1' \
  --model-architecture 'decoder-only transformer' \
  --framework PyTorch \
  --framework-version 2.6.0 \
  --parameter-count 70B \
  --workload-description 'full-parameter fine-tuning' \
  --dataset-description 'curated 2 TB training corpus' \
  --parallel-strategy 'FSDP full shard' \
  --scaling-justification 'model and optimizer state do not fit on one GPU'
```

The CLI rejects missing details locally and the backend validates them independently. A complete request returns `APPROVAL_PENDING`; no Slurm job or logs exist yet. An administrator can approve any count from 1 through the requested count, but cannot increase it.

Job memory up to and including `32G` needs no additional approval. A larger request must describe
the workload, estimated memory breakdown, and necessity:

```bash
h100 job submit preprocess.sh --memory 128G \
  --memory-workload-description 'large-scale data preprocessing' \
  --memory-breakdown '96 GiB dataset index, 32 GiB runtime and cache' \
  --memory-justification 'the upstream pipeline cannot stream its index'
```

A high-memory request also returns `APPROVAL_PENDING`. An administrator may approve less memory but
cannot increase it above the request. Memory and multi-GPU approvals are independent; when a Job
requires both, every required approval must pass before the Portal creates the single Slurm Job.
Worker is not called while either decision is pending, and no Slurm Job is created after a rejection.

CLI v1 rejects scripts containing resource-changing `#SBATCH` directives. Use `--cpus`, `--memory`, `--gpus`, and `--time`; the Portal backend performs authoritative policy validation again.

### 11.3 List, inspect, read logs, and cancel

```bash
h100 job list
h100 job status <PORTAL_JOB_ID>
h100 job logs <PORTAL_JOB_ID>
h100 job logs <PORTAL_JOB_ID> --stderr --tail 100
h100 job logs <PORTAL_JOB_ID> --follow
h100 job cancel <PORTAL_JOB_ID> --wait
```

`logs --follow` polls only the owner-bound Portal logs endpoint and exits when the job becomes terminal. Ctrl-C exits only the local log client; it never cancels the job. State is the Portal projection of authoritative Slurm state. Requests for another user's job, logs, or cancellation return resource not found and do not reveal that the resource exists.

Compatibility aliases are available:

```bash
h100 sbatch train.sh
h100 squeue
h100 scancel <JOB_ID>
```

The platform does not replace the system `sbatch`, `squeue`, or `scancel` commands. Do not configure MUNGE, use host SSH, access the Worker socket, or call host Slurm directly.

### 11.4 JSON and exit status

Every key command supports `--json`. Normal commands emit one `h100.cli.v1` JSON object; `job logs --follow --json` emits JSON Lines. In JSON mode, stdout contains JSON only and diagnostics go to stderr.

Stable exit status: `0` success, `1` generic client/server failure, `2` usage or invalid arguments, `3` authentication required, `4` authorization or resource not found, and `5` policy/quota rejection.

## 12. CPU job example

This script uses both Home and Workspace with the same paths used in the development container:

```bash
#!/usr/bin/env bash
set -euo pipefail

user_name="$(id -un)"
home_input="/home/${user_name}/datasets/example/input.txt"
workspace_output="/workspace/outputs/cpu-result.txt"

mkdir -p /workspace/outputs
wc -l "$home_input" > "$workspace_output"
cat "$workspace_output"
```

Resource directives are generated from the Portal form. Do not call `salloc` or `srun` manually.

## 13. GPU job example

Select GPU=`1` in the job form:

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-UNSET}"
nvidia-smi -L
nvidia-smi --query-gpu=uuid,name,pci.bus_id --format=csv,noheader

user_name="$(id -un)"
model="/home/${user_name}/models/model-a"
output="/workspace/outputs/gpu-job-finished.txt"

test -e "$model"
mkdir -p /workspace/outputs
date -Is > "$output"
```

Expected behavior:

- `CUDA_VISIBLE_DEVICES=0`;
- `nvidia-smi -L` shows only the assigned H100;
- other GPUs are inaccessible;
- the GPU is released automatically when the job ends.

`NVIDIA_VISIBLE_DEVICES` may retain a base-image default value. The enforced security boundary is the Slurm allocation, container device namespace, and the devices actually accessible through `nvidia-smi -L`.

## 14. Job status, logs, and results

**My Jobs** displays Awaiting Approval, Rejected, Pending, Running, Completed, Failed, Cancelled, Timeout, or Out of Memory states.

- `stdout` and `stderr` are visible only to the job owner.
- Write results under `/workspace` or `/home/<your-username>` for persistence.
- A running development container sees job results immediately, with no synchronization.
- If a submitted GPU job is pending because the aggregate four-GPU user limit is reached, wait for a current GPU job to finish or cancel it in the Portal.

## 15. Lease, recycle, and restore

- Lease duration: 96 hours.
- Container Stop/Start: retains data and is not the same as lease recycle.
- Lease expiry: the system stops the environment and enters a recoverable recycle state.
- Data retention: persistent data under both `/workspace` and `/home/<username>` remains preserved.
- Restore: use the formal Portal restore flow. Do not manually start an old container or copy the data.

After restore, verify the files and permissions in both persistent paths before submitting new jobs.

## 16. Frequently asked questions

### The page says H100 / NOT ALLOCATED. Is something broken?

No. Resident containers and GPU allocations are decoupled. Submit one GPU directly or request 2-4 GPUs for approval; GPUs are visible only while the job runs.

### I downloaded tens of GiB, but Storage shows very little usage.

Confirm that the files are under `/workspace` or `/home/<your-username>`. This release includes both paths in the same XFS project quota. Image-layer or temporary files are not persistent quota data. Refresh the Storage page after checking the paths.

### Why is my GPU job pending?

A user's direct and approved jobs can hold at most four GPUs in aggregate. A submitted job stays Pending with the authoritative Slurm reason when resources are unavailable, the aggregate limit is reached, or another scheduling condition is unmet.

### Why did a GPU=2 through GPU=4 request not start immediately?

Multi-GPU requests require approval. Provide the model, framework and version, parameter count, workload, dataset, parallel strategy, and scaling benefit. While approval is pending, Worker is not called, no Slurm job is created, and no GPU is allocated. An administrator may approve fewer GPUs or reject the request with a reason.

### SSH reports Connection refused.

Check that EasyTier is connected and the development container is `RUNNING`, then copy the current port from the Portal. Do not bind the remote EasyTier IP to a local interface.

### SSH reports Permission denied (publickey).

Check the username, private key, and Portal fingerprint. Use `IdentitiesOnly=yes`, and confirm that the public key has `CONTAINER` scope and an installed state.

### My job cannot see a Home file.

Use your own `/home/<your-username>` path. Arbitrary host paths are never accepted, and an owner mismatch fails closed.

### A CPU job fails with Out of Memory during startup.

A cold import of a large runtime image can require more memory. Retry through the Portal with 8 GiB and retain the failed job as history.

### The Portal does not open.

Check EasyTier and the local outbound firewall, and confirm the URL is <http://20.10.10.3:18080/>. Do not switch to a physical management address.
