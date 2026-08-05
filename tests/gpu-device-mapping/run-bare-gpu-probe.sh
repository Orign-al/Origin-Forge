#!/usr/bin/env bash

set -euo pipefail

readonly test_dir=/srv/gpu-platform/platform/tests/gpu-device-mapping
readonly expected_gpus="${GPU_PROBE_EXPECTED_GPUS:-1}"
readonly context_hold="${GPU_PROBE_CONTEXT_HOLD_SECONDS:-5}"
readonly post_sleep="${GPU_PROBE_POST_SLEEP_SECONDS:-0}"

printf '=== ENV ===\n'
env | grep -E '^(SLURM|CUDA|NVIDIA)' | sort || true
printf '=== CGROUP ===\n'
cat /proc/self/cgroup
printf '=== NVIDIA SMI UUID/PCI ===\n'
nvidia-smi \
  --query-gpu=index,uuid,pci.bus_id,name,memory.total \
  --format=csv,noheader,nounits

python3 "${test_dir}/gpu-open-probe.py" \
  --expect-allocated-count "${expected_gpus}" \
  --assert-slurm-isolation

python3 "${test_dir}/cuda-context-probe.py" \
  --expect success \
  --hold-seconds "${context_hold}"

if (( post_sleep > 0 )); then
  printf 'GPU_PROBE_POST_SLEEP_SECONDS=%s\n' "${post_sleep}"
  sleep "${post_sleep}"
fi
