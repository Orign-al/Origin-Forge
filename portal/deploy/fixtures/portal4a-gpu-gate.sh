#!/bin/sh
set -eu

actual_uid="$(id -u)"
actual_user="$(id -un)"
test "${actual_uid}" = "20001"
test "${actual_user}" = "origin-pilot"

gpu_inventory="$(nvidia-smi -L)"
gpu_count="$(printf '%s\n' "${gpu_inventory}" | grep -c '^GPU ')"
test "${gpu_count}" = "1"
printf '%s\n' "${gpu_inventory}"
printf 'PORTAL4A_GPU_GATE_PASS uid=%s user=%s gpu_count=%s\n' \
  "${actual_uid}" "${actual_user}" "${gpu_count}"
