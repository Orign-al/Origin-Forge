# Host GPU device map

Minor numbers are correlated through `/proc/driver/nvidia/gpus/*/information`; this driver does not expose `minor_number` through `nvidia-smi --query-gpu`.

| NVML index | UUID | PCI Bus ID | Linux minor | Device path | Major | Minor | Name | Memory MiB |
|---:|---|---|---:|---|---:|---:|---|---:|
| 0 | `GPU-c8377945-df2c-5761-8798-66385611808b` | `00000000:01:00.0` | 1 | `/dev/nvidia1` | 195 | 1 | NVIDIA H100 PCIe | 81559 |
| 1 | `GPU-992f38cb-b919-5d6c-ddba-b8c1b2f771a9` | `00000000:71:00.0` | 0 | `/dev/nvidia0` | 195 | 0 | NVIDIA H100 PCIe | 81559 |
| 2 | `GPU-cb103ce8-4672-873d-1f87-6d7c5ad771b2` | `00000000:81:00.0` | 3 | `/dev/nvidia3` | 195 | 3 | NVIDIA H100 PCIe | 81559 |
| 3 | `GPU-873d76ca-5a84-5177-7ef1-9fdbc98c540f` | `00000000:F1:00.0` | 2 | `/dev/nvidia2` | 195 | 2 | NVIDIA H100 PCIe | 81559 |
