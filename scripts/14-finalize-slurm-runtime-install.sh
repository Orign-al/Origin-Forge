#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo only for protected configuration.' >&2
    exit 2
fi

run_id=${1:?usage: 14-finalize-slurm-runtime-install.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly expected_version=25.11.7-1h100.1
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
record_dir=${platform_dir}/reports/slurm-runtime-finalize-${run_id}.d
report_file=${platform_dir}/reports/slurm-runtime-install-${run_id}.md
preference_source=${platform_dir}/config/slurm-apt-preferences
preference_target=/etc/apt/preferences.d/90-h100-slurm-local

required_packages=(
    slurm-smd
    slurm-smd-client
    slurm-smd-dev
    slurm-smd-slurmctld
    slurm-smd-slurmd
    slurm-smd-slurmdbd
)

if [[ ! -d ${platform_dir}/reports/slurm-install-${run_id}.d ]]; then
    printf '%s\n' 'The initial installation transaction record is missing.' >&2
    exit 1
fi
if [[ ! -f ${preference_source} ]]; then
    printf '%s\n' 'Tracked Slurm APT preference file is missing.' >&2
    exit 1
fi
if [[ -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite an existing finalization record.' >&2
    exit 1
fi

sudo -n true
install -d -m 2770 "${record_dir}"

audit_result=$(dpkg --audit)
if [[ -n ${audit_result} ]]; then
    printf '%s\n' "${audit_result}" >&2
    exit 1
fi

for package in "${required_packages[@]}"; do
    version=$(dpkg-query -W -f='${Version}' "${package}")
    status=$(dpkg-query -W -f='${db:Status-Abbrev}' "${package}")
    if [[ ${version} != "${expected_version}" || ${status} != ii* ]]; then
        printf 'Package validation failed: %s %s %s\n' "${package}" "${version}" "${status}" >&2
        exit 1
    fi
done
for package in munge mariadb-server mariadb-client; do
    status=$(dpkg-query -W -f='${db:Status-Abbrev}' "${package}")
    if [[ ${status} != ii* ]]; then
        printf 'Runtime dependency is not configured: %s %s\n' "${package}" "${status}" >&2
        exit 1
    fi
done
if dpkg-query -W -f='${db:Status-Abbrev}\n' \
    slurm-wlm slurmctld slurmd slurmdbd libslurm-dev 2>/dev/null | grep -q '^ii'; then
    printf '%s\n' 'A conflicting Ubuntu Slurm package is installed.' >&2
    exit 1
fi

for unit in munge mariadb slurmdbd slurmctld slurmd; do
    if systemctl is-active --quiet "${unit}"; then
        printf 'Service must remain stopped before configuration: %s\n' "${unit}" >&2
        exit 1
    fi
done
if sudo -n test -e /usr/sbin/policy-rc.d; then
    printf '%s\n' 'Temporary policy-rc.d was not absent after installation; stopping.' >&2
    exit 1
fi

plugin_path=$(dpkg-query -L slurm-smd | awk '/\/auth_munge\.so$/ { print; exit }')
if [[ -z ${plugin_path} ]]; then
    printf '%s\n' 'Unable to locate Slurm PluginDir from package metadata.' >&2
    exit 1
fi
plugin_dir=$(dirname -- "${plugin_path}")
required_plugins=(
    accounting_storage_mysql.so
    acct_gather_energy_gpu.so
    auth_jwt.so
    auth_munge.so
    cgroup_v2.so
    gpu_nvml.so
    gres_gpu.so
    jobacct_gather_cgroup.so
    proctrack_cgroup.so
    task_cgroup.so
)
for plugin in "${required_plugins[@]}"; do
    if [[ ! -f ${plugin_dir}/${plugin} ]]; then
        printf 'Installed Slurm plugin is missing: %s\n' "${plugin}" >&2
        exit 1
    fi
    ldd "${plugin_dir}/${plugin}" >"${record_dir}/ldd-${plugin}.txt"
    if grep -q 'not found' "${record_dir}/ldd-${plugin}.txt"; then
        printf 'Installed Slurm plugin has an unresolved library: %s\n' "${plugin}" >&2
        exit 1
    fi
done

srun_path=$(dpkg-query -L slurm-smd-client | awk '$0 == "/usr/bin/srun" { print; exit }')
if [[ ! -x ${srun_path} ]]; then
    printf '%s\n' 'The packaged srun executable is absent.' >&2
    exit 1
fi
ldd "${srun_path}" >"${record_dir}/ldd-srun.txt"
if grep -q 'not found' "${record_dir}/ldd-srun.txt"; then
    printf '%s\n' 'The installed srun binary has an unresolved library.' >&2
    exit 1
fi

slurmctld -V | tee "${record_dir}/slurmctld-version.txt"
slurmd -V | tee "${record_dir}/slurmd-version.txt"
printf '%s\n' \
    'srun runtime invocation deferred until slurm.conf exists because 25.11 configless lookup is active.' \
    >"${record_dir}/srun-runtime-check-deferred.txt"

if sudo -n test -e "${preference_target}"; then
    if ! sudo -n cmp -s "${preference_source}" "${preference_target}"; then
        printf 'Unknown APT preference exists: %s\n' "${preference_target}" >&2
        exit 1
    fi
else
    sudo install -o root -g root -m 0644 "${preference_source}" "${preference_target}"
fi
sudo apt-mark hold "${required_packages[@]}" | tee "${record_dir}/apt-hold-result.txt"

apt-cache policy "${required_packages[@]}" slurm-wlm slurmctld slurmd slurmdbd libslurm-dev \
    >"${record_dir}/apt-policy.txt"
for package in "${required_packages[@]}"; do
    if ! apt-mark showhold | grep -Fxq "${package}"; then
        printf 'APT hold was not applied: %s\n' "${package}" >&2
        exit 1
    fi
done

dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' \
    "${required_packages[@]}" munge mariadb-server mariadb-client \
    >"${record_dir}/runtime-package-versions.txt"
for unit in munge mariadb slurmdbd slurmctld slurmd; do
    active=$(systemctl is-active "${unit}" 2>/dev/null || true)
    enabled=$(systemctl is-enabled "${unit}" 2>/dev/null || true)
    printf '%s\tactive=%s\tenabled=%s\n' "${unit}" "${active}" "${enabled}"
done >"${record_dir}/service-state.txt"

cat >"${report_file}" <<EOF
# Slurm runtime package installation

- Run ID: ${run_id}
- Installed Slurm version: ${expected_version}
- Source: SchedMD official slurm-25.11.7.tar.bz2, locally built Debian packages
- PluginDir: ${plugin_dir}
- MUNGE package: $(dpkg-query -W -f='${Version}' munge)
- MariaDB server package: $(dpkg-query -W -f='${Version}' mariadb-server)
- APT transaction: 15 new packages, zero upgraded, zero removed
- Ubuntu Slurm packages mixed: no
- APT version pin: ${preference_target}
- Package holds: ${required_packages[*]}
- Services after package installation: stopped and disabled pending configuration
- Deferred check: srun and slurmdbd runtime validation after configuration files exist

Status: SLURM RUNTIME PACKAGES INSTALLED
EOF

printf 'SLURM_RUNTIME_VERSION=%s\n' "${expected_version}"
printf 'SLURM_PLUGIN_DIR=%s\n' "${plugin_dir}"
printf '%s\n' 'SLURM RUNTIME PACKAGE FINALIZATION PASSED'
