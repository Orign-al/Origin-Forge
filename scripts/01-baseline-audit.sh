#!/usr/bin/env bash
set -uo pipefail

section() {
    printf '\n## %s\n' "$1"
}

readonly GIB=$((1024 * 1024 * 1024))
readonly TIB=$((1024 * 1024 * 1024 * 1024))
storage_ok=1
gpu_ok=1

section "Time and operating system"
date
date -Is
timedatectl
hostnamectl
cat /etc/os-release
uname -a
uptime

section "CPU NUMA and memory"
lscpu
if command -v numactl >/dev/null 2>&1; then
    numactl --hardware
else
    printf 'numactl is not installed\n'
fi
free -h
swapon --show

section "Block devices and mounts"
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL
lsblk -o NAME,RM,HOTPLUG,TRAN,SIZE,TYPE,FSTYPE,MOUNTPOINTS,MODEL,SERIAL
findmnt
findmnt /
findmnt /boot
findmnt /boot/efi
findmnt /var/lib/docker
findmnt /srv/gpu-platform
df -hT
df -i

section "LVM"
sudo pvs
sudo vgs
sudo lvs -a -o +devices

section "fstab"
cat /etc/fstab

section "PCI devices"
lspci -nn
lspci -nn | grep -i nvidia || true
lspci -nn | grep -Ei 'mellanox|ethernet|infiniband' || true

section "Network"
ip -br addr
ip route
ip -s link
ss -lntup

section "systemd"
systemctl --failed
systemctl list-units --type=service --state=running

section "Installed packages"
dpkg -l
if command -v snap >/dev/null 2>&1; then
    snap list 2>/dev/null || true
else
    printf 'snap command is not installed\n'
fi

section "Accounts and groups"
getent passwd
getent group
id origin-al
id codexops

section "Secure Boot"
if command -v mokutil >/dev/null 2>&1; then
    mokutil --sb-state 2>/dev/null || true
else
    printf 'mokutil is not installed\n'
fi

section "XFS geometry"
sudo xfs_info /var/lib/docker
sudo xfs_info /srv/gpu-platform

section "Automated hard-gate checks"
[[ "$(findmnt -nro FSTYPE /)" == "ext4" ]] || storage_ok=0
[[ "$(findmnt -nro FSTYPE /boot)" == "ext4" ]] || storage_ok=0
[[ "$(findmnt -nro FSTYPE /boot/efi)" == "vfat" ]] || storage_ok=0
[[ "$(findmnt -nro FSTYPE /var/lib/docker)" == "xfs" ]] || storage_ok=0
[[ "$(findmnt -nro FSTYPE /srv/gpu-platform)" == "xfs" ]] || storage_ok=0

root_source="$(findmnt -nro SOURCE /)"
docker_source="$(findmnt -nro SOURCE /var/lib/docker)"
platform_source="$(findmnt -nro SOURCE /srv/gpu-platform)"
[[ "$root_source" != "$docker_source" ]] || storage_ok=0
[[ "$root_source" != "$platform_source" ]] || storage_ok=0
[[ "$docker_source" != "$platform_source" ]] || storage_ok=0

root_size="$(lsblk -bdno SIZE "$root_source" | awk 'NR == 1 { printf "%.0f", $1 }')"
docker_size="$(lsblk -bdno SIZE "$docker_source" | awk 'NR == 1 { printf "%.0f", $1 }')"
platform_size="$(lsblk -bdno SIZE "$platform_source" | awk 'NR == 1 { printf "%.0f", $1 }')"
swap_size="$(swapon --show=SIZE --bytes --noheadings | awk '{ total += $1 } END { printf "%.0f", total }')"
vg_free="$(sudo vgs --noheadings --units b --nosuffix -o vg_free vg_h100 | awk '{ printf "%.0f", $1 }')"

(( root_size >= 180 * GIB && root_size <= 230 * GIB )) || storage_ok=0
(( docker_size >= 650 * GIB && docker_size <= 750 * GIB )) || storage_ok=0
(( platform_size >= 5 * TIB && platform_size <= 6 * TIB )) || storage_ok=0
(( swap_size >= 56 * GIB && swap_size <= 72 * GIB )) || storage_ok=0
(( vg_free >= 800 * GIB && vg_free <= 950 * GIB )) || storage_ok=0

sudo xfs_info /var/lib/docker | grep -Eq 'ftype=1([,[:space:]]|$)' || storage_ok=0
sudo xfs_info /srv/gpu-platform | grep -Eq 'ftype=1([,[:space:]]|$)' || storage_ok=0

h100_count="$(lspci -Dnn -d 10de:2331 | wc -l)"
[[ "$h100_count" -eq 4 ]] || gpu_ok=0

printf 'root_source=%s root_size_bytes=%s\n' "$root_source" "$root_size"
printf 'docker_source=%s docker_size_bytes=%s\n' "$docker_source" "$docker_size"
printf 'platform_source=%s platform_size_bytes=%s\n' "$platform_source" "$platform_size"
printf 'swap_size_bytes=%s vg_h100_free_bytes=%s\n' "$swap_size" "$vg_free"
printf 'h100_pcie_count=%s\n' "$h100_count"

if [[ "$storage_ok" -ne 1 ]]; then
    printf 'STORAGE BASELINE BLOCKED\n'
    exit 20
fi
printf 'STORAGE BASELINE PASSED\n'

if [[ "$gpu_ok" -ne 1 ]]; then
    printf 'GPU HARDWARE BASELINE BLOCKED\n'
    exit 21
fi
printf 'GPU HARDWARE BASELINE PASSED\n'
printf 'BASELINE AUDIT PASSED\n'
