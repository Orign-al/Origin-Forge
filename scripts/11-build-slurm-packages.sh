#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; package compilation must be unprivileged.' >&2
    exit 2
fi

run_id=${1:?usage: 11-build-slurm-packages.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly local_version=25.11.7-1h100.1
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
build_root=${platform_dir}/build/slurm-25.11.7-${run_id}-official
work_tree=${build_root}/work/slurm-smd-25.11.7
record_dir=${build_root}/records
artifact_dir=${platform_dir}/artifacts/slurm-25.11.7-${run_id}
patch_file=${platform_dir}/templates/slurm-smd-25.11.7-h100.patch
build_log=${platform_dir}/logs/slurm-build-${run_id}.log

if [[ ! -d ${work_tree} || ! -f ${patch_file} || ! -d ${record_dir} ]]; then
    printf '%s\n' 'Missing prepared source tree, tracked patch, or record directory.' >&2
    exit 1
fi
if [[ -e ${artifact_dir} ]]; then
    printf 'Refusing to overwrite artifact directory: %s\n' "${artifact_dir}" >&2
    exit 1
fi

sha256sum "${work_tree}/debian/control" "${work_tree}/debian/rules" \
    >"${record_dir}/packaging-sha256-before-h100-patch.txt"

if patch --dry-run --silent --fuzz=0 -d "${work_tree}" -p1 <"${patch_file}"; then
    patch --batch --forward --fuzz=0 -d "${work_tree}" -p1 <"${patch_file}"
elif patch --dry-run --batch --silent --fuzz=0 --reverse -d "${work_tree}" -p1 <"${patch_file}"; then
    printf '%s\n' 'Tracked H100 packaging patch was already applied.'
else
    printf '%s\n' 'Tracked H100 packaging patch does not apply exactly; stopping.' >&2
    exit 1
fi

sha256sum "${patch_file}" >"${record_dir}/h100-packaging-patch-sha256.txt"
sha256sum "${work_tree}/debian/control" "${work_tree}/debian/rules" \
    >"${record_dir}/packaging-sha256-after-h100-patch.txt"

actual_version=$(dpkg-parsechangelog -l"${work_tree}/debian/changelog" -S Version)
if [[ ${actual_version} != "${local_version}" ]]; then
    printf 'Unexpected source package version: %s\n' "${actual_version}" >&2
    exit 1
fi

dpkg-checkbuilddeps "${work_tree}/debian/control" \
    2>&1 | tee "${record_dir}/dpkg-checkbuilddeps.txt"

available_jobs=$(nproc)
jobs=16
if (( available_jobs < jobs )); then
    jobs=${available_jobs}
fi
source_date_epoch=$(dpkg-parsechangelog -l"${work_tree}/debian/changelog" -S Timestamp)

printf 'BUILD_START=%s\n' "$(date --iso-8601=seconds)" | tee "${record_dir}/build-timing.txt"
printf 'BUILD_JOBS=%s\n' "${jobs}" | tee -a "${record_dir}/build-timing.txt"
(
    cd -- "${work_tree}"
    export DEB_BUILD_OPTIONS="parallel=${jobs}"
    export SOURCE_DATE_EPOCH="${source_date_epoch}"
    timeout --signal=TERM --kill-after=60s 3600s \
        nice -n 5 dpkg-buildpackage -b -uc -us -j"${jobs}"
) >"${build_log}" 2>&1
printf 'BUILD_END=%s\n' "$(date --iso-8601=seconds)" | tee -a "${record_dir}/build-timing.txt"

mapfile -t debs < <(find "${build_root}/work" -maxdepth 1 -type f -name '*.deb' -print | sort)
if (( ${#debs[@]} == 0 )); then
    printf '%s\n' 'Build completed without Debian package artifacts.' >&2
    exit 1
fi

for deb in "${debs[@]}"; do
    version=$(dpkg-deb --field "${deb}" Version)
    if [[ ${version} != "${local_version}" ]]; then
        printf 'Package has unexpected version: %s %s\n' "${deb}" "${version}" >&2
        exit 1
    fi
done

install -d -m 2770 "${platform_dir}/artifacts" "${artifact_dir}"
cp -a -- "${debs[@]}" "${artifact_dir}/"
find "${build_root}/work" -maxdepth 1 -type f \
    \( -name '*.changes' -o -name '*.buildinfo' \) \
    -exec cp -a -- {} "${artifact_dir}/" \;

(
    cd -- "${artifact_dir}"
    sha256sum -- *.deb | sort -k2 >SHA256SUMS
    for deb in ./*.deb; do
        dpkg-deb --field "${deb}" Package Version Architecture
        printf 'Filename: %s\n\n' "${deb#./}"
    done >PACKAGE-METADATA.txt
)

printf 'ARTIFACT_DIR=%s\n' "${artifact_dir}"
printf 'PACKAGE_COUNT=%s\n' "${#debs[@]}"
