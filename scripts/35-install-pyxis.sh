#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for the reviewed system installation.' >&2
    exit 2
fi

run_id=${1:?usage: 35-install-pyxis.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

readonly pyxis_version=0.24.0
readonly plugin_sha256=72071897de9c4f34fa92f9414728954be6027f9d3a5ea0427b39ca074747edd9
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
artifact_dir=${platform_dir}/artifacts/pyxis-${pyxis_version}-${run_id}
build_dir=${artifact_dir}/build
stage_plugin=${artifact_dir}/stage/usr/local/lib/slurm/spank_pyxis.so
record_dir=${platform_dir}/reports/pyxis-install-${run_id}.d
report_file=${platform_dir}/reports/pyxis-install-${run_id}.md
backup_file=${platform_dir}/backups/etc-slurm-before-pyxis-${run_id}.tar
plugstack_source=${platform_dir}/config/plugstack.conf
pyxis_conf_source=${platform_dir}/config/pyxis.conf
plugin_target=/usr/local/lib/slurm/spank_pyxis.so
share_conf_target=/usr/local/share/pyxis/pyxis.conf
plugstack_target=/etc/slurm/plugstack.conf
plugstack_dir=/etc/slurm/plugstack.conf.d
pyxis_conf_target=${plugstack_dir}/pyxis.conf
mutated=0

rollback() {
    local rc=$?
    if (( rc != 0 && mutated == 1 )); then
        printf '%s\n' 'Pyxis installation failed; removing only files created by this phase.' >&2
        sudo rm -f -- "${pyxis_conf_target}" "${plugstack_target}" \
            "${share_conf_target}" "${plugin_target}" || true
        sudo rmdir -- "${plugstack_dir}" /usr/local/share/pyxis \
            /usr/local/lib/slurm 2>/dev/null || true
        if sudo systemctl restart slurmd; then
            printf '%s\n' 'rollback=created-files-removed; slurmd-restarted' \
                >"${record_dir}/rollback.txt"
        else
            printf '%s\n' 'rollback=created-files-removed; slurmd-restart-failed' \
                >"${record_dir}/rollback.txt"
        fi
    fi
    exit "${rc}"
}
trap rollback EXIT

if [[ ! -f ${platform_dir}/reports/pyxis-build-${run_id}.md ]] \
    || ! grep -Fxq 'Status: PYXIS BUILD STAGED PASSED' \
        "${platform_dir}/reports/pyxis-build-${run_id}.md"; then
    printf '%s\n' 'A passed staged Pyxis build report is required.' >&2
    exit 1
fi
if [[ -e ${record_dir} || -e ${report_file} || -e ${backup_file} ]]; then
    printf '%s\n' 'Refusing to overwrite Pyxis install records or backup.' >&2
    exit 1
fi
if [[ $(sha256sum "${stage_plugin}" | awk '{print $1}') != "${plugin_sha256}" ]]; then
    printf '%s\n' 'The reviewed staged Pyxis plugin checksum changed.' >&2
    exit 1
fi
if [[ $(<"${plugstack_source}") != 'include /etc/slurm/plugstack.conf.d/*' ]] \
    || [[ $(<"${pyxis_conf_source}") != 'required /usr/local/lib/slurm/spank_pyxis.so' ]]; then
    printf '%s\n' 'The tracked Pyxis configuration sources are unexpected.' >&2
    exit 1
fi

sudo -n true
node_state_before=$(sinfo -h -n sagsh100server -o '%T')
if [[ ${node_state_before} != drain* && ${node_state_before} != drained* ]]; then
    printf '%s\n' 'Slurm node is not drained.' >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly before Pyxis installation.' >&2
    exit 1
fi
if [[ $(srun --version) != 'slurm 25.11.7' ]] \
    || [[ $(dpkg-query -W -f='${Version}' slurm-smd-dev) != 25.11.7-1h100.1 ]]; then
    printf '%s\n' 'The exact Slurm runtime/header contract changed.' >&2
    exit 1
fi
for service in munge slurmdbd slurmctld slurmd; do
    if [[ $(systemctl is-active "${service}") != active ]]; then
        printf 'Required service is not active: %s\n' "${service}" >&2
        exit 1
    fi
done
for target in \
    /usr/local/lib/slurm /usr/local/share/pyxis \
    "${plugstack_target}" "${plugstack_dir}"; do
    if sudo test -e "${target}"; then
        printf 'Unknown target appeared before Pyxis installation: %s\n' "${target}" >&2
        exit 1
    fi
done

install -d -m 2770 "${record_dir}"
sudo find /etc/slurm -xdev -printf '%M %u:%g %s %p\n' | sort \
    >"${record_dir}/etc-slurm-before.txt"
sudo tar --acls --xattrs --numeric-owner -C /etc -cpf "${backup_file}" slurm
sudo chown root:gpu-platform-admin "${backup_file}"
sudo chmod 0640 "${backup_file}"
sha256sum "${backup_file}" >"${record_dir}/backup-sha256.txt"
systemctl cat slurmd >"${record_dir}/slurmd-unit.txt"
systemctl show slurmd -p ActiveState -p SubState -p MainPID \
    >"${record_dir}/slurmd-before.txt"

mutated=1
sudo make -C "${build_dir}" CC=/usr/bin/gcc install \
    >"${record_dir}/make-system-install.txt" 2>&1
sudo install -d -o root -g root -m 0755 "${plugstack_dir}"
sudo install -o root -g root -m 0644 "${plugstack_source}" "${plugstack_target}"
sudo install -o root -g root -m 0644 "${pyxis_conf_source}" "${pyxis_conf_target}"

if [[ $(sudo sha256sum "${plugin_target}" | awk '{print $1}') != "${plugin_sha256}" ]] \
    || ! sudo cmp -s "${share_conf_target}" "${pyxis_conf_source}" \
    || ! sudo cmp -s "${plugstack_target}" "${plugstack_source}" \
    || ! sudo cmp -s "${pyxis_conf_target}" "${pyxis_conf_source}"; then
    printf '%s\n' 'Installed Pyxis files differ from reviewed staging/configuration.' >&2
    exit 1
fi
for target in "${plugin_target}" "${share_conf_target}" \
    "${plugstack_target}" "${pyxis_conf_target}"; do
    if [[ $(sudo stat -c '%U:%G:%a' "${target}") != root:root:644 ]]; then
        printf 'Installed Pyxis file metadata is unexpected: %s\n' "${target}" >&2
        exit 1
    fi
done

srun --help >"${record_dir}/srun-help-before-restart.txt" \
    2>"${record_dir}/srun-help-before-restart.stderr"
if [[ -s ${record_dir}/srun-help-before-restart.stderr ]] \
    || ! grep -Fq -- '--container-image' "${record_dir}/srun-help-before-restart.txt" \
    || ! grep -Fq '[pyxis]' "${record_dir}/srun-help-before-restart.txt"; then
    printf '%s\n' 'The installed Pyxis plugin did not load cleanly in srun.' >&2
    exit 1
fi

restart_since=$(date --iso-8601=seconds)
sudo systemctl restart slurmd
if [[ $(systemctl is-active slurmd) != active ]] \
    || [[ $(systemctl is-enabled slurmd) != enabled ]]; then
    printf '%s\n' 'slurmd failed its post-install service check.' >&2
    exit 1
fi
sudo journalctl -u slurmd --since "${restart_since}" --no-pager \
    >"${record_dir}/slurmd-journal-after-restart.txt"
if grep -Eqi 'Incompatible plugin version|fatal|segfault' \
    "${record_dir}/slurmd-journal-after-restart.txt"; then
    printf '%s\n' 'A critical Pyxis/slurmd error appeared after restart.' >&2
    exit 1
fi

srun --help >"${record_dir}/srun-help-after-restart.txt" \
    2>"${record_dir}/srun-help-after-restart.stderr"
if [[ -s ${record_dir}/srun-help-after-restart.stderr ]] \
    || ! grep -Fq -- '--container-image' "${record_dir}/srun-help-after-restart.txt" \
    || ! grep -Fq '[pyxis]' "${record_dir}/srun-help-after-restart.txt"; then
    printf '%s\n' 'Pyxis option registration failed after slurmd restart.' >&2
    exit 1
fi
container_help_rc=0
srun --container-help >"${record_dir}/srun-container-help.txt" \
    2>"${record_dir}/srun-container-help.stderr" || container_help_rc=$?
if (( container_help_rc != 0 )) \
    && ! grep -Fq "unrecognized option '--container-help'" \
        "${record_dir}/srun-container-help.stderr"; then
    printf 'Unexpected optional container help result: %s\n' "${container_help_rc}" >&2
    exit 1
fi

if grep -FRqi 'Incompatible plugin version' "${record_dir}"; then
    printf '%s\n' 'Incompatible plugin version found in installation records.' >&2
    exit 1
fi
node_state_after=$(sinfo -h -n sagsh100server -o '%T')
if [[ ${node_state_after} != drain* && ${node_state_after} != drained* ]]; then
    printf '%s\n' 'Slurm node left DRAIN during Pyxis installation.' >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly after Pyxis installation.' >&2
    exit 1
fi

sudo stat -c '%F %U:%G %a %s %n' "${plugin_target}" "${share_conf_target}" \
    "${plugstack_target}" "${plugstack_dir}" "${pyxis_conf_target}" \
    >"${record_dir}/installed-files.txt"
systemctl show slurmd -p ActiveState -p SubState -p MainPID \
    >"${record_dir}/slurmd-after.txt"
systemctl --failed --no-legend >"${record_dir}/systemd-failed.txt" || true

cat >"${report_file}" <<EOF
# Pyxis installation

- Run ID: ${run_id}
- Pyxis version: v${pyxis_version}
- Source: NVIDIA/pyxis official tag v${pyxis_version}
- Built against: slurm-smd-dev 25.11.7-1h100.1
- Slurm runtime: $(srun --version)
- Plugin: ${plugin_target}
- Plugin SHA-256: ${plugin_sha256}
- Plugstack root: ${plugstack_target}
- Plugstack include: include /etc/slurm/plugstack.conf.d/*
- Pyxis entry: required /usr/local/lib/slurm/spank_pyxis.so
- srun option registration before restart: passed
- slurmd restart: passed
- srun option registration after restart: passed
- Optional srun --container-help exit: ${container_help_rc} (unsupported is permitted)
- Incompatible plugin version: not present
- Backup: ${backup_file}
- Node state before: ${node_state_before}
- Node state after: ${node_state_after}
- Execution tests: pending explicit temporary RESUME approval

Status: PYXIS INSTALL PASSED
EOF

mutated=0
printf '%s\n' 'PYXIS INSTALL PASSED'
