#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for package and system configuration changes.' >&2
    exit 2
fi

run_id=${1:?usage: 27-install-enroot.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly enroot_version=4.2.1-1
readonly enroot_commit=ead3a25ed974948235d28f99fc387ac82435ce62
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/enroot-4.2.1-${run_id}
record_dir=${platform_dir}/reports/enroot-install-${run_id}.d
report_file=${platform_dir}/reports/enroot-install-${run_id}.md
backup_dir=${platform_dir}/backups
base_deb=${artifact_dir}/enroot_4.2.1-1_amd64.deb
caps_deb=${artifact_dir}/enroot+caps_4.2.1-1_amd64.deb
check_bundle=${artifact_dir}/enroot-check_4.2.1_x86_64.run
config_source=${platform_dir}/config/enroot-platform.conf
config_target=/etc/enroot/enroot.conf.d/90-h100-platform.conf

if [[ ! -f ${platform_dir}/reports/enroot-version-decision-${run_id}.md ]]; then
    printf '%s\n' 'The passed Enroot preflight report is missing.' >&2
    exit 1
fi
if [[ ! -f ${config_source} ]]; then
    printf '%s\n' 'The tracked per-UID Enroot configuration is missing.' >&2
    exit 1
fi
if [[ -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite Enroot installation records.' >&2
    exit 1
fi

sudo -n true
install -d -m 2770 "${record_dir}" "${backup_dir}"

if dpkg-query -W -f='${db:Status-Abbrev}\n' enroot 2>/dev/null | grep -q '^ii'; then
    printf '%s\n' 'Enroot is already installed.' >&2
    exit 1
fi
if sudo test -e /etc/enroot || sudo test -e /etc/apparmor.d/enroot; then
    printf '%s\n' 'An Enroot configuration appeared after preflight.' >&2
    exit 1
fi
node_state=$(sinfo -h -n sagsh100server -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf '%s\n' 'Slurm node is not drained.' >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly before Enroot installation.' >&2
    exit 1
fi

cat >"${record_dir}/EXPECTED_SHA256SUMS" <<'EOF'
5955c51e88df0c5ed538d87abb5264609d4bad55fa8570745ed5d4ec66c8ab91  enroot_4.2.1-1_amd64.deb
ca22652dcbdd3ebb6e2c68df781ef4301647c79dd7a41ed62899c2fd0f692750  enroot+caps_4.2.1-1_amd64.deb
8f39b28312d24c0fc8ae35386906bf51238934bee54df583b63ea99175ba1eea  enroot-check_4.2.1_x86_64.run
EOF
(
    cd -- "${artifact_dir}"
    sha256sum --check "${record_dir}/EXPECTED_SHA256SUMS"
) | tee "${record_dir}/checksum-validation.txt"

dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' \
    >"${record_dir}/dpkg-before.txt"
sudo cp -a /var/lib/dpkg/status "${backup_dir}/dpkg-status-before-enroot-${run_id}"
sudo chmod 0640 "${backup_dir}/dpkg-status-before-enroot-${run_id}"

sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    "${base_deb}" "${caps_deb}" parallel fuse-overlayfs squashfuse \
    >"${record_dir}/apt-install.txt" 2>&1

audit_result=$(dpkg --audit)
if [[ -n ${audit_result} ]]; then
    printf '%s\n' "${audit_result}" >&2
    exit 1
fi
for package in enroot enroot+caps; do
    if [[ $(dpkg-query -W -f='${Version}' "${package}") != "${enroot_version}" ]] \
        || [[ $(dpkg-query -W -f='${db:Status-Abbrev}' "${package}") != ii* ]]; then
        printf 'Installed Enroot package validation failed: %s\n' "${package}" >&2
        exit 1
    fi
done

dpkg-deb --extract "${base_deb}" "${record_dir}/package-root"
if ! sudo cmp -s "${record_dir}/package-root/etc/apparmor.d/enroot" /etc/apparmor.d/enroot; then
    printf '%s\n' 'Installed Enroot AppArmor profile differs from the official package.' >&2
    exit 1
fi
sudo apparmor_parser -r /etc/apparmor.d/enroot
sudo aa-status >"${record_dir}/apparmor-status.txt"
if ! grep -Fq '/usr/bin/enroot-nsenter' "${record_dir}/apparmor-status.txt"; then
    printf '%s\n' 'The Enroot AppArmor profile is not loaded.' >&2
    exit 1
fi
if [[ $(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns) != 1 ]]; then
    printf '%s\n' 'The Ubuntu unprivileged-userns AppArmor restriction was unexpectedly changed.' >&2
    exit 1
fi

sudo tar --acls --xattrs -C /etc -cpf "${backup_dir}/etc-enroot-package-default-${run_id}.tar" enroot
if sudo test -e "${config_target}"; then
    printf '%s\n' 'The platform Enroot drop-in unexpectedly exists.' >&2
    exit 1
fi
sudo install -o root -g root -m 0644 "${config_source}" "${config_target}"

codex_uid=$(id -u codexops)
codex_gid=$(id -g codexops)
if [[ ! ${codex_uid} =~ ^[0-9]+$ || ! ${codex_gid} =~ ^[0-9]+$ ]]; then
    printf '%s\n' 'Unable to determine numeric codexops identity.' >&2
    exit 1
fi
sudo stat -c '%F %U:%G %a %n' /srv/gpu-platform/enroot/{cache,data,runtime} \
    >"${record_dir}/storage-metadata-before.txt"
for parent in cache data runtime; do
    sudo install -d -o root -g root -m 0711 "/srv/gpu-platform/enroot/${parent}"
    sudo install -d -o codexops -g codexops -m 0700 \
        "/srv/gpu-platform/enroot/${parent}/${codex_uid}"
done

enroot version >"${record_dir}/enroot-version.txt"
enroot info >"${record_dir}/enroot-info.txt"
for expected in \
    "ENROOT_CACHE_PATH=/srv/gpu-platform/enroot/cache/${codex_uid}" \
    "ENROOT_DATA_PATH=/srv/gpu-platform/enroot/data/${codex_uid}" \
    "ENROOT_RUNTIME_PATH=/srv/gpu-platform/enroot/runtime/${codex_uid}"; do
    if ! grep -Fxq "  ${expected}" "${record_dir}/enroot-info.txt"; then
        printf 'Enroot effective path validation failed: %s\n' "${expected%%=*}" >&2
        exit 1
    fi
done

getcap /usr/bin/enroot-mksquashovlfs /usr/bin/enroot-aufs2ovlfs \
    >"${record_dir}/enroot-capabilities.txt"
if ! grep -Fq 'enroot-mksquashovlfs cap_sys_admin=ep' "${record_dir}/enroot-capabilities.txt"; then
    printf '%s\n' 'enroot-mksquashovlfs lacks the official +caps capability.' >&2
    exit 1
fi
if ! grep -Eq 'enroot-aufs2ovlfs .*cap_(mknod,sys_admin|sys_admin,mknod)=ep' \
    "${record_dir}/enroot-capabilities.txt"; then
    printf '%s\n' 'enroot-aufs2ovlfs lacks the official +caps capabilities.' >&2
    exit 1
fi

for parent in cache data runtime; do
    user_dir=/srv/gpu-platform/enroot/${parent}/${codex_uid}
    if [[ $(sudo stat -c '%U:%G:%a' "${user_dir}") != codexops:codexops:700 ]]; then
        printf 'Per-UID Enroot directory metadata is invalid: %s\n' "${user_dir}" >&2
        exit 1
    fi
    if sudo -u nobody test -r "${user_dir}"; then
        printf 'The nobody identity can read a private Enroot directory: %s\n' "${user_dir}" >&2
        exit 1
    fi
done

chmod 0750 "${check_bundle}"
"${check_bundle}" >"${record_dir}/enroot-check-run.txt" 2>&1
if ! grep -Fq 'Bundle ran successfully!' "${record_dir}/enroot-check-run.txt"; then
    printf '%s\n' 'The official Enroot requirements bundle did not report success.' >&2
    exit 1
fi

sudo apt-mark hold enroot enroot+caps >"${record_dir}/apt-hold.txt"
dpkg-query -W -f='${binary:Package}\t${Version}\t${db:Status-Abbrev}\n' \
    >"${record_dir}/dpkg-after.txt"
sudo stat -c '%F %U:%G %a %n' /srv/gpu-platform/enroot/{cache,data,runtime} \
    /srv/gpu-platform/enroot/{cache,data,runtime}/${codex_uid} \
    >"${record_dir}/storage-metadata-after.txt"

node_state=$(sinfo -h -n sagsh100server -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf '%s\n' 'Slurm node left DRAIN during Enroot installation.' >&2
    exit 1
fi

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
- Configuration inspection: enroot info (v4.2.1 does not expose an enroot config subcommand)
- Cache path: /srv/gpu-platform/enroot/cache/\${UID}
- Data path: /srv/gpu-platform/enroot/data/\${UID}
- Runtime path: /srv/gpu-platform/enroot/runtime/\${UID}
- Parent modes: root:root 0711
- Per-user modes: user:user 0700
- codexops UID path initialized: ${codex_uid}
- Other users' writable layers readable by codexops/nobody: no cross-user access granted
- Package holds: enroot, enroot+caps
- Node state: ${node_state}

Status: ENROOT PASSED
EOF

printf '%s\n' 'ENROOT PASSED'
