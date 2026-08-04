#!/usr/bin/env bash
set -euo pipefail

metrics_file="$(mktemp /tmp/h100-dcgm-verify.XXXXXX)"
node_metrics_file="$(mktemp /tmp/h100-node-verify.XXXXXX)"

cleanup() {
  rm -f -- "${metrics_file}" "${node_metrics_file}"
}
trap cleanup EXIT

printf 'Endpoints\n'
curl -fsS http://127.0.0.1:9090/-/ready
printf '\n'
curl -fsS http://127.0.0.1:3000/api/health \
  | jq -c '{commit,database,version}'
curl -fsS http://127.0.0.1:9100/metrics -o "${node_metrics_file}"
grep '^node_exporter_build_info' "${node_metrics_file}"
curl -fsS http://127.0.0.1:9400/metrics -o "${metrics_file}"

printf 'DCGM GPU UUID count: '
grep '^DCGM_FI_DEV_GPU_UTIL{' "${metrics_file}" \
  | sed -n 's/.*UUID="\([^"]*\)".*/\1/p' \
  | sort -u \
  | awk 'END {print NR}'

required_metrics=(
  DCGM_FI_DEV_MIG_MODE
  DCGM_FI_DEV_GPU_UTIL
  DCGM_FI_DEV_FB_USED
  DCGM_FI_DEV_GPU_TEMP
  DCGM_FI_DEV_POWER_USAGE
  DCGM_FI_DEV_ECC_SBE_VOL_TOTAL
  DCGM_FI_DEV_ECC_DBE_VOL_TOTAL
  DCGM_FI_DEV_ECC_SBE_AGG_TOTAL
  DCGM_FI_DEV_ECC_DBE_AGG_TOTAL
  DCGM_FI_DEV_PCIE_REPLAY_COUNTER
  DCGM_FI_PROF_PCIE_TX_BYTES
  DCGM_FI_PROF_PCIE_RX_BYTES
)
for metric in "${required_metrics[@]}"; do
  grep -q "^${metric}{" "${metrics_file}"
done
printf 'Required DCGM metrics: present\n'

printf 'Prometheus targets\n'
curl -fsS http://127.0.0.1:9090/api/v1/targets \
  | jq -r '.data.activeTargets[] | [.labels.job,.health,.lastError] | @tsv' \
  | sort

printf 'Listeners\n'
sudo ss -H -lntp \
  | awk '$4 ~ /^127\.0\.0\.1:(3000|9090|9100|9400)$/ {print $4}' \
  | sort

printf 'Containers\n'
sudo docker ps \
  --filter name=h100- \
  --format '{{.Names}}|{{.Status}}|{{.Ports}}' \
  | sort
for container in h100-prometheus h100-grafana h100-dcgm-exporter; do
  sudo docker inspect "${container}" \
    | jq -r '
        .[0]
        | [
            .Name,
            .Config.User,
            (.HostConfig.Privileged | tostring),
            (.HostConfig.CapDrop | join(",")),
            ((.HostConfig.DeviceRequests // []) | length | tostring),
            .HostConfig.NetworkMode
          ]
        | @tsv
      '
done

printf 'Node Exporter\n'
systemctl is-enabled node-exporter.service
systemctl is-active node-exporter.service

printf 'Secret metadata only\n'
sudo stat -c '%U:%G %a %s bytes' \
  /srv/gpu-platform/platform/secrets/monitoring.env

printf 'Slurm\n'
sinfo -h -N -o '%N|%T|%E'

printf 'Failed units\n'
systemctl --failed --no-legend || true

printf 'Monitoring errors\n'
sudo journalctl \
  -u node-exporter.service \
  -b \
  -p err \
  --no-pager
for container in h100-prometheus h100-grafana h100-dcgm-exporter; do
  sudo docker logs --since 15m "${container}" 2>&1 \
    | grep -Ei 'level=(error|critical)|fatal|panic' \
    | tail -20 \
    || true
done
