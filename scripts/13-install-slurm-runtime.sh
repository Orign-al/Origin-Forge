#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo only for system changes.' >&2
    exit 2
fi

run_id=${1:?usage: 13-install-slurm-runtime.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly expected_version=25.11.7-1h100.1
readonly preference_name=90-h100-slurm-local
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/slurm-25.11.7-${run_id}
record_dir=${platform_dir}/reports/slurm-install-${run_id}.d
backup_dir=${platform_dir}/backups
log_file=${platform_dir}/logs/slurm-runtime-install-${run_id}.log
simulation=${record_dir}/apt-simulation.txt
preference_source=${platform_dir}/config/slurm-apt-preferences
preference_target=/etc/apt/preferences.d/${preference_name}
policy_target=/usr/sbin/policy-rc.d
policy_backup=${backup_dir}/policy-rc.d-${run_id}

required_packages=(
    slurm-smd
    slurm-smd-client
    slurm-smd-dev
    slurm-smd-slurmctld
    slurm-smd-slurmd
    slurm-smd-slurmdbd
)
runtime_packages=(munge mariadb-server mariadb-client)

if [[ ! -d ${artifact_dir} || ! -f ${artifact_dir}/SHA256SUMS ]]; then
    printf '%s\n' 'Missing validated Slurm artifact directory.' >&2
    exit 1
fi
if [[ ! -f ${preference_source} ]]; then
    printf '%s\n' 'Missing tracked Slurm APT preference file.' >&2
    exit 1
fi
if [[ -e ${record_dir} ]]; then
    printf 'Refusing to overwrite installation record directory: %s\n' "${record_dir}" >&2
    exit 1
fi
if sudo -n test -e "${preference_target}"; then
    if ! sudo -n cmp -s "${preference_source}" "${preference_target}"; then
        printf 'Unknown APT preference already exists: %s\n' "${preference_target}" >&2
        exit 1
    fi
fi

sudo -n true
install -d -m 2770 "${record_dir}" "${backup_dir}" "${platform_dir}/logs"

(
    cd -- "${artifact_dir}"
    sha256sum --check SHA256SUMS
) | tee "${record_dir}/artifact-checksums.txt"

declare -A package_path=()
while IFS= read -r -d '' deb; do
    package=$(dpkg-deb --field "${deb}" Package)
    version=$(dpkg-deb --field "${deb}" Version)
    if [[ ${version} != "${expected_version}" ]]; then
        printf 'Unexpected artifact version: %s %s\n' "${package}" "${version}" >&2
        exit 1
    fi
    if [[ -n ${package_path[${package}]:-} ]]; then
        printf 'Duplicate package artifact: %s\n' "${package}" >&2
        exit 1
    fi
    package_path[${package}]=${deb}
done < <(find "${artifact_dir}" -maxdepth 1 -type f -name '*.deb' -print0)

local_debs=()
for package in "${required_packages[@]}"; do
    if [[ -z ${package_path[${package}]:-} ]]; then
        printf 'Required package artifact is absent: %s\n' "${package}" >&2
        exit 1
    fi
    local_debs+=("${package_path[${package}]}")
done

mapfile -t installed_slurm < <(
    dpkg-query -W -f='${binary:Package}\t${db:Status-Abbrev}\t${Version}\n' 'slurm*' 2>/dev/null \
        | awk '$2 ~ /^ii/ { print }'
)
if (( ${#installed_slurm[@]} != 0 )); then
    printf '%s\n' 'A Slurm package is already installed; refusing an unreviewed replacement:' >&2
    printf '%s\n' "${installed_slurm[@]}" >&2
    exit 1
fi

if sudo -n test -d /etc/slurm && sudo -n find /etc/slurm -mindepth 1 -print -quit | grep -q .; then
    printf '%s\n' 'Non-empty /etc/slurm appeared after preflight; refusing to overwrite it.' >&2
    exit 1
fi

date --iso-8601=seconds >"${record_dir}/start-time.txt"
dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' \
    >"${record_dir}/dpkg-before.txt"
dpkg --get-selections >"${record_dir}/dpkg-selections-before.txt"
apt-mark showmanual >"${record_dir}/apt-manual-before.txt"
apt-mark showhold >"${record_dir}/apt-holds-before.txt"
ss -lntup >"${record_dir}/listeners-before.txt"
systemctl list-unit-files munge.service mariadb.service slurmdbd.service slurmctld.service slurmd.service \
    --no-legend >"${record_dir}/unit-files-before.txt" 2>&1 || true
for unit in munge mariadb slurmdbd slurmctld slurmd; do
    if systemctl is-active --quiet "${unit}"; then
        printf 'Service unexpectedly active before installation: %s\n' "${unit}" >&2
        exit 1
    fi
done

if sudo -n test -d /etc/slurm; then
    sudo tar --acls --xattrs -C /etc -cpf "${backup_dir}/etc-slurm-preinstall-${run_id}.tar" slurm
else
    : >"${record_dir}/etc-slurm-was-absent.txt"
fi
sudo tar --acls --xattrs -C /etc/apt -cpf \
    "${backup_dir}/apt-preferences-preinstall-${run_id}.tar" preferences.d
sudo cp -a /var/lib/dpkg/status "${backup_dir}/dpkg-status-preinstall-${run_id}"
sudo chmod 0640 "${backup_dir}/dpkg-status-preinstall-${run_id}"

sudo apt-get -s -o Debug::NoLocking=1 install --no-install-recommends \
    "${local_debs[@]}" "${runtime_packages[@]}" | tee "${simulation}"

if grep -q '^Remv ' "${simulation}"; then
    printf '%s\n' 'APT simulation contains removals; stopping.' >&2
    exit 1
fi
if grep -Eqi 'downgrad|DOWNGRADED' "${simulation}"; then
    printf '%s\n' 'APT simulation indicates a downgrade; stopping.' >&2
    exit 1
fi
if ! grep -Eq '^0 upgraded, [0-9]+ newly installed, 0 to remove( and [0-9]+ not upgraded)?\.$' "${simulation}"; then
    printf '%s\n' 'APT simulation is not a zero-upgrade, zero-remove transaction.' >&2
    exit 1
fi
if grep -Eq '^Inst (slurm-wlm|slurmctld|slurmd|slurmdbd|libslurm-dev)( |:)' "${simulation}"; then
    printf '%s\n' 'APT simulation would mix Ubuntu Slurm packages; stopping.' >&2
    exit 1
fi
if grep -Eq '25\.11\.2([^-0-9]|$)' "${simulation}"; then
    printf '%s\n' 'APT simulation contains the rejected Ubuntu Slurm version.' >&2
    exit 1
fi
for package in "${required_packages[@]}"; do
    if ! awk -v package="${package}" -v version="${expected_version}" '
        $1 == "Inst" && $2 == package && index($0, "(" version " ") { found = 1 }
        END { exit !found }
    ' "${simulation}"; then
        printf 'APT simulation did not select the required local package: %s %s\n' \
            "${package}" "${expected_version}" >&2
        exit 1
    fi
done
awk '$1 == "Inst" { print }' "${simulation}" >"${record_dir}/apt-install-set.txt"

policy_was_present=false
policy_installed=false
temporary_policy=

restore_policy() {
    if [[ ${policy_installed} == true ]]; then
        if [[ ${policy_was_present} == true ]]; then
            sudo rm -f -- "${policy_target}"
            sudo cp -a -- "${policy_backup}" "${policy_target}"
        else
            sudo rm -f -- "${policy_target}"
        fi
        policy_installed=false
    fi
}

cleanup() {
    exit_status=$?
    set +e
    restore_policy
    if [[ -n ${temporary_policy} ]]; then
        rm -f -- "${temporary_policy}"
    fi
    exit "${exit_status}"
}
trap cleanup EXIT

if sudo test -L "${policy_target}"; then
    printf '%s\n' 'Refusing to replace a symlinked policy-rc.d.' >&2
    exit 1
fi
if sudo test -e "${policy_target}"; then
    if ! sudo test -f "${policy_target}"; then
        printf '%s\n' 'Existing policy-rc.d is not a regular file; stopping.' >&2
        exit 1
    fi
    sudo cp -a -- "${policy_target}" "${policy_backup}"
    policy_was_present=true
fi
temporary_policy=$(mktemp)
printf '%s\n' '#!/bin/sh' 'exit 101' >"${temporary_policy}"
sudo install -o root -g root -m 0755 "${temporary_policy}" "${policy_target}"
policy_installed=true

sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    "${local_debs[@]}" "${runtime_packages[@]}" 2>&1 | tee "${log_file}"

restore_policy
trap - EXIT
rm -f -- "${temporary_policy}"
temporary_policy=

sudo systemctl daemon-reload
for unit in slurmdbd slurmctld slurmd munge mariadb; do
    sudo systemctl disable "${unit}" >/dev/null 2>&1 || true
done

unexpected_active=false
for unit in munge mariadb slurmdbd slurmctld slurmd; do
    if systemctl is-active --quiet "${unit}"; then
        sudo systemctl stop "${unit}"
        printf 'Stopped service that unexpectedly became active: %s\n' "${unit}" >&2
        unexpected_active=true
    fi
done
if [[ ${unexpected_active} == true ]]; then
    printf '%s\n' 'A service bypassed installation start suppression; review required.' >&2
    exit 1
fi

audit_result=$(dpkg --audit)
if [[ -n ${audit_result} ]]; then
    printf '%s\n' "${audit_result}" >&2
    exit 1
fi

for package in "${required_packages[@]}"; do
    installed_version=$(dpkg-query -W -f='${Version}' "${package}")
    installed_status=$(dpkg-query -W -f='${db:Status-Abbrev}' "${package}")
    if [[ ${installed_version} != "${expected_version}" || ${installed_status} != ii* ]]; then
        printf 'Installed package validation failed: %s %s %s\n' \
            "${package}" "${installed_version}" "${installed_status}" >&2
        exit 1
    fi
done
if dpkg-query -W -f='${db:Status-Abbrev}\n' \
    slurm-wlm slurmctld slurmd slurmdbd libslurm-dev 2>/dev/null | grep -q '^ii'; then
    printf '%s\n' 'Conflicting Ubuntu Slurm package is installed.' >&2
    exit 1
fi

plugin_path=$(dpkg-query -L slurm-smd | awk '/\/auth_munge\.so$/ { print; exit }')
if [[ -z ${plugin_path} ]]; then
    printf '%s\n' 'Unable to locate the installed auth_munge plugin from package metadata.' >&2
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
    if ldd "${plugin_dir}/${plugin}" | grep -q 'not found'; then
        printf 'Installed Slurm plugin has an unresolved library: %s\n' "${plugin}" >&2
        exit 1
    fi
done

slurmctld -V | tee "${record_dir}/slurmctld-version.txt"
slurmd -V | tee "${record_dir}/slurmd-version.txt"
printf '%s\n' \
    'srun executable check deferred until slurm.conf exists; package version is authoritative before configuration.' \
    >"${record_dir}/srun-version-check-deferred.txt"
getent passwd slurm >"${record_dir}/slurm-user.txt"
getent passwd munge >"${record_dir}/munge-user.txt"

sudo install -o root -g root -m 0644 "${preference_source}" "${preference_target}"
sudo apt-mark hold "${required_packages[@]}" | tee "${record_dir}/apt-hold-result.txt"
apt-cache policy "${required_packages[@]}" slurm-wlm slurmctld slurmd slurmdbd libslurm-dev \
    >"${record_dir}/apt-policy-after.txt"

dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' \
    >"${record_dir}/dpkg-after.txt"
dpkg --get-selections >"${record_dir}/dpkg-selections-after.txt"
apt-mark showmanual >"${record_dir}/apt-manual-after.txt"
apt-mark showhold >"${record_dir}/apt-holds-after.txt"
ss -lntup >"${record_dir}/listeners-after.txt"
systemctl list-unit-files munge.service mariadb.service slurmdbd.service slurmctld.service slurmd.service \
    --no-legend >"${record_dir}/unit-files-after.txt" 2>&1 || true
date --iso-8601=seconds >"${record_dir}/end-time.txt"

printf 'SLURM_RUNTIME_VERSION=%s\n' "${expected_version}"
printf 'SLURM_PLUGIN_DIR=%s\n' "${plugin_dir}"
printf '%s\n' 'SLURM RUNTIME PACKAGES INSTALLED; SERVICES REMAIN STOPPED'
