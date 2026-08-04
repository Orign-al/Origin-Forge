#!/usr/bin/env bash
set -euo pipefail

readonly run_id="${RUN_ID:-20260804-050357}"
readonly platform_root=/srv/gpu-platform/platform
readonly monitoring_dir="${platform_root}/monitoring"
readonly artifacts_dir="${platform_root}/artifacts/monitoring-${run_id}"
readonly secrets_dir="${platform_root}/secrets"
readonly backup_root="${platform_root}/backups"
readonly reports_dir="${platform_root}/reports"
readonly compose_file="${monitoring_dir}/compose.yml"
readonly grafana_archive="${artifacts_dir}/grafana-13.1.1.linux-amd64.tar.gz"
readonly node_archive="${artifacts_dir}/node_exporter-1.12.1.linux-amd64.tar.gz"
readonly grafana_sha256=0c07116968aea49768af8babd3c3f162d19012655a1a220cd7a9d97efe91da6c
readonly node_sha256=b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63
readonly prometheus_image='quay.io/prometheus/prometheus@sha256:1147c92841726a6fef55fe6124491d6f85480f8de204f7d420304ca5bbd0a8f7'
readonly dcgm_image='nvcr.io/nvidia/k8s/dcgm-exporter@sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a'
readonly grafana_image='h100-local/grafana-oss:13.1.1-1'
readonly secret_file="${secrets_dir}/monitoring.env"

stamp="$(date +%Y%m%d-%H%M%S-%N)"
backup_dir="${backup_root}/monitoring-${stamp}"
report_file="${reports_dir}/monitoring-${run_id}.md"
temp_dir=''

cleanup() {
  case "${temp_dir}" in
    /tmp/h100-monitoring-build.*)
      if [[ -d "${temp_dir}" ]]; then
        sudo rm -rf -- "${temp_dir}"
      fi
      ;;
  esac
}
trap cleanup EXIT

fail() {
  printf 'MONITORING BLOCKED: %s\n' "$*" >&2
  exit 1
}

require_file() {
  [[ -f "$1" ]] || fail "required file is missing: $1"
}

verify_sha256() {
  local file=$1
  local expected=$2
  local actual
  actual="$(sha256sum "${file}" | awk '{print $1}')"
  [[ "${actual}" == "${expected}" ]] \
    || fail "checksum mismatch for ${file}"
}

