#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for protected validation and package holds.' >&2
    exit 2
fi

run_id=${1:?usage: 29-complete-enroot-install.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly enroot_version=4.2.1-1
readonly enroot_commit=ead3a25ed974948235d28f99fc387ac82435ce62
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/enroot-4.2.1-${run_id}
record_dir=${platform_dir}/reports/enroot-install-completion-${run_id}.d
report_file=${platform_dir}/reports/enroot-install-${run_id}.md
check_bundle=${artifact_dir}/enroot-check_4.2.1_x86_64.run
config_source=${platform_dir}/config/enroot-platform.conf
config_target=/etc/enroot/enroot.conf.d/90-h100-platform.conf

if [[ ! -d ${platform_dir}/reports/enroot-install-finalize-${run_id}.d ]]; then
    printf '%s\n' 'The capability assertion audit record is missing.' >&2
    exit 1
fi
if [[ -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite Enroot completion records.' >&2
    exit 1
fi

sudo -n true
install -d -m 2770 "${record_dir}"
for package in enroot enroot+caps; do
    if [[ $(dpkg-query -W -f='${Version}' "${package}") != "${enroot_version}" ]] \
        || [[ $(dpkg-query -W -f='${db:Status-Abbrev}' "${package}") != ii* ]]; then
        printf 'Enroot package validation failed: %s\n' "${package}" >&2
        exit 1
    fi
done
if ! sudo cmp -s "${config_source}" "${config_target}"; then
    printf '%s\n' 'Installed Enroot drop-in differs from the reviewed source.' >&2
    exit 1
fi
if [[ $(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns) != 1 ]]; then
    printf '%s\n' 'The Ubuntu AppArmor userns restriction was weakened.' >&2
    exit 1
fi
sudo aa-status >"${record_dir}/apparmor-status.txt"
if ! grep -Fq '/usr/bin/enroot-nsenter' "${record_dir}/apparmor-status.txt"; then
    printf '%s\n' 'The Enroot AppArmor profile is not loaded.' >&2
    exit 1
fi

codex_uid=$(id -u codexops)
enroot version >"${record_dir}/enroot-version.txt"
enroot info >"${record_dir}/enroot-info.txt"
for expected in \
    "ENROOT_CACHE_PATH=/srv/gpu-platform/enroot/cache/${codex_uid}" \
    "ENROOT_DATA_PATH=/srv/gpu-platform/enroot/data/${codex_uid}" \
    "ENROOT_RUNTIME_PATH=/srv/gpu-platform/enroot/runtime/${codex_uid}"; do
    if ! grep -Fxq "  ${expected}" "${record_dir}/enroot-info.txt"; then
        printf 'Enroot path validation failed: %s\n' "${expected%%=*}" >&2
        exit 1
    fi
done
for parent in cache data runtime; do
    parent_dir=/srv/gpu-platform/enroot/${parent}
    user_dir=${parent_dir}/${codex_uid}
    if [[ $(sudo stat -c '%U:%G:%a' "${parent_dir}") != root:root:711 ]] \
        || [[ $(sudo stat -c '%U:%G:%a' "${user_dir}") != codexops:codexops:700 ]]; then
        printf 'Enroot storage metadata validation failed: %s\n' "${parent}" >&2
        exit 1
    fi
    if sudo -u nobody test -r "${user_dir}"; then
        printf 'Cross-user Enroot read access detected: %s\n' "${user_dir}" >&2
        exit 1
    fi
done

getcap /usr/bin/enroot-mksquashovlfs /usr/bin/enroot-aufs2ovlfs \
    >"${record_dir}/enroot-capabilities.txt"
if ! grep -Fq 'enroot-mksquashovlfs cap_sys_admin=ep' "${record_dir}/enroot-capabilities.txt"; then
    printf '%s\n' 'enroot-mksquashovlfs capability validation failed.' >&2
    exit 1
fi
if ! grep -Eq 'enroot-aufs2ovlfs .*(cap_mknod,cap_sys_admin|cap_sys_admin,cap_mknod)=ep' \
    "${record_dir}/enroot-capabilities.txt"; then
    printf '%s\n' 'enroot-aufs2ovlfs capability validation failed.' >&2
    exit 1
fi

chmod 0750 "${check_bundle}"
"${check_bundle}" >"${record_dir}/enroot-check-run.txt" 2>&1
if ! grep -Fq 'Bundle ran successfully!' "${record_dir}/enroot-check-run.txt"; then
    printf '%s\n' 'The official Enroot requirements bundle did not report success.' >&2
    exit 1
fi

sudo apt-mark hold enroot enroot+caps >"${record_dir}/apt-hold.txt"
for package in enroot enroot+caps; do
    if ! apt-mark showhold | grep -Fxq "${package}"; then
        printf 'APT hold validation failed: %s\n' "${package}" >&2
        exit 1
    fi
done

node_state=$(sinfo -h -n sagsh100server -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf '%s\n' 'Slurm node left DRAIN during Enroot completion.' >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly after Enroot completion.' >&2
    exit 1
fi

dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' \
    enroot enroot+caps parallel fuse-overlayfs squashfuse \
    >"${record_dir}/package-versions.txt"
sudo stat -c '%F %U:%G %a %n' /srv/gpu-platform/enroot/{cache,data,runtime} \
    /srv/gpu-platform/enroot/{cache,data,runtime}/${codex_uid} \
    >"${record_dir}/storage-metadata.txt"

cat >"${report_file}" <<EOF
# Enroot installation

- Run ID: ${run_id}
- Enroot version: $(enroot version)
- Official tag: v4.2.1
- Tag commit: ${enroot_commit}
- Packages: enroot=${enroot_version}, enroot+caps=${enroot_version}
- AppArmor profile: release-provided /etc/apparmor.d/enroot, loaded
- apparmor_restrict_unprivileged_userns: 1 (not weakened)
- Official requirements bundle: passed
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
