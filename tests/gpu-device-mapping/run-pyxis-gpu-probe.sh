#!/usr/bin/env bash

set -euo pipefail

readonly expected_gpus="${GPU_PROBE_EXPECTED_GPUS:-1}"
readonly context_hold="${GPU_PROBE_CONTEXT_HOLD_SECONDS:-5}"
readonly post_sleep="${GPU_PROBE_POST_SLEEP_SECONDS:-0}"

printf '=== ENV ===\n'
env | grep -E '^(SLURM|CUDA|NVIDIA)' | sort || true
printf '=== CGROUP ===\n'
cat /proc/self/cgroup
printf '=== DEVICE NODES ===\n'
find /dev -maxdepth 2 \
  \( -name 'nvidia*' -o -path '/dev/nvidia-caps/*' \) \
  -print | sort -V
printf '=== NVIDIA SMI UUID/PCI ===\n'
nvidia-smi \
  --query-gpu=index,uuid,pci.bus_id,name,memory.total \
  --format=csv,noheader,nounits

/opt/gpu-tests/gpu-device-context-probe \
  --expect-allocated "${expected_gpus}" \
  --assert-slurm-isolation \
  --expect-cuda success \
  --hold-seconds "${context_hold}"

if (( post_sleep > 0 )); then
  printf 'GPU_PROBE_POST_SLEEP_SECONDS=%s\n' "${post_sleep}"
  sleep "${post_sleep}"
fi
