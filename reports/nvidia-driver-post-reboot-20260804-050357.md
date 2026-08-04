# NVIDIA H100 post-reboot acceptance

- Run ID: `20260804-050357`
- Boot ID: `3b8b3af2-d2bd-4040-886e-e9129ac6c005`
- Running kernel: `7.0.0-28-generic`
- Driver and NVML version: `595.91.07`
- Kernel module: NVIDIA Open Kernel Module, `Dual MIT/GPL`
- Secure Boot: disabled
- Attached GPUs: 4
- `nouveau` loaded: no
- `nvidia-persistenced.service`: enabled and active

## GPU acceptance table

| GPU | UUID | PCI Bus ID | Model | Memory | Temp | Power | ECC | Persistence | Compute Mode | MIG Mode |
|---:|---|---|---|---:|---:|---:|---|---|---|---|
| 0 | `GPU-c8377945-df2c-5761-8798-66385611808b` | `00000000:01:00.0` | NVIDIA H100 PCIe | 81559 MiB | 29 C | 48.51 W | enabled; all reported volatile/aggregate counts 0 | enabled | Default | disabled |
| 1 | `GPU-992f38cb-b919-5d6c-ddba-b8c1b2f771a9` | `00000000:71:00.0` | NVIDIA H100 PCIe | 81559 MiB | 30 C | 51.14 W | enabled; all reported volatile/aggregate counts 0 | enabled | Default | disabled |
| 2 | `GPU-cb103ce8-4672-873d-1f87-6d7c5ad771b2` | `00000000:81:00.0` | NVIDIA H100 PCIe | 81559 MiB | 29 C | 49.16 W | enabled; all reported volatile/aggregate counts 0 | enabled | Default | disabled |
| 3 | `GPU-873d76ca-5a84-5177-7ef1-9fdbc98c540f` | `00000000:F1:00.0` | NVIDIA H100 PCIe | 81559 MiB | 30 C | 50.13 W | enabled; all reported volatile/aggregate counts 0 | enabled | Default | disabled |

## PCIe, ECC, and topology

- All four GPUs negotiated PCIe generation 5, width x16; these equal their reported maximum generation and width.
- GPU 0 and 1 are local to NUMA node 0; GPU 2 and 3 are local to NUMA node 1.
- No GPU workload was running during acceptance.
- Volatile and aggregate SRAM/DRAM corrected and uncorrected ECC counts were all zero.
- Row-remap corrected/uncorrected counts were all zero; no remap pending or failure.
- No channel repair, TPC repair, unrepairable memory, or SRAM threshold condition was reported.
- No Xid, fatal/uncorrectable AER, or driver/library mismatch was found in the current boot acceptance scan.

## MIG capability query

MIG was not enabled or modified. The read-only GPU-instance profile query reported these supported H100 profiles on all four GPUs:

- `1g.10gb`
- `1g.10gb+me`
- `1g.20gb`
- `2g.20gb`
- `3g.40gb`
- `4g.40gb`
- `7g.80gb`

The compute-instance query correctly reported no MIG-enabled devices because MIG remains disabled.

## Non-blocking observations

- The kernel reports an out-of-tree/module-signature taint; Secure Boot is disabled and the installed module is the NVIDIA repository Open DKMS build.
- Headless H100 3D controllers report no compatible DRM CRTC/format; no display service is required.
- Each H100 produced a PCI DOE mailbox timeout during early boot. This is retained as P1 for vendor/kernel follow-up. It was also present before NVIDIA installation. It did not produce an Xid or AER failure, and all four GPUs are visible, at Gen5 x16, ECC-clean, and operational through NVML.
- `systemd-networkd-wait-online.service` remains the only failed unit and is tracked separately as P2; no network configuration was changed.

## Raw records

- Full `nvidia-smi -q`: `/srv/gpu-platform/platform/reports/nvidia-smi-q-post-reboot-20260804-050357.txt`
  - SHA-256: `c34592b2f17c1e0509d5aa2c8e5b3fb9ea9221d914cf8a28edd81138e7870fca`
- MIG profile query: `/srv/gpu-platform/platform/reports/nvidia-mig-profiles-post-reboot-20260804-050357.txt`
  - SHA-256: `abe0079e29c7258973433725db55af0ed47f918d29d55b94b0b04f9b29f7c5ef`
- Current boot kernel journal: `/srv/gpu-platform/platform/reports/nvidia-kernel-journal-post-reboot-20260804-050357.txt`
  - SHA-256: `3585a1411c970dd1adc9895202ae408ef3d0030cc2d485c2f0f11c1c3c08b118`

Status: `NVIDIA DRIVER PASSED`

`MIG NOT MODIFIED`
