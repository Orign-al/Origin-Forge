# H100 GPU bypass report correction — 20260805-205323

## Corrected conclusion

`PREVIOUS JOB 10 RESULT WAS A DEVICE-IDENTITY FALSE NEGATIVE`

The Pilot-1B report conservatively described Job 10 as an allocated-GPU open
failure after a Perl `sysopen(O_RDWR)` of `/dev/nvidia0` returned `EPERM`.
Job 10 did show UUID
`GPU-c8377945-df2c-5761-8798-66385611808b`, but the report did not record or
prove that UUID's Linux minor. Therefore the physical identity of the tested
path was not established.

Original report:

```text
/srv/gpu-platform/platform/reports/gpu-bypass-20260805-193713/h100-slurm-out-of-job-gpu-remediation-20260805-193713.md
SHA-256: ad2ce21382cee7a4e05f75fb59b83ab4259a7b6821f8242ff0c357eabe727a9f
```

The original report remains intact.

## Physical identity evidence

The current driver does not expose `minor_number` through
`nvidia-smi --query-gpu`. Pilot-1C therefore correlated the UUID and PCI Bus ID
with `/proc/driver/nvidia/gpus/*/information` and verified each character
device with `stat(2)`:

| NVML index | UUID | PCI Bus ID | Linux minor | Device |
|---:|---|---|---:|---|
| 0 | `GPU-c8377945-df2c-5761-8798-66385611808b` | `00000000:01:00.0` | 1 | `/dev/nvidia1` |
| 1 | `GPU-992f38cb-b919-5d6c-ddba-b8c1b2f771a9` | `00000000:71:00.0` | 0 | `/dev/nvidia0` |
| 2 | `GPU-cb103ce8-4672-873d-1f87-6d7c5ad771b2` | `00000000:81:00.0` | 3 | `/dev/nvidia3` |
| 3 | `GPU-873d76ca-5a84-5177-7ef1-9fdbc98c540f` | `00000000:F1:00.0` | 2 | `/dev/nvidia2` |

Job 10's visible UUID therefore mapped to `/dev/nvidia1`. The fixed
`/dev/nvidia0` probe addressed the second physical GPU, which was unallocated
to Job 10. Its `EPERM` result was the expected Slurm isolation outcome.

## Corrected validation

- Bare Slurm Job 11 received the same UUID/minor 1 mapping. `O_RDONLY`,
  `O_WRONLY`, and `O_RDWR` succeeded for `/dev/nvidia1`; all three other
  per-GPU nodes returned `EPERM`; a real CUDA Driver API context succeeded;
  the job completed `0:0`.
- Pinned, offline Pyxis Job 12 produced the same result and completed `0:0`.
- Four concurrent Jobs 13–16 covered all four UUIDs/minors; each opened only
  its allocated per-GPU node, denied the other three, and created a CUDA
  context.
- During Job 17, Slurm's effective cgroup-device program ID 1492 (tag
  `790fb0b71046db44`) explicitly allowed char major 195/minor 1 and denied
  minors 0, 3, and 2. Bounded `strace` showed the same path/flag/errno results.
- With transient `user-1001.slice DevicePolicy=closed`, bare Job 18, Pyxis Job
  19, and concurrent Jobs 20–23 all passed the same checks, while the login
  session could open no per-GPU node and `cuInit` returned
  `CUDA_ERROR_NO_DEVICE`.

The failure was in the old test's device-number assumption, not in Slurm GRES,
Slurm cgroup v2, Pyxis, Enroot, NVIDIA shared/control nodes, caps access, or the
CUDA runtime.

## Scope and final state

This correction does not deploy a persistent isolation policy. The transient
per-user slice was rolled back to `DevicePolicy=auto`; no global or per-UID
drop-in and no Guard were installed. The node remains DRAINED pending explicit
approval of the per-Pilot-UID persistent design.
