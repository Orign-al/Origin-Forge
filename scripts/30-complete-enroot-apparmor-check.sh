#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo only for AppArmor and package hold operations.' >&2
    exit 2
fi

run_id=${1:?usage: 30-complete-enroot-apparmor-check.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly enroot_version=4.2.1-1
readonly enroot_commit=ead3a25ed974948235d28f99fc387ac82435ce62
readonly bundle_sha256=8f39b28312d24c0fc8ae35386906bf51238934bee54df583b63ea99175ba1eea
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/enroot-4.2.1-${run_id}
bundle=${artifact_dir}/enroot-check_4.2.1_x86_64.run
work_dir=${artifact_dir}/apparmor-check-${run_id}
rootfs_dir=${work_dir}/enroot-check_4.2.1_x86_64
helper_path=${rootfs_dir}/.enroot/bin/enroot-nsenter
prior_record_dir=${platform_dir}/reports/enroot-install-completion-${run_id}.d
record_dir=${platform_dir}/reports/enroot-apparmor-compat-${run_id}.d
report_file=${platform_dir}/reports/enroot-install-${run_id}.md
profile_file=/tmp/enroot-check-apparmor-${run_id}
profile_loaded=0

cleanup() {
    local rc=$?
    if (( profile_loaded == 1 )); then
        sudo apparmor_parser -R "${profile_file}" >/dev/null 2>&1 || \
            printf '%s\n' 'WARNING: failed to unload the transient Enroot check profile.' >&2
    fi
    rm -f -- "${profile_file}"
    exit "${rc}"
}
trap cleanup EXIT

sudo -n true
if [[ ! -d ${prior_record_dir} ]] \
    || [[ ! -s ${prior_record_dir}/enroot-check-run.txt ]]; then
    printf '%s\n' 'The initial official bundle audit record is missing.' >&2
    exit 1
fi
if ! grep -Fxq 'enroot-nsenter: failed to create user namespace: Permission denied' \
    "${prior_record_dir}/enroot-check-run.txt"; then
    printf '%s\n' 'The prior bundle failure is not the reviewed AppArmor path mismatch.' >&2
    exit 1
fi
if [[ -e ${record_dir} || -e ${report_file} || -e ${work_dir} ]]; then
    printf '%s\n' 'Refusing to overwrite Enroot compatibility records or extracted assets.' >&2
    exit 1
fi
if [[ $(sha256sum "${bundle}" | awk '{print $1}') != "${bundle_sha256}" ]]; then
    printf '%s\n' 'Official Enroot check bundle checksum mismatch.' >&2
    exit 1
fi
for package in enroot enroot+caps; do
    if [[ $(dpkg-query -W -f='${Version}' "${package}") != "${enroot_version}" ]] \
        || [[ $(dpkg-query -W -f='${db:Status-Abbrev}' "${package}") != ii* ]]; then
        printf 'Enroot package validation failed: %s\n' "${package}" >&2
        exit 1
    fi
done
if [[ $(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns) != 1 ]]; then
    printf '%s\n' 'The Ubuntu AppArmor userns restriction was weakened.' >&2
    exit 1
fi
if ! sudo aa-status | grep -Fq '/usr/bin/enroot-nsenter'; then
    printf '%s\n' 'The release-provided Enroot AppArmor profile is not loaded.' >&2
    exit 1
fi

install -d -m 2770 "${record_dir}"
install -d -m 0700 "${work_dir}"

# This proves that the package-installed helper matches the release-provided
# exact-path AppArmor profile before introducing the narrowly scoped check path.
/usr/bin/enroot-nsenter /bin/true

umask 077
printf '%s\n' \
    'abi <abi/4.0>,' \
    'include <tunables/global>' \
    '' \
    "${helper_path} flags=(unconfined) {" \
    '    allow userns create,' \
    '}' >"${profile_file}"
cp -- "${profile_file}" "${record_dir}/transient-apparmor-profile.txt"

# The release bundle copies enroot-nsenter below its extraction directory.
# Ubuntu's release-provided profile intentionally matches only /usr/bin, so a
# fixed, admin-only extraction path is granted the same rule for this check.
sudo apparmor_parser -r "${profile_file}"
profile_loaded=1
if ! sudo aa-status | grep -Fq "${helper_path}"; then
    printf '%s\n' 'The transient exact-path AppArmor profile did not load.' >&2
    exit 1
fi

(
    cd -- "${work_dir}"
    "${bundle}" --keep
) >"${record_dir}/enroot-check-controlled-path.txt" 2>&1
if ! grep -Fq 'Bundle ran successfully!' \
    "${record_dir}/enroot-check-controlled-path.txt"; then
    printf '%s\n' 'The official Enroot bundle did not report success.' >&2
    exit 1
fi

sudo apparmor_parser -R "${profile_file}"
profile_loaded=0
rm -f -- "${profile_file}"
if sudo aa-status | grep -Fq "${helper_path}"; then
    printf '%s\n' 'The transient AppArmor profile remains loaded.' >&2
    exit 1
fi
if ! sudo aa-status | grep -Fq '/usr/bin/enroot-nsenter'; then
    printf '%s\n' 'The package AppArmor profile disappeared unexpectedly.' >&2
    exit 1
fi
if [[ $(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns) != 1 ]]; then
    printf '%s\n' 'The AppArmor userns restriction changed during validation.' >&2
    exit 1
fi

sudo apt-mark hold enroot enroot+caps >"${record_dir}/apt-hold.txt"
for package in enroot enroot+caps; do
    if ! apt-mark showhold | grep -Fxq "${package}"; then
        printf 'APT hold validation failed: %s\n' "${package}" >&2
        exit 1
    fi
done

codex_uid=$(id -u codexops)
enroot version >"${record_dir}/enroot-version.txt"
enroot info >"${record_dir}/enroot-info.txt"
getcap /usr/bin/enroot-mksquashovlfs /usr/bin/enroot-aufs2ovlfs \
    >"${record_dir}/enroot-capabilities.txt"
sudo aa-status >"${record_dir}/apparmor-status-after-unload.txt"
sha256sum "${bundle}" >"${record_dir}/bundle-sha256.txt"
stat -c '%U:%G %a %n' "${work_dir}" "${rootfs_dir}" \
    >"${record_dir}/controlled-path-metadata.txt"

node_state=$(sinfo -h -n sagsh100server -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf '%s\n' 'Slurm node left DRAIN during Enroot validation.' >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly after Enroot validation.' >&2
    exit 1
fi

cat >"${report_file}" <<EOF
# Enroot installation

- Run ID: ${run_id}
- Enroot version: $(enroot version)
- Official tag: v4.2.1
- Tag commit: ${enroot_commit}
- Packages: enroot=${enroot_version}, enroot+caps=${enroot_version}
- Official asset SHA-256: ${bundle_sha256}
- AppArmor profile: release-provided /etc/apparmor.d/enroot, loaded
- apparmor_restrict_unprivileged_userns: 1 (not weakened)
- Installed /usr/bin/enroot-nsenter namespace check: passed
- Initial self-extracting bundle result: denied because its temporary helper path did not match the exact /usr/bin package profile
- Compatibility check: exact controlled helper path granted the release-equivalent userns rule temporarily
- Transient check profile after validation: unloaded and removed
- Official requirements bundle through controlled path: passed
- Configuration inspection: enroot info (v4.2.1 has no enroot config subcommand)
- Cache path: /srv/gpu-platform/enroot/cache/\${UID}
- Data path: /srv/gpu-platform/enroot/data/\${UID}
- Runtime path: /srv/gpu-platform/enroot/runtime/\${UID}
- Parent modes: root:root 0711
- Per-user modes: user:user 0700
- codexops UID path initialized: ${codex_uid}
- Package holds: enroot, enroot+caps
- Node state: ${node_state}

Status: ENROOT PASSED
EOF

printf '%s\n' 'ENROOT PASSED'
