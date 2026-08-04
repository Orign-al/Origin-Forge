#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for protected Slurm changes.' >&2
    exit 2
fi

run_id=${1:?usage: 18-configure-slurm-base.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
record_dir=${platform_dir}/reports/slurm-base-${run_id}.d
report_file=${platform_dir}/reports/slurm-base-${run_id}.md
backup_dir=${platform_dir}/backups
slurm_source=${platform_dir}/config/slurm.conf
cgroup_source=${platform_dir}/config/cgroup.conf
gres_source=${platform_dir}/config/gres.conf
slurm_target=/etc/slurm/slurm.conf
cgroup_target=/etc/slurm/cgroup.conf
gres_target=/etc/slurm/gres.conf
hostname_expected=sagsh100server
hardware_expected='NodeName=sagsh100server CPUs=256 Boards=1 SocketsPerBoard=2 CoresPerSocket=64 ThreadsPerCore=2 RealMemory=510953 Gres=gpu:nvidia_h100_pcie:4'
configured_memory=486377
reserved_memory=24576

for source_file in "${slurm_source}" "${cgroup_source}" "${gres_source}"; do
    if [[ ! -f ${source_file} ]]; then
        printf 'Tracked Slurm configuration is missing: %s\n' "${source_file}" >&2
        exit 1
    fi
done
if [[ -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite an existing Slurm base record.' >&2
    exit 1
fi

sudo -n true
install -d -m 2770 "${record_dir}" "${backup_dir}"

if [[ $(hostname -s) != "${hostname_expected}" ]]; then
    printf 'Hostname changed since discovery: %s\n' "$(hostname -s)" >&2
    exit 1
fi
if ! getent hosts "${hostname_expected}" >"${record_dir}/hostname-resolution.txt"; then
    printf '%s\n' 'The actual short hostname does not resolve.' >&2
    exit 1
fi
if [[ $(stat -fc '%T' /sys/fs/cgroup) != cgroup2fs ]]; then
    printf '%s\n' 'The host is not using unified cgroup v2.' >&2
    exit 1
fi

hardware_line=$(sudo slurmd -C | sed -n '1p')
printf '%s\n' "${hardware_line}" >"${record_dir}/slurmd-C.txt"
if [[ ${hardware_line} != "${hardware_expected}" ]]; then
    printf '%s\n' 'slurmd -C hardware data changed from the reviewed baseline.' >&2
    printf 'Expected: %s\nActual:   %s\n' "${hardware_expected}" "${hardware_line}" >&2
    exit 1
fi
if (( 510953 - configured_memory != reserved_memory )); then
    printf '%s\n' 'Configured RealMemory reserve arithmetic is invalid.' >&2
    exit 1
fi

nvidia-smi -L >"${record_dir}/nvidia-smi-L.txt"
gpu_count=$(grep -c '^GPU [0-9]\+:' "${record_dir}/nvidia-smi-L.txt")
if [[ ${gpu_count} != 4 ]]; then
    printf 'Expected four full GPUs, found %s.\n' "${gpu_count}" >&2
    exit 1
fi
if grep -q '^  MIG ' "${record_dir}/nvidia-smi-L.txt"; then
    printf '%s\n' 'MIG instances appeared unexpectedly; stopping without modifying MIG.' >&2
    exit 1
fi
nvidia-smi --query-gpu=index,name,uuid,pci.bus_id,memory.total \
    --format=csv,noheader >"${record_dir}/gpu-inventory.csv"

for target_file in "${slurm_target}" "${cgroup_target}" "${gres_target}"; do
    if sudo test -e "${target_file}"; then
        printf 'Unknown Slurm configuration appeared: %s\n' "${target_file}" >&2
        exit 1
    fi
done
if [[ $(sudo stat -c '%U:%G:%a' /etc/slurm/slurmdbd.conf) != slurm:slurm:600 ]]; then
    printf '%s\n' 'Protected slurmdbd.conf metadata changed.' >&2
    exit 1
fi

sudo tar --acls --xattrs -C /etc --exclude='slurm/slurmdbd.conf' \
    -cpf "${backup_dir}/etc-slurm-nonsecret-before-base-${run_id}.tar" slurm
if sudo tar -tf "${backup_dir}/etc-slurm-nonsecret-before-base-${run_id}.tar" \
    | grep -q 'slurmdbd.conf'; then
    printf '%s\n' 'Secret exclusion from the Slurm directory backup failed.' >&2
    exit 1
fi

sudo install -o root -g root -m 0644 "${slurm_source}" "${slurm_target}"
sudo install -o root -g root -m 0644 "${cgroup_source}" "${cgroup_target}"
sudo install -o root -g root -m 0644 "${gres_source}" "${gres_target}"

sudo install -d -o slurm -g slurm -m 0750 /var/spool/slurmctld
sudo install -d -o root -g root -m 0755 /var/spool/slurmd
sudo install -d -o slurm -g slurm -m 0750 /var/log/slurm
if sudo test -e /var/log/slurm/slurmctld.log || sudo test -e /var/log/slurm/slurmd.log; then
    printf '%s\n' 'An unknown controller or node log appeared before first start.' >&2
    exit 1
fi
sudo install -o slurm -g slurm -m 0640 /dev/null /var/log/slurm/slurmctld.log
sudo install -o root -g slurm -m 0640 /dev/null /var/log/slurm/slurmd.log

sudo slurmd -C >"${record_dir}/slurmd-C-after-config.txt" 2>&1
sudo slurmd -G >"${record_dir}/slurmd-G.txt" 2>&1
if grep -Eqi 'error:|fatal:|invalid|mismatch|ignored' "${record_dir}/slurmd-G.txt"; then
    printf '%s\n' 'slurmd -G reported a GRES validation problem.' >&2
    exit 1
fi
validated_gpu_count=$(awk '
    /Gres Name=gpu Type=h100 Count=/ {
        for (field = 1; field <= NF; field++) {
            if ($field ~ /^Count=/) {
                split($field, value, "=")
                count += value[2]
            }
        }
    }
    END { print count + 0 }
' "${record_dir}/slurmd-G.txt")
if [[ ${validated_gpu_count} != 4 ]]; then
    printf 'slurmd -G validated %s GPUs with type h100 instead of four.\n' \
        "${validated_gpu_count}" >&2
    exit 1
fi

cluster_count=$(sudo sacctmgr -nP show cluster format=Cluster 2>/dev/null \
    | awk -F'|' '$1 == "h100" { count++ } END { print count + 0 }')
if [[ ${cluster_count} == 0 ]]; then
    sudo sacctmgr -i add cluster h100 >"${record_dir}/sacctmgr-add-cluster.txt" 2>&1
elif [[ ${cluster_count} != 1 ]]; then
    printf '%s\n' 'The accounting database contains duplicate h100 cluster records.' >&2
    exit 1
fi
if ! sudo sacctmgr -nP show cluster format=Cluster | grep -Fxq 'h100'; then
    printf '%s\n' 'The h100 cluster was not registered in SlurmDBD.' >&2
    exit 1
fi

sudo install -d -o slurm -g slurm -m 0755 /run/slurmctld
set +e
sudo -u slurm timeout --signal=TERM --kill-after=5s 12s \
    /usr/sbin/slurmctld -D -c -f "${slurm_target}" -vv \
    >"${record_dir}/slurmctld-foreground-validation.txt" 2>&1
validation_status=$?
set -e
if [[ ${validation_status} -ne 0 && ${validation_status} -ne 124 ]]; then
    printf 'slurmctld foreground validation exited unexpectedly: %s\n' \
        "${validation_status}" >&2
    exit 1
fi
if grep -Eqi 'fatal:|error:' "${record_dir}/slurmctld-foreground-validation.txt"; then
    printf '%s\n' 'slurmctld foreground validation reported a fatal or error condition.' >&2
    exit 1
fi
if ! grep -Eqi 'slurmctld version .* started|slurmctld.*started' \
    "${record_dir}/slurmctld-foreground-validation.txt"; then
    printf '%s\n' 'slurmctld foreground validation did not reach a started state.' >&2
    exit 1
fi
if pgrep -u slurm -x slurmctld >/dev/null; then
    printf '%s\n' 'Foreground validation left a slurmctld process running.' >&2
    exit 1
fi

sudo systemctl enable --now slurmctld
if ! systemctl is-active --quiet slurmctld; then
    printf '%s\n' 'slurmctld failed to become active.' >&2
    exit 1
fi
sudo systemctl enable --now slurmd
if ! systemctl is-active --quiet slurmd; then
    printf '%s\n' 'slurmd failed to become active.' >&2
    exit 1
fi

sudo scontrol update NodeName="${hostname_expected}" State=DRAIN Reason='platform bootstrap'
node_state=$(sinfo -h -n "${hostname_expected}" -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf 'Node did not remain drained: %s\n' "${node_state}" >&2
    exit 1
fi

srun --version >"${record_dir}/srun-version.txt"
scontrol show config >"${record_dir}/scontrol-show-config.txt"
sinfo -Nel >"${record_dir}/sinfo-Nel.txt"
scontrol show node "${hostname_expected}" >"${record_dir}/scontrol-show-node.txt"
squeue >"${record_dir}/squeue.txt"
systemctl status slurmdbd slurmctld slurmd --no-pager >"${record_dir}/service-status.txt"
sudo journalctl -u slurmdbd -u slurmctld -u slurmd -b --no-pager \
    >"${record_dir}/service-journal.txt"

cat >"${report_file}" <<EOF
# Slurm base configuration

- Run ID: ${run_id}
- Slurm version: $(srun --version)
- Cluster: h100
- Controller/node hostname: ${hostname_expected}
- CPU topology source: slurmd -C
- CPUs: 256
- Boards: 1
- Sockets per board: 2
- Cores per socket: 64
- Threads per core: 2
- Discovered RealMemory: 510953 MiB
- Configured RealMemory: ${configured_memory} MiB
- Reserved for host: ${reserved_memory} MiB
- GPU source: NVML
- GPU detected type: nvidia_h100_pcie
- Scheduled GPU type: h100 (validated substring)
- GPU count: 4
- MIG modified: no
- cgroup: v2
- ConstrainCores/RAM/Swap/Devices: yes
- Accounting storage: SlurmDBD
- AccountingStorageTRES: gres/gpu
- FairShare: priority/multifactor with non-zero fair-share weight
- Configless support: enabled
- Partitions: notebook (default, 8h), train (7d)
- Node state after service start: ${node_state}

Status: SLURM BASE CONFIGURED; ACCOUNTING/QOS ASSOCIATIONS PENDING
EOF

printf '%s\n' 'SLURM BASE CONFIGURED'
printf '%s\n' 'SLURM NODE REMAINS DRAINED'
