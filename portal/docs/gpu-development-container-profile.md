# GPU Development Container Profile

Status: implementation contract, not yet deployed

Profile ID: `GPU_1_8CPU_32GB`

## Profiles

| Property           | CPU Development      | GPU Development                                        |
| ------------------ | -------------------- | ------------------------------------------------------ |
| Profile ID         | `STANDARD_8CPU_32GB` | `GPU_1_8CPU_32GB`                                      |
| CPU / memory       | 8 CPU / 32 GiB       | 8 CPU / 32 GiB                                         |
| Interactive GPU    | none                 | exactly 1 H100                                         |
| NVIDIA runtime     | absent               | enabled only while allocated and running               |
| Scheduler source   | none                 | owner/lease-bound Slurm `--gres=gpu:h100:1` allocation |
| Stopped definition | GPU-less             | GPU-less                                               |

The CPU profile is the default and retains its previous GPU-less behavior.
GPU Development requires the user's Slurm maximum GPU entitlement to be `1`.
Both API validation and database constraints reject inconsistent profiles and
GPU count `2`.

## Allocation flow

```text
active owner Lease
      |
      v
sbatch as owner UID/GID
  --account=company --qos=general --partition=gpu-dev --gres=gpu:h100:1
  --comment=h100-gpu-dev:{managed_user_id}:{lease_id}
      |
      v
verify RUNNING + owner + account + QOS + creating-Lease comment + one GPU index
      |
      v
map Slurm GPU index -> physical NVIDIA GPU UUID
      |
      v
fixed Root Worker handler creates Docker NVIDIA DeviceRequest for that UUID
      |
      v
verify Docker device cgroup/runtime/env/labels and nvidia-smi == one UUID
```

The dedicated `gpu-dev` partition is non-default and has `MaxTime=INFINITE`;
its fixed hold command is `/usr/bin/sleep infinity`. The allocation job runs as
the owner and is cancelled by the allocation-aware lifecycle only after Docker
removal is proven. Its comment retains the canonical UUID of the Lease that
created the allocation. A renewed successor Lease may manage that same
allocation, but the managed-user prefix and syntactically valid creating-Lease
UUID remain mandatory. The active Lease controls user access; an allocation may
remain as a safety lock after Lease expiry only while cleanup is failing. This
fail-closed behavior prevents Slurm from assigning the same GPU to someone else
while a possibly live container still references it. The allocation exports the
same `WORKSPACE` contract. No user script runs as root.

Because a Slurm user may cancel their own allocation, the host also configures
the integrity-pinned `h100-gpu-development-epilog`. GPU container start and the
epilog serialize on the platform lifecycle lock. Start revalidates the live
owner/account/QOS/partition/index-to-UUID binding while holding that lock. On
any allocation exit, the epilog removes the container and proves its GPU-less
stopped definition before Slurm returns the node. A teardown failure returns a
nonzero epilog status, causing Slurm to drain the node instead of reassigning a
possibly still-bound GPU.

## Dual isolation lock

The profile deliberately requires both locks:

1. Slurm owns scheduling and concurrency. The allocation must be RUNNING and
   bound to the owner, its creating Lease, account, QOS, and one GPU index.
2. Docker/NVIDIA owns the interactive container device cgroup. Its sole
   `DeviceRequest` names the exact physical GPU UUID proven by Slurm.

The runtime sets:

```text
NVIDIA_VISIBLE_DEVICES=GPU-<assigned-uuid>
CUDA_VISIBLE_DEVICES=0
```

Inside the container, the assigned device is renumbered to CUDA ordinal `0`.
The worker also runs `nvidia-smi --query-gpu=uuid` inside the container and
requires exactly the assigned UUID. Environment variables are defense in
depth; the NVIDIA DeviceRequest and Docker device cgroup are the enforcement
boundary. Direct `HostConfig.Devices`, extra device cgroup rules, added Linux
capabilities, and `NVIDIA_VISIBLE_DEVICES=all` are rejected.

## Start, stop, restart, recycle, and restore

- Stage always creates a stopped GPU-less Compose definition.
- Start first obtains and verifies a new Slurm allocation, then creates the
  UUID-restricted NVIDIA container.
- Stop first force-removes the GPU container and recreates its stopped
  GPU-less definition. Only after that postcondition succeeds may the Worker
  cancel the Slurm allocation.
- Restart performs the same removal/cancel/allocation/start sequence. If the
  new start fails, the Worker removes any partial container before cancelling
  the new allocation and reports authoritative cleanup coordinates to Portal.
- Recycle suspends the container SSH key, removes the container, revokes the
  GPU allocation, cancels remaining jobs, and preserves the workspace.
- Restore creates a new Lease, obtains a new scheduler allocation, and remounts
  the same workspace. Failure rolls back through the controlled recycle path.

If removal cannot be proven, the allocation is retained and the operation is
marked for manual review. Releasing a GPU while a possibly running container
still references it is forbidden.

## Security postconditions

Every running GPU profile must prove:

- one Slurm GPU and one Docker UUID, with identical physical identity;
- the configured, integrity-pinned fail-safe Slurm epilog;
- owner, managed-user, creating-Lease UUID, Slurm account, QOS, and `gpu-dev`
  partition binding;
- UID/GID labels and canonical owner-only workspace;
- `runtime=nvidia`, exactly one NVIDIA DeviceRequest, and no direct devices;
- no privileged mode, host namespaces, Docker socket, MUNGE, host root, or
  unapproved mount;
- active Lease and max GPU `1`.

Every CPU or stopped definition must prove the inverse: no NVIDIA runtime,
NVIDIA/CUDA visibility variables, DeviceRequest, direct device, or GPU
allocation coordinate.
