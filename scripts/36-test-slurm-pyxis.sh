#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo only for controlled node state and cgroup inspection.' >&2
    exit 2
fi

run_id=${1:?usage: 36-test-slurm-pyxis.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly node=sagsh100server
readonly image_tag=nvcr.io/nvidia/cuda:13.2.0-base-ubuntu24.04
readonly image_digest=sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a
readonly image_ref=nvcr.io#nvidia/cuda:13.2.0-base-ubuntu24.04@sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
record_dir=${platform_dir}/reports/pyxis-enroot-test-${run_id}.d
report_file=${platform_dir}/reports/pyxis-enroot-test-${run_id}.md
lock_file=${platform_dir}/.pyxis-enroot-test.lock
cpu_name=h100-bootstrap-cpu-${run_id}
gpu1_name=h100-bootstrap-gpu1-${run_id}
gpu2_name=h100-bootstrap-gpu2-${run_id}
resume_performed=0
active_jobid=
active_launcher_pid=
final_reason='platform bootstrap Pyxis test failed'
last_jobid=

cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    set +e
    if (( rc != 0 )); then
        if [[ ${active_jobid} =~ ^[0-9]+$ ]]; then
            scancel "${active_jobid}" >/dev/null 2>&1 || true
        fi
        for name in "${cpu_name}" "${gpu1_name}" "${gpu2_name}"; do
            scancel --user=codexops --name="${name}" >/dev/null 2>&1 || true
        done
        if [[ ${active_launcher_pid} =~ ^[0-9]+$ ]]; then
            kill "${active_launcher_pid}" >/dev/null 2>&1 || true
        fi
    fi
    if (( resume_performed == 1 )); then
        sudo scontrol update NodeName="${node}" State=DRAIN Reason="${final_reason}" \
            >/dev/null 2>&1 || true
        if [[ -d ${record_dir} ]]; then
            sinfo -h -n "${node}" -o '%N %T %E' >"${record_dir}/cleanup-node-state.txt" 2>&1 || true
            printf 'exit_code=%s\nreason=%s\n' "${rc}" "${final_reason}" \
                >"${record_dir}/cleanup-result.txt"
        fi
    fi
    exit "${rc}"
}
trap cleanup EXIT INT TERM

wait_for_jobid() {
    local name=$1 candidate=
    local attempt
    for attempt in $(seq 1 60); do
        candidate=$(squeue -h -u codexops -n "${name}" -o '%A' | sed -n '1p')
        if [[ ${candidate} =~ ^[0-9]+$ ]]; then
            last_jobid=${candidate}
            return 0
        fi
        sleep 1
    done
    printf 'Timed out waiting for Slurm job ID: %s\n' "${name}" >&2
    return 1
}

wait_for_running() {
    local jobid=$1 state=
    local attempt
    for attempt in $(seq 1 120); do
        state=$(squeue -h -j "${jobid}" -o '%T' | sed -n '1p')
        if [[ ${state} == RUNNING ]]; then
            return 0
        fi
        if [[ -z ${state} ]]; then
            printf 'Job disappeared before RUNNING: %s\n' "${jobid}" >&2
            return 1
        fi
        sleep 1
    done
    printf 'Timed out waiting for job RUNNING: %s\n' "${jobid}" >&2
    return 1
}

