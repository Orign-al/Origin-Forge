#!/usr/bin/env bash
set -euo pipefail

readonly platform_root=/srv/gpu-platform/platform
readonly target_csv="${platform_root}/monitoring/dcgm-counters.csv"
readonly image='nvcr.io/nvidia/k8s/dcgm-exporter@sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a'
readonly listen_port=19400
readonly staged_csv="${1:-}"

if [[ -z "${staged_csv}" || ! -f "${staged_csv}" ]]; then
  printf 'Usage: %s /path/to/staged-dcgm-counters.csv\n' "$0" >&2
  exit 2
fi

awk -F, '
  !/^#/ && NF != 3 { print "invalid CSV row", NR, "with", NF, "fields" > "/dev/stderr"; bad=1 }
  END { exit bad }
' "${staged_csv}"

stamp="$(date +%Y%m%d-%H%M%S-%N)"
probe_name="h100-dcgm-probe-${stamp}"
metrics_file="/tmp/${probe_name}.metrics"
report_file="${platform_root}/reports/dcgm-exporter-probe-${stamp}.md"

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

cleanup() {
  sudo docker rm -f "${probe_name}" >/dev/null 2>&1 || true
  sudo rm -f "${metrics_file}"
}
trap cleanup EXIT

if sudo ss -H -lnt "sport = :${listen_port}" | grep -q .; then
  printf 'TCP port %s is already listening; refusing to run probe.\n' "${listen_port}" >&2
  exit 1
fi

if [[ -e "${target_csv}" ]]; then
  cp -a \
    "${target_csv}" \
    "${platform_root}/backups/dcgm-counters-before-probe-${stamp}.csv"
fi

sudo install \
  -o codexops \
  -g gpu-platform-admin \
  -m 0644 \
  "${staged_csv}" \
  "${target_csv}"
sudo rm -f "${staged_csv}"

sudo docker run --detach \
  --name "${probe_name}" \
  --network host \
  --user 65534:65534 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit 256 \
  --memory 1g \
  --cpus 1 \
  --volume "${target_csv}:/etc/dcgm-exporter/h100-counters.csv:ro" \
  --entrypoint /usr/bin/dcgm-exporter \
  "${image}" \
  --disable-startup-validate \
  --address="127.0.0.1:${listen_port}" \
  --remote-hostengine-info=localhost:5555 \
  --collectors=/etc/dcgm-exporter/h100-counters.csv \
  --collect-interval=5000 \
  >/dev/null

ready=no
for _ in $(seq 1 45); do
  if curl -fs "http://127.0.0.1:${listen_port}/metrics" -o "${metrics_file}"; then
    all_metrics_present=yes
    for metric in "${required_metrics[@]}"; do
      if ! grep -Eq "^${metric}(\\{| )" "${metrics_file}"; then
        all_metrics_present=no
        break
      fi
    done
    if [[ "${all_metrics_present}" == yes ]]; then
      ready=yes
      break
    fi
  fi
  sleep 1
done

if [[ "${ready}" != yes ]]; then
  sudo docker logs --tail 100 "${probe_name}" >&2 || true
fi

missing=()
for metric in "${required_metrics[@]}"; do
  if ! grep -Eq "^${metric}(\\{| )" "${metrics_file}"; then
    missing+=("${metric}")
  fi
done

mapfile -t metric_uuids < <(
  grep -E '^DCGM_FI_DEV_GPU_UTIL\{' "${metrics_file}" \
    | sed -n 's/.*UUID="\([^"]*\)".*/\1/p' \
    | sort -u
)
mapfile -t host_uuids < <(
  nvidia-smi --query-gpu=uuid --format=csv,noheader \
    | sed '/^[[:space:]]*$/d' \
    | sort -u
)

if ((${#missing[@]} != 0)); then
  printf 'Missing required metrics:\n' >&2
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi

if ((${#host_uuids[@]} != 4 || ${#metric_uuids[@]} != 4)); then
  printf 'Expected four host and four exported GPU UUIDs; host=%s exporter=%s.\n' \
    "${#host_uuids[@]}" "${#metric_uuids[@]}" >&2
  exit 1
fi

if [[ "$(printf '%s\n' "${host_uuids[@]}")" != "$(printf '%s\n' "${metric_uuids[@]}")" ]]; then
  printf 'Exporter GPU UUIDs do not match host GPU UUIDs.\n' >&2
  exit 1
fi

sudo docker inspect "${probe_name}" | jq -e '
  .[0] as $c
  | $c.Config.User == "65534:65534"
  and $c.HostConfig.Privileged == false
  and ($c.HostConfig.CapDrop | index("ALL") != null)
  and (($c.HostConfig.DeviceRequests // []) | length == 0)
  and (($c.HostConfig.Devices // []) | length == 0)
  and ($c.HostConfig.SecurityOpt | index("no-new-privileges:true") != null)
' >/dev/null

{
  printf '# DCGM Exporter probe %s\n\n' "${stamp}"
  printf -- '- Result: PASSED\n'
  printf -- '- Image: `%s`\n' "${image}"
  printf -- '- Host engine: `localhost:5555`\n'
  printf -- '- Probe listener: `127.0.0.1:%s` (temporary)\n' "${listen_port}"
  printf -- '- Runtime user: `65534:65534`\n'
  printf -- '- Privileged: `false`\n'
  printf -- '- Capabilities: all dropped\n'
  printf -- '- GPU device requests: none\n'
  printf -- '- Exported GPU UUID count: `%s`\n' "${#metric_uuids[@]}"
  printf '\n## GPU UUIDs\n\n'
  printf -- '- `%s`\n' "${metric_uuids[@]}"
  printf '\n## Required metrics observed\n\n'
  printf -- '- `%s`\n' "${required_metrics[@]}"
} >"${report_file}"

printf 'DCGM EXPORTER PROBE PASSED\n'
printf 'Report: %s\n' "${report_file}"
