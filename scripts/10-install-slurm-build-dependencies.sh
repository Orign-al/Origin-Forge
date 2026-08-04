#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for APT only.' >&2
    exit 2
fi

run_id=${1:?usage: 10-install-slurm-build-dependencies.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
package_file=${platform_dir}/config/slurm-build-dependencies.txt
build_root=${platform_dir}/build/slurm-25.11.7-${run_id}-official
record_dir=${build_root}/records
log_file=${platform_dir}/logs/slurm-build-dependencies-${run_id}.log
simulation=${record_dir}/build-deps-simulation-final.txt

if [[ ! -f ${package_file} || ! -d ${record_dir} ]]; then
    printf '%s\n' 'Missing dependency manifest or prepared build root.' >&2
    exit 1
fi

mapfile -t packages < <(awk 'NF && $1 !~ /^#/ { print $1 }' "${package_file}")
if (( ${#packages[@]} == 0 )); then
    printf '%s\n' 'Dependency manifest is empty.' >&2
    exit 1
fi
for package in "${packages[@]}"; do
    if [[ ! ${package} =~ ^[a-z0-9][a-z0-9+.-]*$ ]]; then
        printf 'Invalid package name: %s\n' "${package}" >&2
        exit 1
    fi
done

sudo -n true
dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' \
    >"${record_dir}/dpkg-before-build-deps.txt"
apt-mark showmanual >"${record_dir}/apt-manual-before-build-deps.txt"
ss -lntup >"${record_dir}/listeners-before-build-deps.txt"

sudo apt-get -s install --no-install-recommends "${packages[@]}" | tee "${simulation}"
if grep -q '^Remv ' "${simulation}"; then
    printf '%s\n' 'APT simulation contains package removals; stopping.' >&2
    exit 1
fi
if ! grep -Eq '^0 upgraded, [0-9]+ newly installed, 0 to remove' "${simulation}"; then
    printf '%s\n' 'APT simulation summary is not the required zero-upgrade/zero-remove form.' >&2
    exit 1
fi

sudo env DEBIAN_FRONTEND=noninteractive \
    apt-get install -y --no-install-recommends "${packages[@]}" \
    2>&1 | tee "${log_file}"

dpkg --audit
dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' \
    >"${record_dir}/dpkg-after-build-deps.txt"
apt-mark showmanual >"${record_dir}/apt-manual-after-build-deps.txt"
ss -lntup >"${record_dir}/listeners-after-build-deps.txt"

if dpkg-query -W apache2 >/dev/null 2>&1; then
    printf '%s\n' 'Unexpected Apache package installed; stopping.' >&2
    exit 1
fi
if systemctl is-active --quiet apache2 2>/dev/null; then
    printf '%s\n' 'Unexpected Apache service active; stopping.' >&2
    exit 1
fi

nvidia-smi -L
printf 'BUILD_DEPENDENCIES_INSTALLED=%s\n' "${#packages[@]}"
