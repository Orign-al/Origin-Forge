# NVIDIA H100 driver installation record

- Run ID: `20260804-050357`
- Host: `sagsh100server`
- OS: Ubuntu 26.04 LTS (`resolute`)
- Running kernel before reboot: `7.0.0-14-generic`
- Next GRUB default kernel: `7.0.0-28-generic`
- Secure Boot: disabled
- PCIe baseline: four NVIDIA GH100 / H100 PCIe devices visible at `01:00.0`, `71:00.0`, `81:00.0`, and `f1:00.0`

## Official sources and selection

Official material consulted during this run:

- NVIDIA Data Center Driver Installation Guide: <https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/>
- Ubuntu installation instructions: <https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/ubuntu.html>
- NVIDIA kernel-module guidance: <https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/kernel-modules.html>
- Supported driver branches and CUDA compatibility: <https://docs.nvidia.com/datacenter/tesla/drivers/supported-drivers-and-cuda-toolkit-versions.html>
- NVIDIA Ubuntu 26.04 x86_64 repository: <https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2604/x86_64/>

The documentation listed R580 as LTS, R595 as the Production Branch, and R610 as the New Feature Branch at the time of selection. R595 was selected because this is a production server and R595 officially supports Hopper/H100. NVIDIA Open Kernel Modules were selected in accordance with current Hopper guidance. No `.run` installer, PPA, third-party source, mixed driver source, or `ubuntu-drivers autoinstall` was used.

## Installed compute-only package set

- `cuda-keyring` `1.1-1`
- `nvidia-driver-pinning-595` `595-1ubuntu1`
- `libnvidia-compute` `595.91.07-1ubuntu1`
- `nvidia-dkms-open` `595.91.07-1ubuntu1`
- `nvidia-persistenced` `595.91.07-1ubuntu1`

The R595 pin is installed in `/etc/apt/preferences.d/nvidia-driver-pin`. The repository is signed through `/usr/share/keyrings/cuda-archive-keyring.gpg`. A full CUDA Toolkit, `nvcc`, and desktop GL/X driver stack were not installed.

## Pre-reboot verification

- DKMS `nvidia/595.91.07`: installed for `7.0.0-14-generic`
- DKMS `nvidia/595.91.07`: installed for `7.0.0-28-generic`
- New-kernel module: `/lib/modules/7.0.0-28-generic/updates/dkms/nvidia.ko.zst`
- Module version: `595.91.07`
- Module license: `Dual MIT/GPL`
- `/boot/initrd.img-7.0.0-28-generic` SHA-256: `c55f8246aaba17be34940a21aca2ef839d3046f00f628d051d9854f4918c35b3`
- New initramfs contains `nvidia.ko`, `nvidia-modeset.ko`, `nvidia-drm.ko`, and the packaged nouveau blacklist
- `update-initramfs -u -k all`: completed successfully

## Required post-reboot checks

The current boot still has `nouveau` bound to all four GPUs. It was intentionally not unloaded or replaced live. `nvidia-persistenced.service` currently fails because `/dev/nvidia*` does not yet exist. This service must be rechecked only after the approved reboot allows the NVIDIA module to bind the devices.

The current boot journal also contains PCI DOE mailbox timeouts and nouveau-associated AMD-Vi I/O page faults for the H100 devices. Post-reboot acceptance must therefore verify driver/NVML consistency, all four GPUs and UUIDs, Xid/AER/PCIe errors, ECC, temperature, power, topology, and persistence service state. MIG may only be queried and must not be changed.

Status: `NVIDIA DRIVER REBOOT REQUIRED`

`NVIDIA DRIVER PASSED` has not been declared.
