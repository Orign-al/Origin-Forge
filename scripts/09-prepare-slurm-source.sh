#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo only to create the build root.' >&2
    exit 2
fi

run_id=${1:?usage: 09-prepare-slurm-source.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly slurm_version=25.11.7
readonly local_version=25.11.7-1h100.1
readonly upstream_url=https://download.schedmd.com/slurm/slurm-25.11.7.tar.bz2
readonly upstream_sha256=8f71a55b755e41f07d0fc790e9d0c94bd810dc1b324cd308545b9a0fd5f66df6

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
template=${platform_dir}/templates/slurm-debian-changelog-entry
build_root=${platform_dir}/build/slurm-${slurm_version}-${run_id}-official
cache_dir=${build_root}/cache
stage_dir=${build_root}/stage
work_dir=${build_root}/work
record_dir=${build_root}/records
archive=${cache_dir}/slurm-${slurm_version}.tar.bz2
partial=${cache_dir}/slurm-${slurm_version}.tar.bz2.partial
upstream_stage=${stage_dir}/upstream
work_tree=${work_dir}/slurm-smd-${slurm_version}

if [[ ! -f ${template} ]]; then
    printf 'Missing tracked changelog template: %s\n' "${template}" >&2
    exit 1
fi

if [[ -e ${build_root} ]]; then
    printf 'Refusing to overwrite existing build root: %s\n' "${build_root}" >&2
    exit 1
fi

sudo -n true
sudo install -d -o codexops -g gpu-platform-admin -m 2770 \
    "${platform_dir}/build" \
    "${build_root}"
install -d -m 2770 "${cache_dir}" "${stage_dir}" "${work_dir}" "${record_dir}" "${upstream_stage}"

cleanup_partial() {
    if [[ -f ${partial} ]]; then
        rm -f -- "${partial}"
    fi
}
trap cleanup_partial EXIT

curl \
    --fail \
    --location \
    --retry 5 \
    --retry-delay 3 \
    --connect-timeout 20 \
    --max-time 900 \
    --output "${partial}" \
    "${upstream_url}"

printf '%s  %s\n' "${upstream_sha256}" "${partial}" | sha256sum --check --status
mv -- "${partial}" "${archive}"
trap - EXIT

archive_bad_entry=$(tar -tjf "${archive}" | awk '
    $0 !~ /^slurm-25[.]11[.]7\// || $0 ~ /(^|\/)\.\.(\/|$)/ { print; exit }
')
if [[ -n ${archive_bad_entry} ]]; then
    printf 'Unsafe or unexpected archive entry: %s\n' "${archive_bad_entry}" >&2
    exit 1
fi

tar -xjf "${archive}" -C "${upstream_stage}"
cp -a -- "${upstream_stage}/slurm-${slurm_version}" "${work_tree}"

upstream_source=$(dpkg-parsechangelog -l"${work_tree}/debian/changelog" -S Source)
upstream_version=$(dpkg-parsechangelog -l"${work_tree}/debian/changelog" -S Version)
if [[ ${upstream_source} != slurm-smd || ${upstream_version} != 25.11.7-1 ]]; then
    printf 'Unexpected SchedMD Debian metadata: source=%s version=%s\n' \
        "${upstream_source}" "${upstream_version}" >&2
    exit 1
fi

build_date=$(LC_ALL=C date -R)
sed "s|@BUILD_DATE@|${build_date}|" "${template}" >"${work_tree}/debian/changelog.new"
sed -n '1,$p' "${work_tree}/debian/changelog" >>"${work_tree}/debian/changelog.new"
mv -- "${work_tree}/debian/changelog.new" "${work_tree}/debian/changelog"

actual_version=$(dpkg-parsechangelog -l"${work_tree}/debian/changelog" -S Version)
if [[ ${actual_version} != "${local_version}" ]]; then
    printf 'Unexpected Debian version: %s\n' "${actual_version}" >&2
    exit 1
fi

# Apply and then unapply the quilt series once. Any rebase conflict is a hard stop.
dpkg-source --before-build "${work_tree}"
dpkg-source --after-build "${work_tree}"

sha256sum "${archive}" >"${record_dir}/upstream-sha256.txt"
dpkg-parsechangelog -l"${work_tree}/debian/changelog" >"${record_dir}/debian-changelog-current.txt"
cp -a -- "${work_tree}/debian/control" "${record_dir}/schedmd-debian-control.txt"
cp -a -- "${work_tree}/debian/rules" "${record_dir}/schedmd-debian-rules.txt"
if [[ -f ${work_tree}/debian/patches/series ]]; then
    cp -a -- "${work_tree}/debian/patches/series" "${record_dir}/schedmd-patch-series.txt"
fi

printf 'BUILD_ROOT=%s\n' "${build_root}"
printf 'WORK_TREE=%s\n' "${work_tree}"
printf 'LOCAL_VERSION=%s\n' "${actual_version}"
printf 'UPSTREAM_SHA256=%s\n' "${upstream_sha256}"
