# Slurm GRES and cgroup-device BPF analysis — 20260805-205323

## GRES mapping

`/etc/slurm/gres.conf` contains only `AutoDetect=nvml`. There is no explicit
`File=`, no `MultipleFiles`, and no separately configured GRES for
`nvidiactl`, UVM, modeset, or caps nodes. `ConstrainDevices=yes` remains set in
`cgroup.conf`; neither file was modified.

`slurmd -G` enumerated devices in NVML/PCI order as follows:

| GRES ordinal | `slurmd -G` Index | File | UUID/PCI identity |
|---:|---:|---|---|
| 0 | 1 | `/dev/nvidia1` | NVML 0, `GPU-c837…`, `0000:01:00.0` |
| 1 | 0 | `/dev/nvidia0` | NVML 1, `GPU-992f…`, `0000:71:00.0` |
| 2 | 3 | `/dev/nvidia3` | NVML 2, `GPU-cb103…`, `0000:81:00.0` |
| 3 | 2 | `/dev/nvidia2` | NVML 3, `GPU-873d…`, `0000:f1:00.0` |

The repeated printed `ID=7696487` is not a unique physical GPU identifier.
Physical identity must be established with UUID, PCI Bus ID, procfs minor, and
the device's `st_rdev`. In each one-GPU job, `CUDA_VISIBLE_DEVICES=0` was only
a job-local logical index.

## Slurm device policy

Bare Job 17 ran at:

```text
/system.slice/slurmstepd.scope/job_17/step_batch/user/task_0
```

Its effective Slurm program was ID 1492, name `Slurm_Cgroup_v2`, tag
`790fb0b71046db44`. The translated instructions implemented this per-GPU mask:

| Device condition | Verdict |
|---|---|
| char major 195, minor 1 (allocated) | allow |
| char major 195, minor 0 | deny |
| char major 195, minor 3 | deny |
| char major 195, minor 2 | deny |
| other devices, including required shared/control/UVM nodes | allow |

The program was effective from `job_17` through the task cgroup; it was not
attached at `slurmstepd.scope`, `system.slice`, or root. Bounded `strace` proved
that `/dev/nvidia1` opened with `O_RDWR`, minors 0/3/2 returned `EPERM`, and
`nvidiactl`, UVM, and the readable caps node remained usable. The real CUDA
context succeeded.

## systemd per-user policy

Under transient `user-1001.slice DevicePolicy=closed`, systemd attached program
ID 1539, name `sd_devices`, tag `a97c143260cd9940` at:

```text
/user.slice/user-1001.slice
```

Its translated program allowed the standard pseudo/PTY devices required by a
login session and denied devices outside that closed list. All four NVIDIA
per-GPU opens returned `EPERM`, and CUDA initialization reported no device.

The user slice is not an ancestor of Slurm steps. Therefore its systemd program
does not combine with or restrict tasks under `system.slice/slurmstepd.scope`.

## Pyxis and Enroot

Pyxis Job 24 saw `/` inside its cgroup namespace, while its host task PID was
at:

```text
/system.slice/slurmstepd.scope/job_24/step_0/user/task_0
```

The effective task program was Slurm ID 1545 with the same
`Slurm_Cgroup_v2` tag `790fb0b71046db44`; step/user ancestors had Slurm IDs
1543–1545. No Pyxis- or Enroot-specific cgroup-device program appeared in the
effective host ancestry, and no `sd_devices` program from `user-1001.slice`
was inherited. Pyxis/Enroot supplied the container device nodes and injected
driver libraries, while Slurm's host cgroup program remained the security
boundary. Allocated minor 1 opened, minors 0/3/2 were denied, and the container
created a CUDA context.

## Root-cause classification

No Slurm GRES/BPF, Pyxis/Enroot, shared/control/UVM/caps, or CUDA failure was
found. The Pilot-1B failure is classified as a test-device identity error: the
probe opened an unallocated Linux minor after equating logical GPU 0 with
`/dev/nvidia0`.