capture_running_job() {
    local label=$1 jobid=$2
    local cgroup_file=${record_dir}/${label}-cgroup-paths.txt
    local attempt cgroup_path

    squeue -j "${jobid}" -o '%.18i %.32j %.10T %.10M %.6D %.8C %.12m %.20b %.16R' \
        >"${record_dir}/${label}-squeue-running.txt"
    if ! grep -Eq "^[[:space:]]*${jobid}[[:space:]]" \
        "${record_dir}/${label}-squeue-running.txt"; then
        printf 'squeue did not expose the running test job: %s\n' "${jobid}" >&2
        return 1
    fi
    scontrol show job -dd "${jobid}" >"${record_dir}/${label}-scontrol-job-running.txt"

    for attempt in $(seq 1 20); do
        if scontrol show step "${jobid}.0" \
            >"${record_dir}/${label}-scontrol-step-running.txt" 2>&1; then
            break
        fi
        sleep 1
    done
    scontrol listpids "${jobid}" >"${record_dir}/${label}-listpids.txt" 2>&1 || true
    sstat -j "${jobid}.0" --noheader --parsable2 \
        --format=JobID,AveCPU,AveRSS,MaxRSS,MaxVMSize,MaxPages \
        >"${record_dir}/${label}-sstat-running.txt" 2>&1 || true

    for attempt in $(seq 1 20); do
        sudo find /sys/fs/cgroup -type d -path "*job_${jobid}*" -print \
            >"${cgroup_file}"
        if [[ -s ${cgroup_file} ]]; then
            break
        fi
        sleep 1
    done
    if [[ ! -s ${cgroup_file} ]]; then
        printf 'No cgroup v2 path found for running job: %s\n' "${jobid}" >&2
        return 1
    fi
    while IFS= read -r cgroup_path; do
        sudo stat -c '%F %U:%G %a %n' "${cgroup_path}"
        for file in cgroup.procs cpuset.cpus.effective cpuset.mems.effective memory.current memory.max; do
            if sudo test -f "${cgroup_path}/${file}"; then
                printf '%s/%s=' "${cgroup_path}" "${file}"
                sudo tr '\n' ',' <"${cgroup_path}/${file}"
                printf '\n'
            fi
        done
    done <"${cgroup_file}" >"${record_dir}/${label}-cgroup-details.txt"
}

wait_for_accounting() {
    local label=$1 jobid=$2 state=
    local attempt
    for attempt in $(seq 1 30); do
        state=$(sacct -X -nP -j "${jobid}" -o State | sed -n '1p')
        if [[ ${state} == COMPLETED ]]; then
            break
        fi
        sleep 1
    done
    sacct -j "${jobid}" --parsable2 \
        --format=JobIDRaw,JobName,Partition,Account,QOS,State,Elapsed,AllocCPUS,ReqTRES,AllocTRES,ExitCode \
        >"${record_dir}/${label}-sacct.txt"
    if [[ ${state} != COMPLETED ]]; then
        printf 'Accounting did not report COMPLETED for job %s (state=%s).\n' \
            "${jobid}" "${state}" >&2
        return 1
    fi
}

run_test_job() {
    local label=$1 name=$2 expected_gpu_count=$3
    shift 3
    local launcher_rc gpu_count

    "$@" >"${record_dir}/${label}-stdout.txt" \
        2>"${record_dir}/${label}-stderr.txt" &
    active_launcher_pid=$!
    wait_for_jobid "${name}"
    active_jobid=${last_jobid}
    printf '%s=%s\n' "${label}" "${active_jobid}" >>"${record_dir}/job-ids.txt"
    wait_for_running "${active_jobid}"
    capture_running_job "${label}" "${active_jobid}"

    launcher_rc=0
    wait "${active_launcher_pid}" || launcher_rc=$?
    active_launcher_pid=
    if (( launcher_rc != 0 )); then
        printf 'srun failed for %s with exit code %s.\n' "${label}" "${launcher_rc}" >&2
        return 1
    fi
    wait_for_accounting "${label}" "${active_jobid}"
    if squeue -h -j "${active_jobid}" | grep -q .; then
        printf 'Job remains in squeue after completion: %s\n' "${active_jobid}" >&2
        return 1
    fi
    if (( expected_gpu_count >= 0 )); then
        gpu_count=$(grep -Ec '^GPU [0-9]+:' "${record_dir}/${label}-stdout.txt" || true)
        if [[ ${gpu_count} -ne ${expected_gpu_count} ]]; then
            printf 'GPU visibility mismatch for %s: expected=%s actual=%s\n' \
                "${label}" "${expected_gpu_count}" "${gpu_count}" >&2
            return 1
        fi
        nvidia-smi --query-compute-apps=pid,gpu_uuid,process_name,used_memory \
            --format=csv,noheader >"${record_dir}/${label}-host-compute-after.txt" 2>/dev/null || true
        if [[ -s ${record_dir}/${label}-host-compute-after.txt ]]; then
            printf 'GPU compute process remains after %s.\n' "${label}" >&2
            return 1
        fi
    fi
    active_jobid=
}

