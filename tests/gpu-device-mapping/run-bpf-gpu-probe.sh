#!/usr/bin/env bash

set -euo pipefail

readonly test_dir=/srv/gpu-platform/platform/tests/gpu-device-mapping
readonly report_dir="${GPU_PROBE_REPORT_DIR:?GPU_PROBE_REPORT_DIR is required}"
readonly post_sleep="${GPU_PROBE_POST_SLEEP_SECONDS:-130}"
readonly strace_log="${report_dir}/auto-bpf-open-strace-${SLURM_JOB_ID}.log"

printf '=== ENV ===\n'
env | grep -E '^(SLURM|CUDA|NVIDIA|GPU_PROBE)' | sort || true
printf '=== CGROUP ===\n'
cat /proc/self/cgroup

strace \
  -f \
  -yy \
  -e trace=open,openat,openat2,ioctl \
  -o "${strace_log}" \
  python3 "${test_dir}/gpu-open-probe.py" \
    --expect-allocated-count 1 \
    --assert-slurm-isolation

python3 "${test_dir}/cuda-context-probe.py" \
  --expect success \
  --hold-seconds 5

printf 'STRACE_LOG=%s\n' "${strace_log}"
printf 'GPU_PROBE_POST_SLEEP_SECONDS=%s\n' "${post_sleep}"
sleep "${post_sleep}"