wait_url() {
  local url=$1
  local attempts=${2:-90}
  local i
  for i in $(seq 1 "${attempts}"); do
    if curl -fsS --max-time 5 "${url}" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'

for file in \
  "${compose_file}" \
  "${monitoring_dir}/prometheus.yml" \
  "${monitoring_dir}/dcgm-counters.csv" \
  "${monitoring_dir}/grafana-image.Dockerfile" \
  "${monitoring_dir}/grafana-passwd" \
  "${monitoring_dir}/grafana-group" \
  "${monitoring_dir}/grafana/provisioning/datasources/prometheus.yml" \
  "${monitoring_dir}/node-exporter.service" \
  "${grafana_archive}" \
  "${node_archive}"; do
  require_file "${file}"
done

verify_sha256 "${grafana_archive}" "${grafana_sha256}"
verify_sha256 "${node_archive}" "${node_sha256}"

awk -F, '
  !/^#/ && NF != 3 { print "invalid DCGM CSV row", NR > "/dev/stderr"; bad=1 }
  END { exit bad }
' "${monitoring_dir}/dcgm-counters.csv" \
  || fail 'DCGM collector CSV is invalid'

sudo docker image inspect "${prometheus_image}" >/dev/null \
  || fail 'pinned Prometheus image is not present'
sudo docker image inspect "${dcgm_image}" >/dev/null \
  || fail 'pinned DCGM Exporter image is not present'
systemctl is-active --quiet nvidia-dcgm \
  || fail 'nvidia-dcgm is not active'
sudo ss -H -lnt 'sport = :5555' \
  | awk '{print $4}' \
  | grep -qx '127.0.0.1:5555' \
  || fail 'DCGM hostengine is not restricted to 127.0.0.1:5555'

if sinfo -h -N -o '%T' | grep -Evq '^drain'; then
  fail 'Slurm node is not drained'
fi

for port in 3000 9090 9100 9400; do
  if sudo ss -H -lnt "sport = :${port}" | grep -q .; then
    fail "TCP port ${port} is already in use"
  fi
done

for container in h100-prometheus h100-grafana h100-dcgm-exporter; do
  if sudo docker container inspect "${container}" >/dev/null 2>&1; then
    fail "container already exists: ${container}"
  fi
done

grafana_image_present=no
if sudo docker image inspect "${grafana_image}" >/dev/null 2>&1; then
  sudo docker image inspect "${grafana_image}" \
    | jq -e --arg archive_sha "${grafana_sha256}" '
        .[0].Config.User == "472:0"
        and .[0].Config.Labels["org.opencontainers.image.version"] == "13.1.1"
        and .[0].Config.Labels["h100.grafana.archive.sha256"] == $archive_sha
      ' >/dev/null \
    || fail "existing Grafana image tag is not the expected verified build: ${grafana_image}"
  grafana_image_present=yes
fi

secret_present=no
if [[ -e "${secret_file}" ]]; then
  [[ "$(sudo stat -c '%U:%G %a' "${secret_file}")" == 'root:root 600' ]] \
    || fail 'existing monitoring.env ownership or mode is incorrect'
  sudo awk -F= '
    $1 == "GF_SECURITY_ADMIN_USER" && $2 == "admin" { user_ok++ }
    $1 == "GF_SECURITY_ADMIN_PASSWORD" && length($2) == 64 && $2 !~ /[^0-9a-f]/ { password_ok++ }
    END { exit !(NR == 2 && user_ok == 1 && password_ok == 1) }
  ' "${secret_file}" \
    || fail 'existing monitoring.env structure is invalid'
  secret_present=yes
fi

install -d -m 2770 "${backup_dir}"
if [[ -e /usr/local/bin/node_exporter ]]; then
  sha256sum /usr/local/bin/node_exporter >"${backup_dir}/node_exporter.state"
else
  printf 'ABSENT before deployment\n' >"${backup_dir}/node_exporter.state"
fi
if [[ -e /etc/systemd/system/node-exporter.service ]]; then
  sudo cp -a \
    /etc/systemd/system/node-exporter.service \
    "${backup_dir}/node-exporter.service.before"
else
  printf 'ABSENT before deployment\n' >"${backup_dir}/node-exporter.service.state"
fi
if [[ "${secret_present}" == yes ]]; then
  printf 'PRESENT and validated; content not copied\n' \
    >"${backup_dir}/monitoring.env.state"
else
  printf 'ABSENT before deployment\n' >"${backup_dir}/monitoring.env.state"
fi
printf 'No target monitoring containers existed before deployment.\n' \
  >"${backup_dir}/containers.state"

temp_dir="$(mktemp -d /tmp/h100-monitoring-build.XXXXXX)"

if tar -tzf "${grafana_archive}" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  fail 'Grafana archive contains an unsafe path'
fi
if tar -tzf "${node_archive}" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  fail 'Node Exporter archive contains an unsafe path'
fi

tar --no-same-owner --no-same-permissions \
  -xzf "${grafana_archive}" \
  -C "${temp_dir}"
tar --no-same-owner --no-same-permissions \
  -xzf "${node_archive}" \
  -C "${temp_dir}"

grafana_source="${temp_dir}/grafana-13.1.1"
node_source="${temp_dir}/node_exporter-1.12.1.linux-amd64/node_exporter"
require_file "${grafana_source}/bin/grafana"
require_file "${node_source}"

file "${grafana_source}/bin/grafana" | grep -q 'statically linked' \
  || fail 'Grafana binary is not statically linked'
"${node_source}" --version 2>&1 | grep -q 'version 1.12.1' \
  || fail 'Node Exporter archive version check failed'

install -d -m 0755 "${grafana_source}/.h100-image"
install -m 0644 \
  "${monitoring_dir}/grafana-passwd" \
  "${grafana_source}/.h100-image/passwd"
install -m 0644 \
  "${monitoring_dir}/grafana-group" \
  "${grafana_source}/.h100-image/group"
install -m 0644 \
  /etc/ssl/certs/ca-certificates.crt \
  "${grafana_source}/.h100-image/ca-certificates.crt"
install -m 0644 \
  "${monitoring_dir}/grafana-image.Dockerfile" \
  "${grafana_source}/Dockerfile.h100"

if [[ "${grafana_image_present}" == no ]]; then
  sudo docker build \
    --pull=false \
    --network none \
    --tag "${grafana_image}" \
    --file "${grafana_source}/Dockerfile.h100" \
    "${grafana_source}"
fi

grafana_image_id="$(sudo docker image inspect --format '{{.Id}}' "${grafana_image}")"
[[ -n "${grafana_image_id}" ]] || fail 'Grafana image ID is empty'
sudo docker image inspect "${grafana_image}" \
  | jq -e '.[0].Config.User == "472:0"' >/dev/null \
  || fail 'Grafana image user is not 472:0'

if [[ -e /usr/local/bin/node_exporter ]]; then
  sudo cmp -s "${node_source}" /usr/local/bin/node_exporter \
    || fail 'existing node_exporter differs from the verified 1.12.1 archive'
else
  sudo install -o root -g root -m 0755 \
    "${node_source}" \
    /usr/local/bin/node_exporter
fi
if [[ -e /etc/systemd/system/node-exporter.service ]]; then
  sudo cmp -s \
    "${monitoring_dir}/node-exporter.service" \
    /etc/systemd/system/node-exporter.service \
    || fail 'existing node-exporter.service differs from the current project file'
else
  sudo install -o root -g root -m 0644 \
    "${monitoring_dir}/node-exporter.service" \
    /etc/systemd/system/node-exporter.service
fi

if [[ "${secret_present}" == no ]]; then
  secret_staging="${temp_dir}/monitoring.env"
  umask 077
  {
    printf 'GF_SECURITY_ADMIN_USER=admin\n'
    printf 'GF_SECURITY_ADMIN_PASSWORD='
    openssl rand -hex 32
  } >"${secret_staging}"
  sudo install -o root -g root -m 0600 \
    "${secret_staging}" \
    "${secret_file}"
  umask 022
fi

sudo install -d -o root -g root -m 0755 \
  "${monitoring_dir}/data"
sudo install -d -o root -g root -m 0750 \
  "${monitoring_dir}/data/prometheus" \
  "${monitoring_dir}/data/grafana"
sudo chown 65534:65534 "${monitoring_dir}/data/prometheus"
sudo chown 472:0 "${monitoring_dir}/data/grafana"

sudo systemctl daemon-reload
sudo systemctl enable --now node-exporter.service
systemctl is-active --quiet node-exporter.service \
  || fail 'Node Exporter failed to start'

sudo docker compose \
  --project-directory "${monitoring_dir}" \
  --file "${compose_file}" \
  config --quiet
sudo docker compose \
  --project-directory "${monitoring_dir}" \
  --file "${compose_file}" \
  up --detach --no-build

wait_url http://127.0.0.1:9100/metrics 60 \
  || fail 'Node Exporter endpoint did not become ready'
wait_url http://127.0.0.1:9090/-/ready 90 \
  || fail 'Prometheus endpoint did not become ready'
wait_url http://127.0.0.1:3000/api/health 120 \
  || fail 'Grafana endpoint did not become ready'
wait_url http://127.0.0.1:9400/metrics 90 \
  || fail 'DCGM Exporter endpoint did not become ready'

dcgm_metrics="${temp_dir}/dcgm.metrics"
dcgm_ready=no
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 http://127.0.0.1:9400/metrics -o "${dcgm_metrics}" \
    && grep -q '^DCGM_FI_PROF_PCIE_TX_BYTES{' "${dcgm_metrics}" \
    && grep -q '^DCGM_FI_PROF_PCIE_RX_BYTES{' "${dcgm_metrics}"; then
    dcgm_ready=yes
    break
  fi
  sleep 1
done
[[ "${dcgm_ready}" == yes ]] \
  || fail 'DCGM profiling metrics did not become ready'

required_dcgm_metrics=(
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
for metric in "${required_dcgm_metrics[@]}"; do
  grep -Eq "^${metric}(\\{| )" "${dcgm_metrics}" \
    || fail "DCGM metric is missing: ${metric}"
done

exported_gpu_count="$({
  grep -E '^DCGM_FI_DEV_GPU_UTIL\{' "${dcgm_metrics}" \
    | sed -n 's/.*UUID="\([^"]*\)".*/\1/p' \
    | sort -u
  } | awk 'END {print NR}')"