if [[ -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite Slurm + Pyxis test records.' >&2
    exit 1
fi
if [[ ! -f ${platform_dir}/reports/pyxis-install-${run_id}.md ]] \
    || ! grep -Fxq 'Status: PYXIS INSTALL PASSED' \
        "${platform_dir}/reports/pyxis-install-${run_id}.md"; then
    printf '%s\n' 'A passed Pyxis installation report is required.' >&2
    exit 1
fi

exec 9>"${lock_file}"
if ! flock -n 9; then
    printf '%s\n' 'Another Slurm + Pyxis test holds the platform lock.' >&2
    exit 1
fi
install -d -m 2770 "${record_dir}"
sudo -n true

date --iso-8601=seconds >"${record_dir}/started-at.txt"
scontrol show node "${node}" >"${record_dir}/node-before.txt"
sinfo -h -n "${node}" -o '%N %T %E' >"${record_dir}/sinfo-before.txt"
squeue >"${record_dir}/squeue-before.txt"
if [[ $(sinfo -h -n "${node}" -o '%T') != drain* ]] \
    && [[ $(sinfo -h -n "${node}" -o '%T') != drained* ]]; then
    printf '%s\n' 'Slurm node is not drained before the approved test.' >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'The Slurm queue is not empty before the approved test.' >&2
    exit 1
fi
if nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -q .; then
    printf '%s\n' 'A GPU compute process exists before the approved test.' >&2
    exit 1
fi
for service in munge slurmdbd slurmctld slurmd; do
    if [[ $(systemctl is-active "${service}") != active ]]; then
        printf 'Required service is not active: %s\n' "${service}" >&2
        exit 1
    fi
done
if [[ $(nvidia-smi -L | grep -c '^GPU ') -ne 4 ]]; then
    printf '%s\n' 'The host does not expose exactly four GPUs before testing.' >&2
    exit 1
fi
if [[ $(enroot version) != 4.2.1 || $(srun --version) != 'slurm 25.11.7' ]]; then
    printf '%s\n' 'The validated Enroot or Slurm version changed.' >&2
    exit 1
fi
if ! srun --help 2>&1 | grep -Fq -- '--container-image'; then
    printf '%s\n' 'Pyxis options are unavailable before testing.' >&2
    exit 1
fi

printf 'tag=%s\ndigest=%s\npyxis_ref=%s\n' \
    "${image_tag}" "${image_digest}" "${image_ref}" >"${record_dir}/image-reference.txt"
nvidia-smi -L >"${record_dir}/gpu-before.txt"
nvidia-smi --query-gpu=index,uuid,name,pci.bus_id,memory.total,temperature.gpu,power.draw \
    --format=csv,noheader >"${record_dir}/gpu-health-before.txt"
sacctmgr -nP show assoc where user=codexops \
    format=User,Account,Cluster,DefaultQOS,QOS >"${record_dir}/assoc-before.txt"
sacctmgr -nP show qos where name=admin format=Name,MaxTRESPU \
    >"${record_dir}/qos-before.txt"

resume_performed=1
sudo scontrol update NodeName="${node}" State=RESUME
for attempt in $(seq 1 30); do
    node_state=$(sinfo -h -n "${node}" -o '%T')
    [[ ${node_state} == idle ]] && break
    sleep 1
done
if [[ ${node_state} != idle ]]; then
    printf 'Node failed to reach IDLE after approved RESUME: %s\n' "${node_state}" >&2
    exit 1
fi
sinfo -h -n "${node}" -o '%N %T %E' >"${record_dir}/sinfo-after-resume.txt"

run_test_job cpu "${cpu_name}" -1 \
    srun --partition=notebook --account=platform-admin --qos=admin \
    --job-name="${cpu_name}" --nodes=1 --ntasks=1 --cpus-per-task=1 \
    --mem=1G --time=00:05:00 \
    /bin/bash -lc 'hostname -s; printf "SLURM_JOB_ID=%s\n" "${SLURM_JOB_ID}"; cat /proc/self/cgroup; sleep 15'
cpu_jobid=${last_jobid}
if ! grep -Fxq "${node}" "${record_dir}/cpu-stdout.txt"; then
    printf '%s\n' 'CPU test did not return the expected hostname.' >&2
    exit 1
fi

run_test_job gpu1 "${gpu1_name}" 1 \
    srun --partition=notebook --account=platform-admin --qos=admin \
    --job-name="${gpu1_name}" --nodes=1 --ntasks=1 --cpus-per-task=2 \
    --mem=4G --gres=gpu:h100:1 --time=00:10:00 --mpi=none \
    --container-image="${image_ref}" --no-container-mount-home \
    /bin/sh -lc 'printf "CUDA_VISIBLE_DEVICES=%s\n" "${CUDA_VISIBLE_DEVICES-UNSET}"; printf "SLURM_JOB_GPUS=%s\n" "${SLURM_JOB_GPUS-UNSET}"; nvidia-smi -L; nvidia-smi --query-gpu=index,uuid,name,memory.total --format=csv,noheader; cat /proc/self/cgroup; sleep 15'
gpu1_jobid=${last_jobid}

run_test_job gpu2 "${gpu2_name}" 2 \
    srun --partition=notebook --account=platform-admin --qos=admin \
    --job-name="${gpu2_name}" --nodes=1 --ntasks=1 --cpus-per-task=4 \
    --mem=8G --gres=gpu:h100:2 --time=00:10:00 --mpi=none \
    --container-image="${image_ref}" --no-container-mount-home \
    /bin/sh -lc 'printf "CUDA_VISIBLE_DEVICES=%s\n" "${CUDA_VISIBLE_DEVICES-UNSET}"; printf "SLURM_JOB_GPUS=%s\n" "${SLURM_JOB_GPUS-UNSET}"; nvidia-smi -L; nvidia-smi --query-gpu=index,uuid,name,memory.total --format=csv,noheader; cat /proc/self/cgroup; sleep 15'
gpu2_jobid=${last_jobid}

final_reason='platform bootstrap complete'
sudo scontrol update NodeName="${node}" State=DRAIN Reason="${final_reason}"
for attempt in $(seq 1 30); do
    final_state=$(sinfo -h -n "${node}" -o '%T')
    if [[ ${final_state} == drain* || ${final_state} == drained* ]]; then
        break
    fi
    sleep 1
done
if [[ ${final_state} != drain* && ${final_state} != drained* ]]; then
    printf 'Node did not return to DRAIN after testing: %s\n' "${final_state}" >&2
    exit 1
fi
resume_performed=0

sinfo >"${record_dir}/sinfo-final.txt"
squeue >"${record_dir}/squeue-final.txt"
scontrol show node "${node}" >"${record_dir}/node-final.txt"
sacct -j "${cpu_jobid},${gpu1_jobid},${gpu2_jobid}" --parsable2 \
    --format=JobIDRaw,JobName,Partition,Account,QOS,State,Elapsed,AllocCPUS,ReqTRES,AllocTRES,ExitCode \
    >"${record_dir}/sacct-all-tests.txt"
nvidia-smi -L >"${record_dir}/gpu-final.txt"
nvidia-smi --query-compute-apps=pid,gpu_uuid,process_name,used_memory \
    --format=csv,noheader >"${record_dir}/gpu-compute-final.txt" 2>/dev/null || true
if [[ -s ${record_dir}/gpu-compute-final.txt ]] || [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Resources were not fully released after the test suite.' >&2
    exit 1
fi
date --iso-8601=seconds >"${record_dir}/finished-at.txt"

gpu1_uuid=$(grep -m1 '^GPU [0-9]\+:' "${record_dir}/gpu1-stdout.txt" | sed -E 's/.*UUID: ([^)]+).*/\1/')
gpu2_uuids=$(grep '^GPU [0-9]\+:' "${record_dir}/gpu2-stdout.txt" | sed -E 's/.*UUID: ([^)]+).*/\1/' | paste -sd, -)
cat >"${report_file}" <<EOF
# Slurm + Pyxis + Enroot administrator tests

- Run ID: ${run_id}
- Approved temporary RESUME: yes
- Node: ${node}
- Slurm: $(srun --version)
- Pyxis: v0.24.0
- Enroot: $(enroot version)
- Image tag: ${image_tag}
- Image digest: ${image_digest}
- Pyxis digest reference: ${image_ref}
- CPU job ID: ${cpu_jobid}
- CPU job result: COMPLETED; hostname=${node}
- Single-GPU job ID: ${gpu1_jobid}
- Single-GPU visibility: exactly 1 GPU
- Single-GPU UUID: ${gpu1_uuid}
- Dual-GPU job ID: ${gpu2_jobid}
- Dual-GPU visibility: exactly 2 GPUs
- Dual-GPU UUIDs: ${gpu2_uuids}
- squeue visibility during each job: passed
- cgroup v2 job paths during each job: observed
- Accounting records: COMPLETED with ExitCode 0:0
- GPU processes after each job: none
- Queue after tests: empty
- Final node state: ${final_state}
- Final drain reason: ${final_reason}

Status: PYXIS ENROOT PASSED
EOF

printf '%s\n' 'PYXIS ENROOT PASSED'
