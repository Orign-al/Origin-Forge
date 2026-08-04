#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; the build and staging tree are unprivileged.' >&2
    exit 2
fi

run_id=${1:?usage: 32-build-stage-pyxis.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly pyxis_version=0.24.0
readonly archive_sha256=9c4cdb79a67301d8ea05951aa4d2c205f40cf6145e338214b0191add8b36d6c4
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/pyxis-${pyxis_version}-${run_id}
source_dir=${artifact_dir}/source
archive=${artifact_dir}/pyxis-${pyxis_version}.tar.gz
build_dir=${artifact_dir}/build
stage_dir=${artifact_dir}/stage
stage_plugin=${stage_dir}/usr/local/lib/slurm/spank_pyxis.so
stage_generated_conf=${stage_dir}/usr/local/share/pyxis/pyxis.conf
record_dir=${platform_dir}/reports/pyxis-build-${run_id}.d
report_file=${platform_dir}/reports/pyxis-build-${run_id}.md

if [[ ! -f ${platform_dir}/reports/pyxis-version-decision-${run_id}.md ]] \
    || ! grep -Fxq 'Status: PYXIS SOURCE PREPARED' \
        "${platform_dir}/reports/pyxis-version-decision-${run_id}.md"; then
    printf '%s\n' 'A passed Pyxis source preparation report is required.' >&2
    exit 1
fi
if [[ -e ${build_dir} || -e ${stage_dir} || -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite Pyxis build, staging, or audit records.' >&2
    exit 1
fi
if [[ $(sha256sum "${archive}" | awk '{print $1}') != "${archive_sha256}" ]]; then
    printf '%s\n' 'The prepared Pyxis source archive checksum changed.' >&2
    exit 1
fi

sudo -n true
node_state=$(sinfo -h -n sagsh100server -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf '%s\n' 'Slurm node is not drained.' >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly during the Pyxis build.' >&2
    exit 1
fi
if [[ $(srun --version) != 'slurm 25.11.7' ]] \
    || [[ $(dpkg-query -W -f='${Version}' slurm-smd-dev) != 25.11.7-1h100.1 ]] \
    || [[ ! -f /usr/include/slurm/spank.h ]]; then
    printf '%s\n' 'The exact Slurm runtime/header contract changed.' >&2
    exit 1
fi
if sudo test -e /etc/slurm/plugstack.conf \
    || sudo test -e /etc/slurm/plugstack.conf.d \
    || sudo test -e /usr/local/lib/slurm/spank_pyxis.so \
    || sudo test -e /usr/local/share/pyxis/pyxis.conf; then
    printf '%s\n' 'An unknown system Pyxis file appeared before staging.' >&2
    exit 1
fi

install -d -m 2770 "${record_dir}"
cp -a -- "${source_dir}" "${build_dir}"
make -C "${build_dir}" clean >"${record_dir}/make-clean.txt" 2>&1
make -C "${build_dir}" -j 8 CC=/usr/bin/gcc \
    >"${record_dir}/make-build.txt" 2>&1
make -C "${build_dir}" CC=/usr/bin/gcc DESTDIR="${stage_dir}" install \
    >"${record_dir}/make-stage-install.txt" 2>&1

if [[ ! -f ${stage_plugin} || ! -f ${stage_generated_conf} ]]; then
    printf '%s\n' 'Pyxis staging did not create the official expected layout.' >&2
    exit 1
fi
if [[ $(<"${stage_generated_conf}") != 'required /usr/local/lib/slurm/spank_pyxis.so' ]]; then
    printf '%s\n' 'The generated Pyxis configuration is unexpected.' >&2
    exit 1
fi

file "${stage_plugin}" >"${record_dir}/plugin-file.txt"
readelf --file-header "${stage_plugin}" >"${record_dir}/plugin-elf-header.txt"
readelf --dynamic "${stage_plugin}" >"${record_dir}/plugin-elf-dynamic.txt"
readelf --wide --syms "${stage_plugin}" >"${record_dir}/plugin-elf-symbols.txt"
ldd "${stage_plugin}" >"${record_dir}/plugin-ldd.txt"
sha256sum "${stage_plugin}" >"${record_dir}/plugin-sha256.txt"
for symbol in slurm_spank_init slurm_spank_init_post_opt slurm_spank_exit; do
    if ! grep -Eq "[[:space:]]${symbol}$" "${record_dir}/plugin-elf-symbols.txt"; then
        printf 'The staged plugin lacks required SPANK symbol: %s\n' "${symbol}" >&2
        exit 1
    fi
done
if ! file "${stage_plugin}" | grep -Fq 'ELF 64-bit LSB shared object, x86-64'; then
    printf '%s\n' 'The staged plugin is not the expected amd64 ELF shared object.' >&2
    exit 1
fi
if grep -Fq 'not found' "${record_dir}/plugin-ldd.txt"; then
    printf '%s\n' 'The staged plugin has an unresolved shared-library dependency.' >&2
    exit 1
fi

printf 'required %s\n' "${stage_plugin}" >"${record_dir}/stage-pyxis.conf"
sudo cat /etc/slurm/slurm.conf >"${record_dir}/stage-slurm.conf"
printf 'PlugStackConfig=%s\n' "${record_dir}/stage-pyxis.conf" \
    >>"${record_dir}/stage-slurm.conf"
SLURM_CONF="${record_dir}/stage-slurm.conf" srun --help \
    >"${record_dir}/srun-help-stage.txt" 2>"${record_dir}/srun-help-stage.stderr"
if grep -Fqi 'Incompatible plugin version' \
    "${record_dir}/srun-help-stage.txt" "${record_dir}/srun-help-stage.stderr"; then
    printf '%s\n' 'Incompatible plugin version detected in staged load.' >&2
    exit 1
fi
if ! grep -Fq -- '--container-image' "${record_dir}/srun-help-stage.txt" \
    || ! grep -Fq '[pyxis]' "${record_dir}/srun-help-stage.txt"; then
    printf '%s\n' 'The staged plugin did not register its srun options.' >&2
    exit 1
fi

plugin_sha=$(awk '{print $1}' "${record_dir}/plugin-sha256.txt")
cat >"${report_file}" <<EOF
# Pyxis staged build

- Run ID: ${run_id}
- Pyxis version: v${pyxis_version}
- Source archive SHA-256: ${archive_sha256}
- Compiler: $(gcc --version | head -n 1)
- Build headers: /usr/include/slurm/spank.h from slurm-smd-dev $(dpkg-query -W -f='\${Version}' slurm-smd-dev)
- Slurm runtime: $(srun --version)
- Staged plugin: ${stage_plugin}
- Staged plugin SHA-256: ${plugin_sha}
- Official install target: /usr/local/lib/slurm/spank_pyxis.so
- Staged srun option registration: passed
- Incompatible plugin version: not present
- System installation performed: no
- Node state: ${node_state}

Status: PYXIS BUILD STAGED PASSED
EOF

printf 'PYXIS_PLUGIN_SHA256=%s\n' "${plugin_sha}"
printf '%s\n' 'PYXIS BUILD STAGED PASSED'