[[ "${exported_gpu_count}" == 4 ]] \
  || fail "DCGM Exporter reported ${exported_gpu_count} GPU UUIDs instead of 4"

grafana_health="$(curl -fsS http://127.0.0.1:3000/api/health)"
jq -e '.database == "ok"' <<<"${grafana_health}" >/dev/null \
  || fail 'Grafana database health is not ok'

targets_ready=no
targets_json=''
for _ in $(seq 1 60); do
  targets_json="$(curl -fsS http://127.0.0.1:9090/api/v1/targets)"
  if jq -e '
      .status == "success"
      and (.data.activeTargets | length == 3)
      and all(.data.activeTargets[]; .health == "up")
    ' <<<"${targets_json}" >/dev/null; then
    targets_ready=yes
    break
  fi
  sleep 1
done
[[ "${targets_ready}" == yes ]] || fail 'not all three Prometheus targets are UP'

mapfile -t target_jobs < <(
  jq -r '.data.activeTargets[].labels.job' <<<"${targets_json}" | sort -u
)
[[ "$(printf '%s\n' "${target_jobs[@]}")" == $'dcgm-exporter\nnode-exporter\nprometheus' ]] \
  || fail 'Prometheus target job set is unexpected'

for port in 3000 9090 9100 9400; do
  mapfile -t listen_addresses < <(
    sudo ss -H -lnt "sport = :${port}" | awk '{print $4}' | sort -u
  )
  if ((${#listen_addresses[@]} != 1)) \
    || [[ "${listen_addresses[0]}" != "127.0.0.1:${port}" ]]; then
    fail "TCP port ${port} is not restricted to 127.0.0.1"
  fi
done

for container in h100-prometheus h100-grafana h100-dcgm-exporter; do
  [[ "$(sudo docker inspect --format '{{.State.Status}}' "${container}")" == running ]] \
    || fail "container is not running: ${container}"
  sudo docker inspect "${container}" | jq -e '
    .[0] as $c
    | $c.HostConfig.Privileged == false
    and ($c.HostConfig.CapDrop | index("ALL") != null)
    and (($c.HostConfig.DeviceRequests // []) | length == 0)
    and (($c.HostConfig.Devices // []) | length == 0)
    and ($c.HostConfig.SecurityOpt | index("no-new-privileges:true") != null)
  ' >/dev/null || fail "container security validation failed: ${container}"
done

secret_mode="$(sudo stat -c '%U:%G %a' "${secret_file}")"
[[ "${secret_mode}" == 'root:root 600' ]] \
  || fail 'monitoring.env ownership or mode is incorrect'

node_version="$(/usr/local/bin/node_exporter --version 2>&1 | sed -n '1p')"
grafana_version="$(jq -r '.version' <<<"${grafana_health}")"
prometheus_image_id="$(sudo docker image inspect --format '{{.Id}}' "${prometheus_image}")"
dcgm_image_id="$(sudo docker image inspect --format '{{.Id}}' "${dcgm_image}")"

[[ ! -e "${report_file}" ]] || fail "refusing to overwrite report: ${report_file}"
{
  printf '# H100 monitoring deployment\n\n'
  printf -- '- Run ID: `%s`\n' "${run_id}"
  printf -- '- Result: `MONITORING PASSED`\n'
  printf -- '- Prometheus: `3.13.2`\n'
  printf -- '- Prometheus image: `%s`\n' "${prometheus_image}"
  printf -- '- Prometheus image ID: `%s`\n' "${prometheus_image_id}"
  printf -- '- Grafana OSS: `%s`\n' "${grafana_version}"
  printf -- '- Grafana local image: `%s`\n' "${grafana_image}"
  printf -- '- Grafana image ID: `%s`\n' "${grafana_image_id}"
  printf -- '- Grafana official archive SHA-256: `%s`\n' "${grafana_sha256}"
  printf -- '- Node Exporter: `%s`\n' "${node_version}"
  printf -- '- Node Exporter official archive SHA-256: `%s`\n' "${node_sha256}"
  printf -- '- DCGM Exporter: `4.6.0-4.8.3`\n'
  printf -- '- DCGM Exporter image: `%s`\n' "${dcgm_image}"
  printf -- '- DCGM Exporter image ID: `%s`\n' "${dcgm_image_id}"
  printf -- '- Host DCGM: `4.6.1`\n'
  printf -- '- Exported GPU UUID count: `%s`\n' "${exported_gpu_count}"
  printf -- '- Prometheus targets: `prometheus`, `node-exporter`, `dcgm-exporter` all UP\n'
  printf -- '- Grafana: `127.0.0.1:3000`\n'
  printf -- '- Prometheus: `127.0.0.1:9090`\n'
  printf -- '- Node Exporter: `127.0.0.1:9100`\n'
  printf -- '- DCGM Exporter: `127.0.0.1:9400`\n'
  printf -- '- Monitoring containers: non-privileged, all capabilities dropped, no GPU device requests\n'
  printf -- '- Grafana secret file: `%s` (`root:root`, mode `0600`; password not recorded here)\n' "${secret_file}"
  printf -- '- Backup directory: `%s`\n' "${backup_dir}"
  printf -- '- Slurm node remained DRAIN throughout deployment\n'
  printf -- '- MIG was not modified\n'
  printf -- '- Firewall was not modified\n'
} >"${report_file}"

printf 'MONITORING PASSED\n'
printf 'Report: %s\n' "${report_file}"
printf 'Grafana password remains undisclosed in %s\n' "${secret_file}"
