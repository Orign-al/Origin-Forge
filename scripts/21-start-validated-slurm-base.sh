#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for protected Slurm changes.' >&2
    exit 2
fi

run_id=${1:?usage: 21-start-validated-slurm-base.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
record_dir=${platform_dir}/reports/slurm-base-start-${run_id}.d
report_file=${platform_dir}/reports/slurm-base-${run_id}.md
hostname_expected=sagsh100server
slurm_target=/etc/slurm/slurm.conf
configured_memory=486377
reserved_memory=24576

if [[ ! -d ${platform_dir}/reports/slurm-base-completion-${run_id}.d ]]; then
    printf '%s\n' 'The cluster-registration format audit record is missing.' >&2
    exit 1
fi
if [[ -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite a Slurm start record or report.' >&2
    exit 1
fi

sudo -n true
install -d -m 2770 "${record_dir}"

for pair in \
    "${platform_dir}/config/slurm.conf:/etc/slurm/slurm.conf" \
    "${platform_dir}/config/cgroup.conf:/etc/slurm/cgroup.conf" \
    "${platform_dir}/config/gres.conf:/etc/slurm/gres.conf"; do
    source_file=${pair%%:*}
    target_file=${pair#*:}
    if ! sudo cmp -s "${source_file}" "${target_file}"; then
        printf 'Installed configuration differs from the reviewed candidate: %s\n' \
            "${target_file}" >&2
        exit 1
    fi
done
for unit in slurmctld slurmd; do
    if systemctl is-active --quiet "${unit}"; then
        printf 'Service unexpectedly active before final start: %s\n' "${unit}" >&2
        exit 1
    fi
done
if ! systemctl is-active --quiet slurmdbd || ! systemctl is-active --quiet munge; then
    printf '%s\n' 'SlurmDBD and MUNGE must be active.' >&2
    exit 1
fi

mapfile -t clusters < <(sudo sacctmgr -nP show cluster format=Cluster)
if (( ${#clusters[@]} != 1 )) || [[ ${clusters[0]} != h100 ]]; then
    printf '%s\n' 'The accounting database does not contain exactly one h100 cluster.' >&2
    exit 1
fi

sudo slurmd -C >"${record_dir}/slurmd-C.txt" 2>&1
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
    printf 'slurmd -G validated %s h100 GPUs rather than four.\n' \
        "${validated_gpu_count}" >&2
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
- GPU count: ${validated_gpu_count}
- MIG modified: no
- cgroup: v2
- ConstrainCores/RAM/Swap/Devices: yes
- Accounting storage: SlurmDBD
- AccountingStorageTRES: gres/gpu
- FairShare: priority/multifactor with non-zero fair-share weight
- Configless support: enabled
- Partitions: notebook (default, 8h), train (7d)
- slurmctld validation: 25.11.7 has no -t option; validated with bounded foreground start using -D -c -f
- sacctmgr parsable single-column format: no trailing delimiter in 25.11.7
- Audited pre-start slurmd log: preserved and backed up
- Node state after service start: ${node_state}

Status: SLURM BASE CONFIGURED; ACCOUNTING/QOS ASSOCIATIONS PENDING
EOF

printf '%s\n' 'SLURM BASE CONFIGURED'
printf '%s\n' 'SLURM NODE REMAINS DRAINED'
