#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for protected database and configuration changes.' >&2
    exit 2
fi

run_id=${1:?usage: 16-configure-slurmdbd.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
record_dir=${platform_dir}/reports/slurmdbd-${run_id}.d
report_file=${platform_dir}/reports/slurmdbd-${run_id}.md
backup_dir=${platform_dir}/backups
mariadb_source=${platform_dir}/config/mariadb-slurm.cnf
mariadb_target=/etc/mysql/mariadb.conf.d/60-slurm.cnf
slurmdbd_target=/etc/slurm/slurmdbd.conf
validation_log=${record_dir}/slurmdbd-foreground-validation.txt

if [[ ! -f ${mariadb_source} ]]; then
    printf '%s\n' 'Tracked MariaDB configuration is missing.' >&2
    exit 1
fi
if [[ -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite an existing SlurmDBD record.' >&2
    exit 1
fi

sudo -n true
install -d -m 2770 "${record_dir}" "${backup_dir}"

if ! systemctl is-active --quiet munge; then
    printf '%s\n' 'MUNGE must be active before configuring SlurmDBD.' >&2
    exit 1
fi
if ! id slurm >/dev/null 2>&1 || [[ $(id -gn slurm) != slurm ]]; then
    printf '%s\n' 'The slurm system account is missing or malformed.' >&2
    exit 1
fi
if [[ $(sudo stat -c '%U:%G:%a' /etc/munge/munge.key) != munge:munge:400 ]]; then
    printf '%s\n' 'MUNGE key metadata does not satisfy the required policy.' >&2
    exit 1
fi
if ! dpkg-query -W -f='${db:Status-Abbrev}\n' mariadb-server 2>/dev/null | grep -q '^ii'; then
    printf '%s\n' 'MariaDB server package is not configured.' >&2
    exit 1
fi
if ! dpkg-query -W -f='${Version}\n' slurm-smd-slurmdbd | grep -Fxq '25.11.7-1h100.1'; then
    printf '%s\n' 'The expected SlurmDBD package version is not installed.' >&2
    exit 1
fi
if sudo test -e "${mariadb_target}" || sudo test -e "${slurmdbd_target}"; then
    printf '%s\n' 'A target MariaDB or SlurmDBD configuration already exists; refusing to overwrite it.' >&2
    exit 1
fi
if sudo test -e /var/log/slurm/slurmdbd.log; then
    printf '%s\n' 'An unknown SlurmDBD log already exists; refusing to truncate or replace it.' >&2
    exit 1
fi
if systemctl is-active --quiet slurmdbd; then
    printf '%s\n' 'SlurmDBD is unexpectedly active before configuration.' >&2
    exit 1
fi

actual_hostname=$(hostname -s)
if [[ ! ${actual_hostname} =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
    printf 'Unsafe short hostname: %s\n' "${actual_hostname}" >&2
    exit 1
fi
if ! getent hosts "${actual_hostname}" >"${record_dir}/hostname-resolution.txt"; then
    printf '%s\n' 'The local short hostname does not resolve.' >&2
    exit 1
fi

my_print_defaults --defaults-file="${mariadb_source}" mysqld \
    >"${record_dir}/mariadb-candidate-validation.txt"
for expected_line in \
    '--innodb_buffer_pool_size=4G' \
    '--innodb_log_file_size=1G' \
    '--innodb_lock_wait_timeout=900' \
    '--max_allowed_packet=16M'; do
    if ! grep -Fxq -- "${expected_line}" "${record_dir}/mariadb-candidate-validation.txt"; then
        printf 'MariaDB candidate validation did not find: %s\n' "${expected_line}" >&2
        exit 1
    fi
done

sudo tar --acls --xattrs -C /etc -cpf "${backup_dir}/etc-slurm-before-slurmdbd-${run_id}.tar" slurm
: >"${record_dir}/mariadb-target-was-absent.txt"
: >"${record_dir}/slurmdbd-target-was-absent.txt"
sudo install -o root -g root -m 0644 "${mariadb_source}" "${mariadb_target}"

sudo systemctl enable --now mariadb
if ! systemctl is-active --quiet mariadb; then
    printf '%s\n' 'MariaDB did not become active.' >&2
    exit 1
fi
if ! sudo mariadb-admin --protocol=socket ping >/dev/null; then
    printf '%s\n' 'MariaDB socket health check failed.' >&2
    exit 1
fi

sudo mariadb --batch --skip-column-names -e \
    "SHOW VARIABLES WHERE Variable_name IN ('innodb_buffer_pool_size','innodb_log_file_size','innodb_lock_wait_timeout','max_allowed_packet');" \
    >"${record_dir}/mariadb-effective-settings.txt"
for expected_line in \
    $'innodb_buffer_pool_size\t4294967296' \
    $'innodb_lock_wait_timeout\t900' \
    $'innodb_log_file_size\t1073741824' \
    $'max_allowed_packet\t16777216'; do
    if ! grep -Fxq -- "${expected_line}" "${record_dir}/mariadb-effective-settings.txt"; then
        printf 'MariaDB effective setting validation failed: %s\n' "${expected_line%%$'\t'*}" >&2
        exit 1
    fi
done
if ! sudo mariadb --batch --skip-column-names -e \
    "SHOW ENGINES;" | awk '$1 == "InnoDB" && ($2 == "DEFAULT" || $2 == "YES") { found = 1 } END { exit !found }'; then
    printf '%s\n' 'MariaDB does not report usable InnoDB support.' >&2
    exit 1
fi

database_count=$(sudo mariadb --batch --skip-column-names -e \
    "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='slurm_acct_db';")
user_count=$(sudo mariadb --batch --skip-column-names -e \
    "SELECT COUNT(*) FROM mysql.user WHERE User='slurm';")
if [[ ${database_count} != 0 || ${user_count} != 0 ]]; then
    printf '%s\n' 'A Slurm accounting database or database user already exists; stopping.' >&2
    exit 1
fi

sudo /bin/bash -s -- "${actual_hostname}" <<'ROOT_HELPER'
set -euo pipefail
umask 077
hostname_value=${1}
sql_file=$(mktemp /run/slurm-db-init.XXXXXX)
sql_error=$(mktemp /run/slurm-db-error.XXXXXX)
conf_file=$(mktemp /run/slurmdbd-conf.XXXXXX)
password=

cleanup_secret_files() {
    unset password
    shred -u -- "${sql_file}" "${sql_error}" "${conf_file}" 2>/dev/null || true
}
trap cleanup_secret_files EXIT

password=$(openssl rand -base64 48 | tr -d '\n')
if [[ ${#password} -lt 64 || ! ${password} =~ ^[A-Za-z0-9+/=]+$ || ${password} == *'#'* ]]; then
    printf '%s\n' 'Secure database password generation failed validation.' >&2
    exit 1
fi

{
    printf '%s\n' "CREATE DATABASE slurm_acct_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
    printf "CREATE USER 'slurm'@'localhost' IDENTIFIED BY '%s';\n" "${password}"
    printf "%s\n" "GRANT ALL ON slurm_acct_db.* TO 'slurm'@'localhost';"
    printf '%s\n' 'FLUSH PRIVILEGES;'
} >"${sql_file}"

if ! mariadb --protocol=socket --user=root <"${sql_file}" > /dev/null 2>"${sql_error}"; then
    printf '%s\n' 'MariaDB rejected the protected Slurm database initialization transaction.' >&2
    exit 1
fi

cat >"${conf_file}" <<EOF
AuthType=auth/munge
DbdHost=${hostname_value}
SlurmUser=slurm
DebugLevel=info
LogFile=/var/log/slurm/slurmdbd.log
PidFile=/run/slurmdbd/slurmdbd.pid
StorageType=accounting_storage/mysql
StorageHost=localhost
StoragePort=3306
StorageLoc=slurm_acct_db
StorageUser=slurm
StoragePass=${password}
EOF
install -o slurm -g slurm -m 0600 "${conf_file}" /etc/slurm/slurmdbd.conf
ROOT_HELPER

if [[ $(sudo stat -c '%U:%G:%a' "${slurmdbd_target}") != slurm:slurm:600 ]]; then
    printf '%s\n' 'SlurmDBD configuration owner, group, or mode is incorrect.' >&2
    exit 1
fi

sudo install -d -o slurm -g slurm -m 0750 /var/log/slurm
sudo install -o slurm -g slurm -m 0640 /dev/null /var/log/slurm/slurmdbd.log
sudo install -d -o slurm -g slurm -m 0755 /run/slurmdbd

set +e
sudo -u slurm timeout --signal=TERM --kill-after=5s 15s \
    /usr/sbin/slurmdbd -D -vv >"${validation_log}" 2>&1
validation_status=$?
set -e
if [[ ${validation_status} -ne 0 && ${validation_status} -ne 124 ]]; then
    printf 'SlurmDBD foreground validation exited unexpectedly: %s\n' "${validation_status}" >&2
    exit 1
fi
if grep -Eqi 'fatal:|error:' "${validation_log}"; then
    printf '%s\n' 'SlurmDBD foreground validation logged a fatal or error condition.' >&2
    exit 1
fi
if ! grep -Eqi 'slurmdbd version .* started|slurmdbd.*started' "${validation_log}"; then
    printf '%s\n' 'SlurmDBD foreground validation did not reach a started state.' >&2
    exit 1
fi
if pgrep -u slurm -x slurmdbd >/dev/null; then
    printf '%s\n' 'Foreground validation left a SlurmDBD process running.' >&2
    exit 1
fi

table_count=$(sudo mariadb --batch --skip-column-names -e \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='slurm_acct_db';")
if (( table_count == 0 )); then
    printf '%s\n' 'SlurmDBD validation did not initialize accounting tables.' >&2
    exit 1
fi

sudo systemctl enable --now slurmdbd
if ! systemctl is-active --quiet slurmdbd; then
    printf '%s\n' 'SlurmDBD did not become active under systemd.' >&2
    exit 1
fi

sudo -u slurm slurmdbd -V >"${record_dir}/slurmdbd-version.txt"
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
if ss -lnt | awk '$4 ~ /:3306$/ && $4 !~ /^127\.0\.0\.1:3306$/ { bad = 1 } END { exit bad }'; then
    :
else
    printf '%s\n' 'MariaDB is listening beyond 127.0.0.1.' >&2
    exit 1
fi

cat >"${report_file}" <<EOF
# MariaDB and SlurmDBD configuration

- Run ID: ${run_id}
- Host: ${actual_hostname}
- MariaDB version: $(mariadb --version | sed 's/,.*//')
- MariaDB data directory: /var/lib/mariadb (Ubuntu 26.04 package default)
- MariaDB bind address: 127.0.0.1
- Database: slurm_acct_db
- Database user: slurm@localhost
- Database password displayed, logged, or passed in argv/environment: no
- Plaintext password location: /etc/slurm/slurmdbd.conf only
- SlurmDBD configuration mode: slurm:slurm 0600
- SlurmDBD version: $(sudo -u slurm slurmdbd -V 2>&1 | tail -n 1)
- Accounting tables after initialization: ${table_count}
- MariaDB service: enabled and active
- SlurmDBD service: enabled and active
- SchedMD database guidance: https://slurm.schedmd.com/accounting.html#mysql-configuration
- Applied database sizing: 4 GiB buffer pool, 1 GiB log file, 900 second lock wait, 16 MiB packet

Status: SLURMDBD PASSED
EOF

printf '%s\n' 'SLURMDBD PASSED'
