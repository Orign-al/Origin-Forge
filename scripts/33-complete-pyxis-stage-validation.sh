#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; staged validation is unprivileged.' >&2
    exit 2
fi

run_id=${1:?usage: 33-complete-pyxis-stage-validation.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly pyxis_version=0.24.0
readonly archive_sha256=9c4cdb79a67301d8ea05951aa4d2c205f40cf6145e338214b0191add8b36d6c4
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/pyxis-${pyxis_version}-${run_id}
stage_dir=${artifact_dir}/stage
stage_plugin=${stage_dir}/usr/local/lib/slurm/spank_pyxis.so
stage_generated_conf=${stage_dir}/usr/local/share/pyxis/pyxis.conf
record_dir=${platform_dir}/reports/pyxis-build-${run_id}.d
report_file=${platform_dir}/reports/pyxis-build-${run_id}.md

if [[ ! -d ${record_dir} || ! -f ${stage_plugin} \
    || ! -f ${stage_generated_conf} ]]; then
    printf '%s\n' 'The completed Pyxis staging output is missing.' >&2
    exit 1
fi
if [[ -e ${report_file} || -e ${record_dir}/stage-pyxis.conf \
    || -e ${record_dir}/stage-slurm.conf ]]; then
    printf '%s\n' 'Refusing to overwrite Pyxis continuation records.' >&2
    exit 1
fi
if [[ $(<"${stage_generated_conf}") != 'required /usr/local/lib/slurm/spank_pyxis.so' ]]; then
    printf '%s\n' 'The official staged Pyxis configuration is unexpected.' >&2
    exit 1
fi

sudo -n true
node_state=$(sinfo -h -n sagsh100server -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf '%s\n' 'Slurm node is not drained.' >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly during staged Pyxis validation.' >&2
    exit 1
fi
if [[ $(srun --version) != 'slurm 25.11.7' ]] \
    || [[ $(dpkg-query -W -f='${Version}' slurm-smd-dev) != 25.11.7-1h100.1 ]]; then
    printf '%s\n' 'The exact Slurm runtime/header contract changed.' >&2
    exit 1
fi
if sudo test -e /etc/slurm/plugstack.conf \
    || sudo test -e /etc/slurm/plugstack.conf.d \
    || sudo test -e /usr/local/lib/slurm/spank_pyxis.so \
    || sudo test -e /usr/local/share/pyxis/pyxis.conf; then
    printf '%s\n' 'An unknown system Pyxis file appeared before staged validation.' >&2
    exit 1
fi

nm -D --defined-only "${stage_plugin}" >"${record_dir}/plugin-nm-dynamic-defined.txt"
for symbol in slurm_spank_init slurm_spank_init_post_opt slurm_spank_exit; do
    if ! grep -Eq "[[:space:]]T[[:space:]]${symbol}(@@PYXIS_1\\.0)?$" \
        "${record_dir}/plugin-nm-dynamic-defined.txt"; then
        printf 'The staged plugin lacks required SPANK symbol: %s\n' "${symbol}" >&2
        exit 1
    fi
done
if grep -Fq 'not found' "${record_dir}/plugin-ldd.txt"; then
    printf '%s\n' 'The staged plugin has an unresolved shared-library dependency.' >&2
    exit 1
fi

printf 'required %s\n' "${stage_plugin}" >"${record_dir}/stage-pyxis.conf"
sudo cat /etc/slurm/slurm.conf >"${record_dir}/stage-slurm.conf"
if grep -Eq '^[[:space:]]*PlugStackConfig=' "${record_dir}/stage-slurm.conf"; then
    printf '%s\n' 'The deployed Slurm configuration unexpectedly defines PlugStackConfig.' >&2
    exit 1
fi
printf 'PlugStackConfig=%s\n' "${record_dir}/stage-pyxis.conf" \
    >>"${record_dir}/stage-slurm.conf"

SLURM_CONF="${record_dir}/stage-slurm.conf" srun --help \
    >"${record_dir}/srun-help-stage.txt" 2>"${record_dir}/srun-help-stage.stderr"
container_help_rc=0
SLURM_CONF="${record_dir}/stage-slurm.conf" srun --container-help \
    >"${record_dir}/srun-container-help-stage.txt" \
    2>"${record_dir}/srun-container-help-stage.stderr" || container_help_rc=$?
if grep -Fqi 'Incompatible plugin version' \
    "${record_dir}/srun-help-stage.txt" "${record_dir}/srun-help-stage.stderr" \
    "${record_dir}/srun-container-help-stage.txt" \
    "${record_dir}/srun-container-help-stage.stderr"; then
    printf '%s\n' 'Incompatible plugin version detected in staged load.' >&2
    exit 1
fi
if ! grep -Fq -- '--container-image' "${record_dir}/srun-help-stage.txt" \
    || ! grep -Fq '[pyxis]' "${record_dir}/srun-help-stage.txt"; then
    printf '%s\n' 'The staged plugin did not register its srun options.' >&2
    exit 1
fi
if (( container_help_rc == 0 )); then
    if ! grep -Fq 'pyxis' "${record_dir}/srun-container-help-stage.txt"; then
        printf '%s\n' 'The optional container help output is unexpected.' >&2
        exit 1
    fi
    container_help_status=passed
elif [[ ${container_help_rc} -eq 255 ]] \
    && grep -Fq "unrecognized option '--container-help'" \
        "${record_dir}/srun-container-help-stage.stderr"; then
    container_help_status='unsupported optional probe; srun --help used'
else
    printf 'Unexpected optional container help result: %s\n' "${container_help_rc}" >&2
    exit 1
fi

sha256sum "${stage_plugin}" >"${record_dir}/plugin-sha256.txt"
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
- Exported callbacks: slurm_spank_init, slurm_spank_init_post_opt, slurm_spank_exit
- ELF symbol version: PYXIS_1.0
- Official install target: /usr/local/lib/slurm/spank_pyxis.so
- Staged srun option registration: passed
- Staged srun container help: ${container_help_status}
- Incompatible plugin version: not present
- System installation performed: no
- Node state: ${node_state}

Status: PYXIS BUILD STAGED PASSED
EOF

printf 'PYXIS_PLUGIN_SHA256=%s\n' "${plugin_sha}"
printf '%s\n' 'PYXIS BUILD STAGED PASSED'
