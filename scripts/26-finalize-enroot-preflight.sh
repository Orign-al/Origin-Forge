#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; no root installation is performed.' >&2
    exit 2
fi

run_id=${1:?usage: 26-finalize-enroot-preflight.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly enroot_tag=v4.2.1
readonly enroot_version=4.2.1-1
readonly enroot_commit=ead3a25ed974948235d28f99fc387ac82435ce62
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/enroot-4.2.1-${run_id}
record_dir=${platform_dir}/reports/enroot-preflight-finalize-${run_id}.d
report_file=${platform_dir}/reports/enroot-version-decision-${run_id}.md
base_deb=${artifact_dir}/enroot_4.2.1-1_amd64.deb
caps_deb=${artifact_dir}/enroot+caps_4.2.1-1_amd64.deb
check_bundle=${artifact_dir}/enroot-check_4.2.1_x86_64.run
simulation=${record_dir}/apt-simulation.txt

if [[ ! -d ${platform_dir}/reports/enroot-preflight-${run_id}.d ]]; then
    printf '%s\n' 'The stopped Enroot preflight audit record is missing.' >&2
    exit 1
fi
if [[ -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite Enroot preflight finalization records.' >&2
    exit 1
fi

sudo -n true
install -d -m 2770 "${record_dir}"
if dpkg-query -W -f='${db:Status-Abbrev}\n' enroot 2>/dev/null | grep -q '^ii'; then
    printf '%s\n' 'Enroot was installed during the paused preflight.' >&2
    exit 1
fi
if sudo test -e /etc/enroot || sudo test -e /etc/apparmor.d/enroot; then
    printf '%s\n' 'An Enroot configuration appeared during the paused preflight.' >&2
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

if ! dpkg-deb --contents "${base_deb}" >"${record_dir}/base-package-contents.txt"; then
    printf '%s\n' 'Unable to inspect the Enroot package.' >&2
    exit 1
fi
if ! grep -Fq 'etc/apparmor.d/enroot' "${record_dir}/base-package-contents.txt"; then
    printf '%s\n' 'The official package lacks its persistent AppArmor profile.' >&2
    exit 1
fi
if [[ $(dpkg-deb --field "${base_deb}" Package) != enroot ]] \
    || [[ $(dpkg-deb --field "${base_deb}" Version) != "${enroot_version}" ]]; then
    printf '%s\n' 'The Enroot base package metadata changed.' >&2
    exit 1
fi
if [[ $(dpkg-deb --field "${caps_deb}" Package) != enroot+caps ]] \
    || [[ $(dpkg-deb --field "${caps_deb}" Version) != "${enroot_version}" ]]; then
    printf '%s\n' 'The Enroot capabilities package metadata changed.' >&2
    exit 1
fi

chmod 0750 "${check_bundle}"
"${check_bundle}" --verify >"${record_dir}/enroot-check-verify.txt"

sudo apt-get -s -o Debug::NoLocking=1 install --no-install-recommends \
    "${base_deb}" "${caps_deb}" parallel fuse-overlayfs squashfuse \
    | tee "${simulation}"
if grep -q '^Remv ' "${simulation}" || grep -Eqi 'downgrad|DOWNGRADED' "${simulation}"; then
    printf '%s\n' 'Enroot APT simulation contains a removal or downgrade.' >&2
    exit 1
fi
if ! grep -Eq '^0 upgraded, [0-9]+ newly installed, 0 to remove( and [0-9]+ not upgraded)?\.$' \
    "${simulation}"; then
    printf '%s\n' 'Enroot APT simulation is not a zero-upgrade, zero-remove transaction.' >&2
    exit 1
fi
for package in enroot enroot+caps; do
    if ! awk -v package="${package}" -v version="${enroot_version}" '
        $1 == "Inst" && $2 == package && index($0, "(" version " ") { found = 1 }
        END { exit !found }
    ' "${simulation}"; then
        printf 'APT did not select the pinned package: %s %s\n' "${package}" "${enroot_version}" >&2
        exit 1
    fi
done

cat >"${report_file}" <<EOF
# Enroot version decision

- Stable release selected: ${enroot_tag}
- Release date: 2026-06-09
- Tag commit: ${enroot_commit}
- Official release: https://github.com/NVIDIA/enroot/releases/tag/${enroot_tag}
- Package flavor: standard plus capabilities
- Selection reason: official standard HPC flavor; +caps enables unprivileged image import/conversion
- Hardened flavor selected: no; its documented overhead is not required for this trusted internal HPC model
- AppArmor sysctl changed: no
- AppArmor plan: use the persistent /etc/apparmor.d/enroot profile shipped in the release asset
- Artifact checksum manifest: ${record_dir}/EXPECTED_SHA256SUMS
- APT transaction: zero upgrades and zero removals

Status: ENROOT PREFLIGHT PASSED
EOF

printf '%s\n' 'ENROOT PREFLIGHT PASSED'
