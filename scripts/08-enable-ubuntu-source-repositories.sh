#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo only where needed.' >&2
    exit 2
fi

run_id=${1:?usage: 08-enable-ubuntu-source-repositories.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
source_file=${platform_dir}/config/ubuntu-src.sources
target_file=/etc/apt/sources.list.d/ubuntu-src.sources
backup_dir=${platform_dir}/backups/apt-sources-before-slurm-${run_id}

if [[ ! -f ${source_file} ]]; then
    printf 'Missing tracked source definition: %s\n' "${source_file}" >&2
    exit 1
fi

sudo -n true

if [[ ! -e ${backup_dir} ]]; then
    sudo install -d -o root -g gpu-platform-admin -m 0750 "${backup_dir}"
    sudo cp -a /etc/apt/sources.list "${backup_dir}/sources.list"
    sudo cp -a /etc/apt/sources.list.d "${backup_dir}/sources.list.d"
fi

if [[ -e ${target_file} ]]; then
    sudo cp -a "${target_file}" "${backup_dir}/ubuntu-src.sources.existing-${run_id}"
fi

sudo install -o root -g root -m 0644 "${source_file}" "${target_file}"
sudo apt-get update
apt-cache showsrc slurm-wlm | sed -n '1,120p'
