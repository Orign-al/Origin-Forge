#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for protected changes.' >&2
    exit 2
fi

run_id=${1:?usage: 15-configure-munge.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
record_dir=${platform_dir}/reports/munge-${run_id}.d
report_file=${platform_dir}/reports/munge-${run_id}.md
secret_dir=${platform_dir}/secrets
key_file=/etc/munge/munge.key
key_backup=${secret_dir}/munge-key-before-mode-fix-${run_id}.key
account_backup=${secret_dir}/account-db-before-slurm-service-user-${run_id}.tar

if [[ -e ${record_dir} || -e ${report_file} || -e ${key_backup} || -e ${account_backup} ]]; then
    printf '%s\n' 'Refusing to overwrite an existing MUNGE configuration record or secret backup.' >&2
    exit 1
fi

sudo -n true
install -d -m 2770 "${record_dir}"
sudo install -d -o codexops -g gpu-platform-admin -m 0700 "${secret_dir}"

if ! dpkg-query -W -f='${db:Status-Abbrev}\n' munge 2>/dev/null | grep -q '^ii'; then
    printf '%s\n' 'The Ubuntu MUNGE package is not installed.' >&2
    exit 1
fi
if [[ $(command -v mungekey) != /usr/sbin/mungekey ]]; then
    printf '%s\n' 'The system mungekey utility is unavailable at the expected path.' >&2
    exit 1
fi
if sudo test -L "${key_file}" || ! sudo test -f "${key_file}"; then
    printf '%s\n' 'MUNGE key is missing or is not a regular file.' >&2
    exit 1
fi
key_size=$(sudo stat -c '%s' "${key_file}")
if (( key_size < 32 )); then
    printf '%s\n' 'MUNGE key is unexpectedly small.' >&2
    exit 1
fi

sudo stat -c '%F %U:%G %a %s bytes mtime=%y %n' \
    /etc/munge "${key_file}" /var/log/munge /var/lib/munge \
    >"${record_dir}/metadata-before.txt"
sudo cp -a -- "${key_file}" "${key_backup}"
sudo chown root:root "${key_backup}"
sudo chmod 0400 "${key_backup}"

if ! getent group slurm >/dev/null; then
    sudo tar -C /etc -cpf "${account_backup}" passwd group shadow gshadow
    sudo chown root:root "${account_backup}"
    sudo chmod 0400 "${account_backup}"
    sudo addgroup --system slurm
elif ! getent passwd slurm >/dev/null; then
    sudo tar -C /etc -cpf "${account_backup}" passwd group shadow gshadow
    sudo chown root:root "${account_backup}"
    sudo chmod 0400 "${account_backup}"
fi

if ! getent passwd slurm >/dev/null; then
    sudo adduser --system --ingroup slurm --home /nonexistent --no-create-home \
        --shell /usr/sbin/nologin slurm
fi

slurm_uid=$(id -u slurm)
slurm_gid=$(id -g slurm)
if (( slurm_uid >= 1000 || slurm_gid >= 1000 )); then
    printf '%s\n' 'The slurm account is not a system account; stopping.' >&2
    exit 1
fi
if [[ $(getent passwd slurm | cut -d: -f6) != /nonexistent ]] \
    || [[ $(getent passwd slurm | cut -d: -f7) != /usr/sbin/nologin ]]; then
    printf '%s\n' 'The slurm system account has unexpected home or shell settings.' >&2
    exit 1
fi
if [[ $(id -gn slurm) != slurm ]]; then
    printf '%s\n' 'The slurm system account has an unexpected primary group.' >&2
    exit 1
fi

sudo chown munge:munge /etc/munge "${key_file}" /var/log/munge /var/lib/munge
sudo chmod 0700 /etc/munge /var/log/munge
sudo chmod 0711 /var/lib/munge
sudo chmod 0400 "${key_file}"

if [[ $(sudo stat -c '%U:%G:%a' "${key_file}") != munge:munge:400 ]]; then
    printf '%s\n' 'MUNGE key ownership or mode validation failed.' >&2
    exit 1
fi

sudo systemctl enable --now munge
if ! systemctl is-active --quiet munge; then
    printf '%s\n' 'MUNGE service did not become active.' >&2
    exit 1
fi

credential_file=$(mktemp)
decoded_file=$(mktemp)
cleanup_credentials() {
    rm -f -- "${credential_file}" "${decoded_file}"
}
trap cleanup_credentials EXIT
chmod 0600 "${credential_file}" "${decoded_file}"
munge -n >"${credential_file}"
unmunge <"${credential_file}" >"${decoded_file}"
if ! grep -Eq '^STATUS:[[:space:]]+Success \(0\)$' "${decoded_file}"; then
    printf '%s\n' 'MUNGE encode/decode status was not successful.' >&2
    exit 1
fi
if ! grep -Eq '^UID:[[:space:]]+codexops \(' "${decoded_file}"; then
    printf '%s\n' 'Decoded MUNGE credential does not identify codexops.' >&2
    exit 1
fi
cleanup_credentials
trap - EXIT

sudo stat -c '%F %U:%G %a %s bytes mtime=%y %n' \
    /etc/munge "${key_file}" /var/log/munge /var/lib/munge /run/munge \
    >"${record_dir}/metadata-after.txt"
getent passwd slurm >"${record_dir}/slurm-service-account.txt"
getent group slurm >"${record_dir}/slurm-service-group.txt"
systemctl status munge --no-pager >"${record_dir}/munge-status.txt"
sudo journalctl -u munge -b --no-pager >"${record_dir}/munge-journal.txt"

cat >"${report_file}" <<EOF
# MUNGE configuration

- Run ID: ${run_id}
- Package version: $(dpkg-query -W -f='${Version}' munge)
- Key origin: generated during this fresh package installation by the Ubuntu package using the system mungekey utility
- Key content displayed or logged: no
- Key owner/group/mode: munge:munge 0400
- Local encode/decode: passed for codexops
- localhost SSH encode/decode: not attempted; the server intentionally has no administrator private key for localhost SSH
- Service: enabled and active
- Slurm service account: system UID ${slurm_uid}, system GID ${slurm_gid}, non-login shell

Status: MUNGE PASSED
EOF

printf '%s\n' 'MUNGE PASSED'
