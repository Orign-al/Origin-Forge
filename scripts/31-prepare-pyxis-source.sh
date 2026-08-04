#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; source preparation is unprivileged.' >&2
    exit 2
fi

run_id=${1:?usage: 31-prepare-pyxis-source.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly pyxis_tag=v0.24.0
readonly pyxis_version=0.24.0
readonly pyxis_commit=107519944221822ea1dace4db8e7234b2eaa4cd5
readonly repository=https://github.com/NVIDIA/pyxis.git
readonly release_url=https://github.com/NVIDIA/pyxis/releases/tag/v0.24.0
readonly archive_url=https://github.com/NVIDIA/pyxis/archive/refs/tags/v0.24.0.tar.gz
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/pyxis-${pyxis_version}-${run_id}
source_dir=${artifact_dir}/source
archive=${artifact_dir}/pyxis-${pyxis_version}.tar.gz
record_dir=${platform_dir}/reports/pyxis-source-${run_id}.d
report_file=${platform_dir}/reports/pyxis-version-decision-${run_id}.md

if [[ ! -f ${platform_dir}/reports/enroot-install-${run_id}.md ]] \
    || ! grep -Fxq 'Status: ENROOT PASSED' \
        "${platform_dir}/reports/enroot-install-${run_id}.md"; then
    printf '%s\n' 'A passed Enroot report is required before Pyxis preparation.' >&2
    exit 1
fi
if [[ -e ${artifact_dir} || -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite Pyxis source artifacts or records.' >&2
    exit 1
fi

sudo -n true
node_state=$(sinfo -h -n sagsh100server -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf '%s\n' 'Slurm node is not drained.' >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly during Pyxis source preparation.' >&2
    exit 1
fi
if [[ $(srun --version) != 'slurm 25.11.7' ]]; then
    printf '%s\n' 'The deployed Slurm version changed unexpectedly.' >&2
    exit 1
fi
if [[ $(dpkg-query -W -f='${Version}' slurm-smd-dev) != 25.11.7-1h100.1 ]] \
    || [[ ! -f /usr/include/slurm/spank.h ]]; then
    printf '%s\n' 'The exact local Slurm development headers are unavailable.' >&2
    exit 1
fi
if [[ $(enroot version) != 4.2.1 ]]; then
    printf '%s\n' 'The validated Enroot version changed unexpectedly.' >&2
    exit 1
fi
if sudo test -e /etc/slurm/plugstack.conf \
    || sudo test -e /etc/slurm/plugstack.conf.d \
    || sudo find /usr/local/lib/slurm /usr/lib/x86_64-linux-gnu/slurm \
        -maxdepth 1 -name 'spank_pyxis.so' -print -quit 2>/dev/null | grep -q .; then
    printf '%s\n' 'An unknown Pyxis or Slurm plugstack configuration exists.' >&2
    exit 1
fi

install -d -m 2770 "${artifact_dir}" "${record_dir}"
tag_result=$(git ls-remote "${repository}" "refs/tags/${pyxis_tag}")
printf '%s\n' "${tag_result}" >"${record_dir}/git-ls-remote.txt"
if [[ ${tag_result} != "${pyxis_commit}"$'\t'"refs/tags/${pyxis_tag}" ]]; then
    printf '%s\n' 'The official Pyxis tag no longer resolves to the reviewed commit.' >&2
    exit 1
fi

curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --retry 3 --output "${archive}.part" "${archive_url}"
mv -- "${archive}.part" "${archive}"
sha256sum "${archive}" >"${artifact_dir}/SHA256SUMS"
tar -tzf "${archive}" >"${record_dir}/archive-members.txt"
if ! awk '
    index($0, "/../") || $0 ~ /^\.\.\// || $0 ~ /^\// { bad = 1 }
    $0 !~ /^pyxis-0\.24\.0\// { bad = 1 }
    END { exit bad }
' "${record_dir}/archive-members.txt"; then
    printf '%s\n' 'The Pyxis archive contains an unexpected path.' >&2
    exit 1
fi

install -d -m 0750 "${source_dir}"
tar --extract --gzip --file "${archive}" --directory "${source_dir}" \
    --strip-components=1 --no-same-owner
for required in Makefile README.md pyxis_slurmstepd.c pyxis_slurmstepd.h; do
    if [[ ! -f ${source_dir}/${required} ]]; then
        printf 'The official Pyxis archive lacks expected source: %s\n' "${required}" >&2
        exit 1
    fi
done

srun --version >"${record_dir}/slurm-version.txt"
dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' \
    slurm-smd-client slurm-smd-dev >"${record_dir}/slurm-package-versions.txt"
stat -c '%U:%G %a %s %n' /usr/include/slurm/spank.h \
    >"${record_dir}/spank-header.txt"
scontrol show config | grep -E '^(PluginDir|SLURM_VERSION)' \
    >"${record_dir}/slurm-plugin-config.txt"
enroot version >"${record_dir}/enroot-version.txt"
make --version | head -n 1 >"${record_dir}/build-tools.txt"
gcc --version | head -n 1 >>"${record_dir}/build-tools.txt"
pkg-config --version >>"${record_dir}/build-tools.txt"

archive_sha=$(awk '{print $1}' "${artifact_dir}/SHA256SUMS")
cat >"${report_file}" <<EOF
# Pyxis version decision and source preparation

- Stable release selected: ${pyxis_tag}
- Tag commit: ${pyxis_commit}
- Official release: ${release_url}
- Official repository: ${repository}
- Source archive: ${archive_url}
- Downloaded archive SHA-256: ${archive_sha}
- Exact Slurm runtime: $(srun --version)
- Exact Slurm headers package: slurm-smd-dev $(dpkg-query -W -f='\${Version}' slurm-smd-dev)
- SPANK header: /usr/include/slurm/spank.h
- Slurm PluginDir: /usr/lib/x86_64-linux-gnu/slurm
- Enroot: $(enroot version)
- Existing Pyxis plugin/configuration: none
- Node state: ${node_state}
- Installation performed: no

Status: PYXIS SOURCE PREPARED
EOF

printf 'PYXIS_TAG=%s\n' "${pyxis_tag}"
printf 'PYXIS_COMMIT=%s\n' "${pyxis_commit}"
printf 'PYXIS_ARCHIVE_SHA256=%s\n' "${archive_sha}"
printf '%s\n' 'PYXIS SOURCE PREPARED'
