#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this validation as codexops; no root privileges are required.' >&2
    exit 2
fi

run_id=${1:?usage: 12-validate-slurm-packages.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly expected_version=25.11.7-1h100.1
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
build_root=${platform_dir}/build/slurm-25.11.7-${run_id}-official
record_dir=${build_root}/records
artifact_dir=${platform_dir}/artifacts/slurm-25.11.7-${run_id}
validation_dir=${build_root}/validation-root-v4
validation_template=${platform_dir}/templates/slurm-preinstall-validation.conf
validation_conf=${validation_dir}/slurm-validation.conf

if [[ ! -d ${artifact_dir} || ! -f ${artifact_dir}/SHA256SUMS || ! -f ${validation_template} ]]; then
    printf '%s\n' 'Missing built package artifact directory or checksum manifest.' >&2
    exit 1
fi
if [[ -e ${validation_dir} ]]; then
    printf 'Refusing to overwrite validation root: %s\n' "${validation_dir}" >&2
    exit 1
fi

(
    cd -- "${artifact_dir}"
    sha256sum --check SHA256SUMS
)

mapfile -t debs < <(find "${artifact_dir}" -maxdepth 1 -type f -name '*.deb' -print | sort)
if (( ${#debs[@]} != 17 )); then
    printf 'Expected 17 packages, found %s.\n' "${#debs[@]}" >&2
    exit 1
fi

declare -A package_path=()
for deb in "${debs[@]}"; do
    package=$(dpkg-deb --field "${deb}" Package)
    version=$(dpkg-deb --field "${deb}" Version)
    if [[ ${version} != "${expected_version}" ]]; then
        printf 'Version mismatch: %s is %s\n' "${package}" "${version}" >&2
        exit 1
    fi
    if [[ -n ${package_path[${package}]:-} ]]; then
        printf 'Duplicate package artifact: %s\n' "${package}" >&2
        exit 1
    fi
    package_path[${package}]=${deb}
done

required_packages=(
    slurm-smd
    slurm-smd-client
    slurm-smd-dev
    slurm-smd-slurmctld
    slurm-smd-slurmd
    slurm-smd-slurmdbd
)
for package in "${required_packages[@]}"; do
    if [[ -z ${package_path[${package}]:-} ]]; then
        printf 'Missing required package: %s\n' "${package}" >&2
        exit 1
    fi
done

install -d -m 2770 "${validation_dir}"
for package in "${required_packages[@]}"; do
    dpkg-deb --extract "${package_path[${package}]}" "${validation_dir}"
done

plugin_dir=${validation_dir}/usr/lib/x86_64-linux-gnu/slurm
sed "s|@PLUGIN_DIR@|${plugin_dir}|" "${validation_template}" >"${validation_conf}"
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
        printf 'Missing required plugin: %s\n' "${plugin}" >&2
        exit 1
    fi
done

required_files=(
    usr/bin/srun
    usr/include/slurm/spank.h
    usr/sbin/slurmctld
    usr/sbin/slurmd
    usr/sbin/slurmdbd
)
for relative_path in "${required_files[@]}"; do
    if [[ ! -f ${validation_dir}/${relative_path} ]]; then
        printf 'Missing required file: /%s\n' "${relative_path}" >&2
        exit 1
    fi
done

ld_library_path=${plugin_dir}:${validation_dir}/usr/lib/x86_64-linux-gnu
: >"${record_dir}/required-plugin-ldd.txt"
: >"${record_dir}/required-plugin-needed.txt"
for plugin in "${required_plugins[@]}"; do
    printf 'PLUGIN=%s\n' "${plugin}" >>"${record_dir}/required-plugin-ldd.txt"
    LD_LIBRARY_PATH="${ld_library_path}" ldd "${plugin_dir}/${plugin}" \
        >>"${record_dir}/required-plugin-ldd.txt"
    readelf --dynamic "${plugin_dir}/${plugin}" \
        | grep NEEDED >>"${record_dir}/required-plugin-needed.txt" || true
done
if grep -q 'not found' "${record_dir}/required-plugin-ldd.txt"; then
    printf '%s\n' 'A required plugin has an unresolved shared-library dependency.' >&2
    exit 1
fi

SLURM_CONF="${validation_conf}" LD_LIBRARY_PATH="${ld_library_path}" \
    "${validation_dir}/usr/bin/srun" -V \
    | tee "${record_dir}/preinstall-srun-version.txt"
LD_LIBRARY_PATH="${ld_library_path}" "${validation_dir}/usr/sbin/slurmctld" -V \
    | tee "${record_dir}/preinstall-slurmctld-version.txt"
LD_LIBRARY_PATH="${ld_library_path}" "${validation_dir}/usr/sbin/slurmd" -V \
    | tee "${record_dir}/preinstall-slurmd-version.txt"
printf 'slurmdbd package version %s; executable configuration check deferred until slurmdbd.conf exists\n' \
    "$(dpkg-deb --field "${package_path[slurm-smd-slurmdbd]}" Version)" \
    | tee "${record_dir}/preinstall-slurmdbd-version.txt"

dpkg-deb --field "${package_path[slurm-smd]}" \
    Package Version Architecture Depends Conflicts \
    >"${record_dir}/slurm-smd-core-control-fields.txt"
nvidia-smi -L >"${record_dir}/gpu-after-slurm-build.txt"

printf '%s\n' 'SLURM PACKAGE VALIDATION PASSED'
