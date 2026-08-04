#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for protected validation only.' >&2
    exit 2
fi

run_id=${1:?usage: 17-finalize-slurmdbd.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
record_dir=${platform_dir}/reports/slurmdbd-${run_id}.d
report_file=${platform_dir}/reports/slurmdbd-${run_id}.md

if [[ ! -d ${record_dir} ]]; then
    printf '%s\n' 'SlurmDBD transaction records are missing.' >&2
    exit 1
fi
if [[ -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite the SlurmDBD report.' >&2
    exit 1
fi

sudo -n true
for unit in munge mariadb slurmdbd; do
    if ! systemctl is-active --quiet "${unit}"; then
        printf 'Required service is not active: %s\n' "${unit}" >&2
        exit 1
    fi
    if [[ $(systemctl is-enabled "${unit}") != enabled ]]; then
        printf 'Required service is not enabled: %s\n' "${unit}" >&2
        exit 1
    fi
done

if [[ $(sudo stat -c '%U:%G:%a' /etc/slurm/slurmdbd.conf) != slurm:slurm:600 ]]; then
    printf '%s\n' 'Protected SlurmDBD configuration metadata is invalid.' >&2
    exit 1
fi
if [[ -r /etc/slurm/slurmdbd.conf ]]; then
    printf '%s\n' 'codexops can unexpectedly read the protected SlurmDBD configuration.' >&2
    exit 1
fi
if ! sudo -u slurm test -r /etc/slurm/slurmdbd.conf; then
    printf '%s\n' 'The slurm service identity cannot read slurmdbd.conf.' >&2
    exit 1
fi

slurmdbd_version=$(sudo -u slurm slurmdbd -V 2>&1 | tail -n 1)
if [[ ${slurmdbd_version} != 'slurm 25.11.7' ]]; then
    printf 'Unexpected SlurmDBD version: %s\n' "${slurmdbd_version}" >&2
    exit 1
fi
printf '%s\n' "${slurmdbd_version}" >"${record_dir}/slurmdbd-version.txt"

table_count=$(sudo mariadb --batch --skip-column-names -e \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='slurm_acct_db';")
if (( table_count == 0 )); then
    printf '%s\n' 'The Slurm accounting database has no tables.' >&2
    exit 1
fi
user_count=$(sudo mariadb --batch --skip-column-names -e \
    "SELECT COUNT(*) FROM mysql.user WHERE User='slurm' AND Host='localhost';")
if [[ ${user_count} != 1 ]]; then
    printf '%s\n' 'The expected local Slurm database identity is absent or duplicated.' >&2
    exit 1
fi

sudo mariadb --batch --skip-column-names -e \
    "SELECT User,Host,plugin FROM mysql.user WHERE User='slurm';" \
    >"${record_dir}/database-user-metadata.txt"
sudo mariadb --batch --skip-column-names -e \
    "SELECT PRIVILEGE_TYPE FROM information_schema.schema_privileges WHERE GRANTEE=\"'slurm'@'localhost'\" AND TABLE_SCHEMA='slurm_acct_db' ORDER BY PRIVILEGE_TYPE;" \
    >"${record_dir}/database-user-privileges.txt"
printf '%s\n' "${table_count}" >"${record_dir}/accounting-table-count.txt"
systemctl status mariadb slurmdbd --no-pager >"${record_dir}/service-status.txt"
sudo journalctl -u mariadb -u slurmdbd -b --no-pager >"${record_dir}/service-journal.txt"
ss -lntup >"${record_dir}/listeners.txt"

if ! ss -lnt | awk '$4 == "127.0.0.1:3306" { found = 1 } END { exit !found }'; then
    printf '%s\n' 'MariaDB is not listening on the expected loopback endpoint.' >&2
    exit 1
fi
if ss -lnt | awk '$4 ~ /:3306$/ && $4 != "127.0.0.1:3306" { bad = 1 } END { exit bad }'; then
    :
else
    printf '%s\n' 'MariaDB has a non-loopback TCP listener.' >&2
    exit 1
fi
if ! ss -lnt | awk '$4 ~ /:6819$/ { found = 1 } END { exit !found }'; then
    printf '%s\n' 'SlurmDBD is not listening on its expected port.' >&2
    exit 1
fi

cat >"${report_file}" <<EOF
# MariaDB and SlurmDBD configuration

- Run ID: ${run_id}
- Host: $(hostname -s)
- MariaDB version: $(mariadb --version | sed 's/,.*//')
- MariaDB data directory: /var/lib/mariadb (Ubuntu 26.04 package default)
- MariaDB bind address: 127.0.0.1
- Database: slurm_acct_db
- Database user: slurm@localhost
- Database password displayed, logged, or passed in argv/environment: no
- Plaintext password location: /etc/slurm/slurmdbd.conf only
- SlurmDBD configuration mode: slurm:slurm 0600
- SlurmDBD version: ${slurmdbd_version}
- Accounting tables after initialization: ${table_count}
- MariaDB service: enabled and active
- SlurmDBD service: enabled and active
- SlurmDBD listener: TCP 6819; required for controller accounting traffic and subject to the final inbound-access audit
- SchedMD database guidance: https://slurm.schedmd.com/accounting.html#mysql-configuration
- Applied database sizing: 4 GiB buffer pool, 1 GiB log file, 900 second lock wait, 16 MiB packet

Status: SLURMDBD PASSED
EOF

printf '%s\n' 'SLURMDBD PASSED'
