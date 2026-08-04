#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; downloads and inspection are unprivileged.' >&2
    exit 2
fi

run_id=${1:?usage: 25-preflight-enroot.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly enroot_tag=v4.2.1
readonly enroot_version=4.2.1-1
readonly enroot_commit=ead3a25ed974948235d28f99fc387ac82435ce62
readonly release_base=https://github.com/NVIDIA/enroot/releases/download/v4.2.1
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/enroot-4.2.1-${run_id}
record_dir=${platform_dir}/reports/enroot-preflight-${run_id}.d
report_file=${platform_dir}/reports/enroot-version-decision-${run_id}.md
base_deb=${artifact_dir}/enroot_4.2.1-1_amd64.deb
caps_deb=${artifact_dir}/enroot+caps_4.2.1-1_amd64.deb
check_bundle=${artifact_dir}/enroot-check_4.2.1_x86_64.run
simulation=${record_dir}/apt-simulation.txt

if [[ -e ${artifact_dir} || -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite Enroot artifacts or preflight records.' >&2
    exit 1
fi

sudo -n true
install -d -m 2770 "${artifact_dir}" "${record_dir}"

if dpkg-query -W -f='${db:Status-Abbrev}\n' enroot 2>/dev/null | grep -q '^ii'; then
    printf '%s\n' 'Enroot is already installed unexpectedly.' >&2
    exit 1
fi
if sudo test -e /etc/enroot || sudo test -e /etc/apparmor.d/enroot; then
    printf '%s\n' 'An unknown Enroot or Enroot AppArmor configuration exists.' >&2
    exit 1
fi
if [[ $(sinfo -h -n sagsh100server -o '%T') != drain* ]] \
    && [[ $(sinfo -h -n sagsh100server -o '%T') != drained* ]]; then
    printf '%s\n' 'Slurm node is not drained.' >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly during Enroot preflight.' >&2
    exit 1
fi

curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --output "${base_deb}" "${release_base}/enroot_4.2.1-1_amd64.deb"
curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --output "${caps_deb}" "${release_base}/enroot+caps_4.2.1-1_amd64.deb"
curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --output "${check_bundle}" "${release_base}/enroot-check_4.2.1_x86_64.run"
chmod 0750 "${check_bundle}"

(
    cd -- "${artifact_dir}"
    sha256sum -- enroot_4.2.1-1_amd64.deb enroot+caps_4.2.1-1_amd64.deb \
        enroot-check_4.2.1_x86_64.run >SHA256SUMS
)

for package_file in "${base_deb}" "${caps_deb}"; do
    dpkg-deb --info "${package_file}" >>"${record_dir}/deb-info.txt"
    dpkg-deb --contents "${package_file}" >>"${record_dir}/deb-contents.txt"
done
if [[ $(dpkg-deb --field "${base_deb}" Package) != enroot ]] \
    || [[ $(dpkg-deb --field "${base_deb}" Version) != "${enroot_version}" ]] \
    || [[ $(dpkg-deb --field "${base_deb}" Architecture) != amd64 ]]; then
    printf '%s\n' 'The Enroot base package metadata is unexpected.' >&2
    exit 1
fi
if [[ $(dpkg-deb --field "${caps_deb}" Package) != enroot+caps ]] \
    || [[ $(dpkg-deb --field "${caps_deb}" Version) != "${enroot_version}" ]] \
    || [[ $(dpkg-deb --field "${caps_deb}" Architecture) != amd64 ]]; then
    printf '%s\n' 'The Enroot capabilities package metadata is unexpected.' >&2
    exit 1
fi
if ! grep -Fq 'etc/apparmor.d/enroot' "${record_dir}/deb-contents.txt"; then
    printf '%s\n' 'The official Enroot package lacks its AppArmor profile.' >&2
    exit 1
fi

"${check_bundle}" --verify >"${record_dir}/enroot-check-verify.txt"

sudo apt-get -s -o Debug::NoLocking=1 install --no-install-recommends \
    "${base_deb}" "${caps_deb}" parallel fuse-overlayfs squashfuse \
    | tee "${simulation}"
if grep -q '^Remv ' "${simulation}"; then
    printf '%s\n' 'Enroot APT simulation contains removals.' >&2
    exit 1
fi
if grep -Eqi 'downgrad|DOWNGRADED' "${simulation}"; then
    printf '%s\n' 'Enroot APT simulation contains a downgrade.' >&2
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
- Selection reason: official standard HPC flavor; +caps is required for unprivileged image import/conversion
- Hardened flavor selected: no; its documented overhead is not required for this trusted internal HPC model
- AppArmor sysctl changed: no
- AppArmor plan: install and load the release-provided profile because Ubuntu reports apparmor_restrict_unprivileged_userns=1
- Artifact checksum manifest: ${artifact_dir}/SHA256SUMS
- APT transaction: zero upgrades and zero removals

Status: ENROOT PREFLIGHT PASSED
EOF

printf 'ENROOT_TAG=%s\n' "${enroot_tag}"
printf 'ENROOT_COMMIT=%s\n' "${enroot_commit}"
printf 'ENROOT_ARTIFACT_DIR=%s\n' "${artifact_dir}"
printf '%s\n' 'ENROOT PREFLIGHT PASSED'
