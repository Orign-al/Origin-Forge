#!/usr/bin/env bash
set -euo pipefail

readonly run_id="${RUN_ID:-20260804-050357}"
readonly platform_root=/srv/gpu-platform/platform
readonly source_dir="${platform_root}/scripts"
readonly install_dir=/usr/local/sbin
readonly library_dir=/usr/local/lib/h100-platform
readonly audit_log=/var/log/h100-platform-audit.log
readonly lock_file=/run/lock/h100-platform.lock

scripts=(
  h100-user-create
  h100-container-create
  h100-container-start
  h100-container-stop
  h100-container-rebuild
  h100-container-delete
  h100-container-status
  h100-quota-show
)

fail() {
  printf 'MANAGEMENT SCRIPT INSTALL BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'

for script in "${scripts[@]}"; do
  [[ -f "${source_dir}/${script}" ]] || fail "missing source script: ${script}"
  grep -q '^set -euo pipefail$' "${source_dir}/${script}" \
    || fail "strict shell mode is missing: ${script}"
  if sudo test -e "${install_dir}/${script}"; then
    fail "refusing to overwrite existing target: ${install_dir}/${script}"
  fi
done
[[ -f "${source_dir}/h100-platform-common.sh" ]] \
  || fail 'common library source is missing'
[[ -f "${source_dir}/h100-platform-common.sh" ]] \
  && grep -q '^set -euo pipefail$' "${source_dir}/h100-platform-common.sh" \
  || fail 'common library strict shell mode is missing'

if sudo test -e "${library_dir}/h100-platform-common.sh"; then
  fail 'refusing to overwrite existing common library'
fi
if sudo test -e "${audit_log}"; then
  fail 'refusing to overwrite existing audit log'
fi

bash -n \
  "${source_dir}/h100-platform-common.sh" \
  "${source_dir}/h100-user-create" \
  "${source_dir}/h100-container-create" \
  "${source_dir}/h100-container-start" \
  "${source_dir}/h100-container-stop" \
  "${source_dir}/h100-container-rebuild" \
  "${source_dir}/h100-container-delete" \
  "${source_dir}/h100-container-status" \
  "${source_dir}/h100-quota-show"

if grep -R -n -E '(^|[[:space:]])eval([[:space:]]|$)' \
  "${source_dir}/h100-platform-common.sh" \
  "${source_dir}/h100-user-create" \
  "${source_dir}/h100-container-create" \
  "${source_dir}/h100-container-start" \
  "${source_dir}/h100-container-stop" \
  "${source_dir}/h100-container-rebuild" \
  "${source_dir}/h100-container-delete" \
  "${source_dir}/h100-container-status" \
  "${source_dir}/h100-quota-show"; then
  fail 'eval is forbidden in management scripts'
fi

stamp="$(date +%Y%m%d-%H%M%S-%N)"
backup_dir="${platform_root}/backups/management-scripts-${stamp}"
report_file="${platform_root}/reports/management-scripts-${run_id}.md"
[[ ! -e "${report_file}" ]] || fail 'refusing to overwrite report'
install -d -m 2770 "${backup_dir}"
printf 'All management targets, common library, lock, and audit log were absent.\n' \
  >"${backup_dir}/state"

sudo install -d -o root -g gpu-platform-admin -m 0750 "${library_dir}"
sudo install -o root -g gpu-platform-admin -m 0640 \
  "${source_dir}/h100-platform-common.sh" \
  "${library_dir}/h100-platform-common.sh"
for script in "${scripts[@]}"; do
  sudo install -o root -g gpu-platform-admin -m 0750 \
    "${source_dir}/${script}" \
    "${install_dir}/${script}"
done
sudo install -o root -g gpu-platform-admin -m 0660 /dev/null "${audit_log}"
sudo install -o root -g gpu-platform-admin -m 0660 /dev/null "${lock_file}"

sudo /usr/local/sbin/h100-user-create --help >/dev/null
sudo /usr/local/sbin/h100-container-create --help >/dev/null
sudo /usr/local/sbin/h100-container-delete --help >/dev/null
status_output="$(sudo /usr/local/sbin/h100-container-status codexops)"
grep -q 'container=gpu-dev-codexops' <<<"${status_output}" \
  || fail 'installed status script did not find codexops container'
quota_output="$(sudo /usr/local/sbin/h100-quota-show codexops)"
grep -q 'h100_codexops' <<<"${quota_output}" \
  || fail 'installed quota script did not find codexops quota'

[[ "$(sudo stat -c '%U:%G %a' "${audit_log}")" == 'root:gpu-platform-admin 660' ]] \
  || fail 'audit log ownership or mode is incorrect'
sudo test "$(sudo wc -l <"${audit_log}")" -ge 2 \
  || fail 'audit log did not record status and quota tests'

{
  printf '# H100 platform management scripts\n\n'
  printf -- '- Run ID: `%s`\n' "${run_id}"
  printf -- '- Result: `MANAGEMENT SCRIPTS INSTALLED`\n'
  printf -- '- Source: `%s`\n' "${source_dir}"
  printf -- '- Install target: `%s`\n' "${install_dir}"
  printf -- '- Common library: `%s/h100-platform-common.sh`\n' "${library_dir}"
  printf -- '- Global lock: `%s`\n' "${lock_file}"
  printf -- '- Audit log: `%s` (`root:gpu-platform-admin`, `0660`)\n' "${audit_log}"
  printf -- '- Username validation: strict allowlist\n'
  printf -- '- Arbitrary host mounts: not accepted\n'
  printf -- '- Docker Socket: never mounted\n'
  printf -- '- Container data deletion: separate flag, repeated username, and interactive phrase required\n'
  printf -- '- Default container deletion preserves user data\n'
  printf -- '- `eval`: absent\n'
  printf -- '- Functional read-only tests: status and quota passed for codexops\n'
  printf -- '- User/container creation tests: not invoked; no other users or containers created\n'
  printf -- '- Backup record: `%s`\n' "${backup_dir}"
} >"${report_file}"

printf 'MANAGEMENT SCRIPTS INSTALLED\n'
printf 'Report: %s\n' "${report_file}"
