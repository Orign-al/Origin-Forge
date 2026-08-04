#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo only for fixed Docker image pulls and probes.' >&2
    exit 2
fi

run_id=${1:?usage: 38-prepare-monitoring-assets.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly prometheus_version=3.13.2
readonly prometheus_image=quay.io/prometheus/prometheus@sha256:1147c92841726a6fef55fe6124491d6f85480f8de204f7d420304ca5bbd0a8f7
readonly node_exporter_version=1.12.1
readonly grafana_version=13.1.1
readonly dcgm_exporter_version=4.6.0-4.8.3
readonly dcgm_exporter_image=nvcr.io/nvidia/k8s/dcgm-exporter@sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/monitoring-${run_id}
record_dir=${platform_dir}/reports/monitoring-assets-${run_id}.d
report_file=${platform_dir}/reports/monitoring-version-decision-${run_id}.md
node_archive=${artifact_dir}/node_exporter-${node_exporter_version}.linux-amd64.tar.gz
node_sums=${artifact_dir}/node-exporter-sha256sums.txt
node_root=${artifact_dir}/node-exporter-root
grafana_archive=${artifact_dir}/grafana-${grafana_version}.linux-amd64.tar.gz
grafana_sum=${artifact_dir}/grafana-${grafana_version}.linux-amd64.tar.gz.sha256
grafana_root=${artifact_dir}/grafana-root

if [[ -e ${artifact_dir} || -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite monitoring artifacts or preparation records.' >&2
    exit 1
fi
if find "${platform_dir}/monitoring" -mindepth 1 -print -quit | grep -q .; then
    printf '%s\n' 'Unknown monitoring content exists before preparation.' >&2
    exit 1
fi
if [[ -e ${platform_dir}/secrets/monitoring.env ]]; then
    printf '%s\n' 'An unknown monitoring secret file exists.' >&2
    exit 1
fi

sudo -n true
node_state=$(sinfo -h -n sagsh100server -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf '%s\n' 'Slurm node is not drained during monitoring preparation.' >&2
    exit 1
fi
if ss -lntH | awk '{print $4}' | grep -Eq '(^|:)(3000|9090|9100|9400)$'; then
    printf '%s\n' 'A required monitoring port is already listening.' >&2
    exit 1
fi
if sudo docker ps -a --format '{{.Names}}' \
    | grep -Eq '^(h100-prometheus|h100-grafana|h100-dcgm-exporter)$'; then
    printf '%s\n' 'An unknown monitoring container already exists.' >&2
    exit 1
fi

install -d -m 2770 "${artifact_dir}" "${record_dir}"
df -hT "${platform_dir}" >"${record_dir}/filesystem-before.txt"

curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --retry 3 --output "${node_archive}" \
    "https://github.com/prometheus/node_exporter/releases/download/v${node_exporter_version}/node_exporter-${node_exporter_version}.linux-amd64.tar.gz"
curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --retry 3 --output "${node_sums}" \
    "https://github.com/prometheus/node_exporter/releases/download/v${node_exporter_version}/sha256sums.txt"
grep -E "[[:space:]]node_exporter-${node_exporter_version}\\.linux-amd64\\.tar\\.gz$" \
    "${node_sums}" >"${record_dir}/node-exporter-expected-sha256.txt"
(
    cd -- "${artifact_dir}"
    sha256sum --check "${record_dir}/node-exporter-expected-sha256.txt"
) >"${record_dir}/node-exporter-checksum-validation.txt"

curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --retry 3 --output "${grafana_archive}" \
    "https://dl.grafana.com/oss/release/grafana-${grafana_version}.linux-amd64.tar.gz"
curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --retry 3 --output "${grafana_sum}" \
    "https://dl.grafana.com/oss/release/grafana-${grafana_version}.linux-amd64.tar.gz.sha256"
grafana_expected=$(tr -d '[:space:]' <"${grafana_sum}")
if [[ ! ${grafana_expected} =~ ^[0-9a-f]{64}$ ]] \
    || [[ $(sha256sum "${grafana_archive}" | awk '{print $1}') != "${grafana_expected}" ]]; then
    printf '%s\n' 'The Grafana official archive checksum validation failed.' >&2
    exit 1
fi
printf '%s  %s\n' "${grafana_expected}" "$(basename "${grafana_archive}")" \
    >"${record_dir}/grafana-checksum-validation.txt"

curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --retry 3 --output "${artifact_dir}/grafana-official-Dockerfile" \
    "https://raw.githubusercontent.com/grafana/grafana/v${grafana_version}/Dockerfile"
curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --retry 3 --output "${artifact_dir}/grafana-official-run.sh" \
    "https://raw.githubusercontent.com/grafana/grafana/v${grafana_version}/packaging/docker/run.sh"
sha256sum "${artifact_dir}/grafana-official-Dockerfile" \
    "${artifact_dir}/grafana-official-run.sh" >"${record_dir}/grafana-build-reference-sha256.txt"

tar -tzf "${node_archive}" >"${record_dir}/node-exporter-archive-members.txt"
tar -tzf "${grafana_archive}" >"${record_dir}/grafana-archive-members.txt"
for list in "${record_dir}/node-exporter-archive-members.txt" \
    "${record_dir}/grafana-archive-members.txt"; do
    if awk 'index($0, "/../") || $0 ~ /^\.\.\// || $0 ~ /^\// { bad = 1 } END { exit bad }' "${list}"; then
        :
    else
        printf 'Unsafe archive path detected in %s.\n' "${list}" >&2
        exit 1
    fi
done
install -d -m 0750 "${node_root}" "${grafana_root}"
tar -xzf "${node_archive}" -C "${node_root}" --strip-components=1 --no-same-owner
tar -xzf "${grafana_archive}" -C "${grafana_root}" --strip-components=1 --no-same-owner
if [[ ! -x ${node_root}/node_exporter || ! -x ${grafana_root}/bin/grafana \
    || ! -f ${grafana_root}/conf/sample.ini || ! -d ${grafana_root}/public ]]; then
    printf '%s\n' 'An official monitoring archive lacks expected runtime files.' >&2
    exit 1
fi
"${node_root}/node_exporter" --version >"${record_dir}/node-exporter-version.txt" 2>&1
"${grafana_root}/bin/grafana" server -v >"${record_dir}/grafana-version.txt" 2>&1
file "${grafana_root}/bin/grafana" >"${record_dir}/grafana-binary-file.txt"
ldd "${grafana_root}/bin/grafana" >"${record_dir}/grafana-binary-ldd.txt" 2>&1 || true

sudo docker pull --platform linux/amd64 "${prometheus_image}" \
    >"${record_dir}/prometheus-pull.txt" 2>&1
sudo docker pull --platform linux/amd64 "${dcgm_exporter_image}" \
    >"${record_dir}/dcgm-exporter-pull.txt" 2>&1
sudo docker image inspect "${prometheus_image}" \
    --format '{{.Id}} {{json .RepoDigests}}' >"${record_dir}/prometheus-image.txt"
sudo docker image inspect "${dcgm_exporter_image}" \
    --format '{{.Id}} {{json .RepoDigests}}' >"${record_dir}/dcgm-exporter-image.txt"
sudo docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true "${prometheus_image}" --version \
    >"${record_dir}/prometheus-version.txt" 2>&1
sudo docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true "${dcgm_exporter_image}" --help \
    >"${record_dir}/dcgm-exporter-help.txt" 2>&1
sudo docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true "${dcgm_exporter_image}" --version \
    >"${record_dir}/dcgm-exporter-version.txt" 2>&1
if ! grep -Fq "${prometheus_version}" "${record_dir}/prometheus-version.txt" \
    || ! grep -Fq "${dcgm_exporter_version##*-}" "${record_dir}/dcgm-exporter-version.txt"; then
    printf '%s\n' 'A prepared monitoring image did not report the selected version.' >&2
    exit 1
fi
if ! grep -Eq -- '(^|[[:space:]])-a([,[:space:]]|$)|--address' \
    "${record_dir}/dcgm-exporter-help.txt"; then
    printf '%s\n' 'DCGM Exporter help lacks a reviewed listen-address option.' >&2
    exit 1
fi

node_sha=$(awk '{print $1}' "${record_dir}/node-exporter-expected-sha256.txt")
grafana_binary_type=$(cut -d: -f2- "${record_dir}/grafana-binary-file.txt" | sed 's/^ //')
cat >"${report_file}" <<EOF
# Monitoring version decision and asset preparation

- Run ID: ${run_id}
- Prometheus: v${prometheus_version}
- Prometheus official release: https://github.com/prometheus/prometheus/releases/tag/v${prometheus_version}
- Prometheus image: ${prometheus_image}
- Node Exporter: v${node_exporter_version}
- Node Exporter official release: https://github.com/prometheus/node_exporter/releases/tag/v${node_exporter_version}
- Node Exporter archive SHA-256: ${node_sha}
- Grafana OSS: v${grafana_version}
- Grafana official release: https://github.com/grafana/grafana/releases/tag/v${grafana_version}
- Grafana official archive SHA-256: ${grafana_expected}
- Grafana binary type: ${grafana_binary_type}
- Grafana official Docker Hub image: unreachable because registry-1.docker.io timed out
- Grafana plan: locally build from the checksum-verified official archive using a separately pinned reachable base; do not use a mirror
- DCGM Exporter: ${dcgm_exporter_version}
- DCGM Exporter official release: https://github.com/NVIDIA/dcgm-exporter/releases/tag/${dcgm_exporter_version}
- DCGM Exporter image: ${dcgm_exporter_image}
- DCGM runtime included by exporter: 4.6.0
- Host DCGM: 4.6.1
- NVIDIA documented container capability: SYS_ADMIN
- Privileged container required: no
- Services started: no
- Ports opened: no
- Node state: ${node_state}

Status: MONITORING ASSETS PREPARED
EOF

printf 'NODE_EXPORTER_SHA256=%s\n' "${node_sha}"
printf 'GRAFANA_ARCHIVE_SHA256=%s\n' "${grafana_expected}"
printf '%s\n' 'MONITORING ASSETS PREPARED'
