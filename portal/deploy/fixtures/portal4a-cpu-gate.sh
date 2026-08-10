#!/bin/sh
set -eu

actual_uid="$(id -u)"
actual_user="$(id -un)"
test "${actual_uid}" = "20001"
test "${actual_user}" = "origin-pilot"
printf 'PORTAL4A_CPU_GATE_PASS uid=%s user=%s\n' "${actual_uid}" "${actual_user}"
