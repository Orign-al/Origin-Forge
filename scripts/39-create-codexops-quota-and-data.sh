#!/usr/bin/env bash
set -euo pipefail

readonly run_id="${RUN_ID:-20260804-050357}"
readonly platform_root=/srv/gpu-platform/platform
readonly mountpoint=/srv/gpu-platform
readonly user_name=codexops
readonly project_name=h100_codexops
readonly project_id=10001
readonly quota_hard=300g
readonly user_root="${mountpoint}/users/${user_name}"
readonly projects_file=/etc/projects
readonly projid_file=/etc/projid

stamp="$(date +%Y%m%d-%H%M%S-%N)"
backup_dir="${platform_root}/backups/codexops-quota-${stamp}"
report_file="${platform_root}/reports/codexops-quota-${run_id}.md"
temp_dir=''

cleanup() {
  case "${temp_dir}" in
    /tmp/h100-codexops-quota.*)
      rm -rf -- "${temp_dir}"
      ;;
  esac
}
trap cleanup EXIT

fail() {
  printf 'PROJECT QUOTA BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'
[[ "$(findmnt -no FSTYPE "${mountpoint}")" == xfs ]] \
  || fail "${mountpoint} is not XFS"
findmnt -no OPTIONS "${mountpoint}" \
  | tr ',' '\n' \
  | grep -qx prjquota \
  || fail 'prjquota is not active'

quota_state="$(sudo xfs_quota -x -c state "${mountpoint}")"
grep -A4 '^Project quota state' <<<"${quota_state}" | grep -q 'Accounting: ON' \
  || fail 'project quota accounting is not active'
grep -A4 '^Project quota state' <<<"${quota_state}" | grep -q 'Enforcement: ON' \
  || fail 'project quota enforcement is not active'

getent passwd "${user_name}" >/dev/null || fail 'codexops account is missing'
[[ -f /home/codexops/.ssh/authorized_keys ]] \
  || fail 'codexops authorized_keys is missing'
[[ ! -e "${report_file}" ]] \
  || fail "refusing to overwrite report: ${report_file}"
[[ ! -e "${user_root}" ]] \
  || fail "refusing to overwrite existing user data path: ${user_root}"

if [[ -e "${projid_file}" ]]; then
  sudo grep -q "^${project_name}:" "${projid_file}" \
    && fail "project name already exists: ${project_name}"
  sudo grep -Eq ":${project_id}[[:space:]]*$" "${projid_file}" \
    && fail "project ID already exists in ${projid_file}: ${project_id}"
fi
if [[ -e "${projects_file}" ]]; then
  sudo grep -Eq "^${project_id}:" "${projects_file}" \
    && fail "project ID already exists in ${projects_file}: ${project_id}"
  sudo grep -Fq ":${user_root}" "${projects_file}" \
    && fail "user path already exists in ${projects_file}: ${user_root}"
fi

install -d -m 2770 "${backup_dir}"
if [[ -e "${projects_file}" ]]; then
  sudo cp -a "${projects_file}" "${backup_dir}/projects.before"
else
  printf 'ABSENT before deployment\n' >"${backup_dir}/projects.state"
fi
if [[ -e "${projid_file}" ]]; then
  sudo cp -a "${projid_file}" "${backup_dir}/projid.before"
else
  printf 'ABSENT before deployment\n' >"${backup_dir}/projid.state"
fi

temp_dir="$(mktemp -d /tmp/h100-codexops-quota.XXXXXX)"
new_projects="${temp_dir}/projects"
new_projid="${temp_dir}/projid"

if [[ -e "${projects_file}" ]]; then
  sudo sed -e '$a\' "${projects_file}" >"${new_projects}"
fi
printf '%s:%s\n' "${project_id}" "${user_root}" >>"${new_projects}"

if [[ -e "${projid_file}" ]]; then
  sudo sed -e '$a\' "${projid_file}" >"${new_projid}"
fi
printf '%s:%s\n' "${project_name}" "${project_id}" >>"${new_projid}"

awk -F: '
  NF != 2 || $1 !~ /^[0-9]+$/ || $2 !~ /^\// { bad=1 }
  seen_id[$1]++ > 0 { bad=1 }
  seen_path[$2]++ > 0 { bad=1 }
  END { exit bad }
' "${new_projects}" || fail 'generated /etc/projects is invalid'
awk -F: '
  NF != 2 || $1 !~ /^[a-zA-Z0-9_.-]+$/ || $2 !~ /^[0-9]+$/ { bad=1 }
  seen_name[$1]++ > 0 { bad=1 }
  seen_id[$2]++ > 0 { bad=1 }
  END { exit bad }
' "${new_projid}" || fail 'generated /etc/projid is invalid'

sudo install -o root -g root -m 0644 "${new_projects}" "${projects_file}"
sudo install -o root -g root -m 0644 "${new_projid}" "${projid_file}"

sudo install -d -o codexops -g codexops -m 0710 "${user_root}"
sudo install -d -o codexops -g codexops -m 0700 \
  "${user_root}/home"
sudo install -d -o codexops -g codexops -m 0750 \
  "${user_root}/workspace" \
  "${user_root}/shared"
sudo install -d -o codexops -g codexops -m 0700 \
  "${user_root}/home/.ssh"
sudo install -o codexops -g codexops -m 0600 \
  /home/codexops/.ssh/authorized_keys \
  "${user_root}/home/.ssh/authorized_keys"

sudo install -d -o root -g root -m 0700 \
  /srv/gpu-platform/container-data/codexops \
  /srv/gpu-platform/container-data/codexops/ssh-host-keys

sudo xfs_quota -x \
  -c "project -s ${project_name}" \
  "${mountpoint}"
sudo xfs_quota -x \
  -c "limit -p bhard=${quota_hard} ${project_name}" \
  "${mountpoint}"

sudo xfs_quota -x \
  -c "project -c ${project_name}" \
  "${mountpoint}"
quota_report="$(sudo xfs_quota -x -c 'report -p -h' "${mountpoint}")"
grep -E "^${project_name}[[:space:]]" <<<"${quota_report}" \
  | grep -q '300G' \
  || fail '300GB hard project quota was not observed'

project_stat="$(sudo xfs_io -c 'stat -v' "${user_root}")"
grep -Eq "projid[[:space:]]*=[[:space:]]*${project_id}" <<<"${project_stat}" \
  || fail 'user directory project ID does not match'

ssh_fingerprint="$(ssh-keygen -lf /home/codexops/.ssh/authorized_keys)"
{
  printf '# codexops XFS project quota and data directories\n\n'
  printf -- '- Run ID: `%s`\n' "${run_id}"
  printf -- '- Result: `PROJECT QUOTA PASSED`\n'
  printf -- '- Project name: `%s`\n' "${project_name}"
  printf -- '- Project ID: `%s`\n' "${project_id}"
  printf -- '- Project root: `%s`\n' "${user_root}"
  printf -- '- Hard limit: `300GB`\n'
  printf -- '- Home: `%s/home`\n' "${user_root}"
  printf -- '- Workspace: `%s/workspace`\n' "${user_root}"
  printf -- '- Shared: `%s/shared`\n' "${user_root}"
  printf -- '- Persistent SSH host-key directory: `/srv/gpu-platform/container-data/codexops/ssh-host-keys`\n'
  printf -- '- Authorized public-key fingerprint: `%s`\n' "${ssh_fingerprint}"
  printf -- '- Backups: `%s`\n' "${backup_dir}"
  printf -- '- MIG was not modified\n'
  printf -- '- Firewall was not modified\n'
} >"${report_file}"

printf 'PROJECT QUOTA PASSED\n'
printf 'Report: %s\n' "${report_file}"
