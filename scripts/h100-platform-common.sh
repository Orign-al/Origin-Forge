#!/usr/bin/env bash
# Constants in this sourced library are consumed by its callers.
# shellcheck disable=SC2034
set -euo pipefail

readonly H100_PLATFORM_ROOT=/srv/gpu-platform/platform
readonly H100_DATA_ROOT=/srv/gpu-platform
readonly H100_WORKSPACE_ROOT=/storage/users
readonly H100_HOME_ALIAS_ROOT=/storage/homes
readonly H100_MANAGEMENT_IP=10.82.36.1
readonly H100_PUBLIC_ACCESS_IP=20.10.10.3
readonly H100_LEGACY_PUBLIC_ACCESS_IP=10.10.10.220
readonly -a H100_CONTAINER_PUBLISH_IPS=(
  "${H100_PUBLIC_ACCESS_IP}"
  "${H100_LEGACY_PUBLIC_ACCESS_IP}"
)
readonly H100_AUDIT_LOG=/var/log/h100-platform-audit.log
readonly H100_LOCK_FILE=/run/lock/h100-platform.lock
readonly H100_GPU_ISOLATION_REGISTRY=/etc/h100-platform/gpu-isolated-users
readonly H100_GPU_ISOLATION_TOOL=/usr/local/sbin/h100-user-gpu-isolation
readonly H100_WORKSPACE_ALIAS_TOOL=/usr/local/sbin/h100-workspace-alias
readonly H100_HOME_ALIAS_TOOL=/usr/local/sbin/h100-home-alias
readonly H100_CONTAINER_DATA_ROOT=/srv/gpu-platform/container-data

h100_fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

h100_require_root() {
  [[ "$(id -u)" == 0 ]] || h100_fail 'run this command with sudo'
}

h100_validate_username() {
  local candidate_username=${1:-}
  [[ "${candidate_username}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] \
    || h100_fail 'invalid username'
  [[ "${candidate_username}" != *..* ]] || h100_fail 'invalid username'
}

h100_container_sudo_policy_file() {
  local managed_username=$1
  h100_validate_username "${managed_username}"
  printf '%s/%s/sudoers/90-h100-dev-user\n' \
    "${H100_CONTAINER_DATA_ROOT}" "${managed_username}"
}

h100_require_container_data_root() {
  local managed_username=$1 managed_root mode
  h100_validate_username "${managed_username}"
  managed_root="${H100_CONTAINER_DATA_ROOT}/${managed_username}"
  for directory in "${H100_CONTAINER_DATA_ROOT}" "${managed_root}"; do
    [[ -d "${directory}" && ! -L "${directory}" ]] \
      || h100_fail "container data directory is unsafe: ${managed_username}"
    [[ "$(stat -c '%u:%g' "${directory}")" == 0:0 ]] \
      || h100_fail "container data directory ownership is unsafe: ${managed_username}"
    mode="$(stat -c '%a' "${directory}")"
    [[ "${mode}" =~ ^[0-7]+$ ]] \
      && (((8#${mode} & 0022) == 0)) \
      || h100_fail "container data directory is writable outside root: ${managed_username}"
  done
}

h100_require_container_sudo_policy() {
  local managed_username=$1 policy_file expected
  h100_require_container_data_root "${managed_username}"
  policy_file="$(h100_container_sudo_policy_file "${managed_username}")"
  expected="${managed_username} ALL=(ALL:ALL) NOPASSWD: ALL"
  [[ -f "${policy_file}" && ! -L "${policy_file}" ]] \
    || h100_fail "container sudo policy is missing: ${managed_username}"
  [[ "$(stat -c '%u:%g:%a' "${policy_file}")" == 0:0:440 ]] \
    || h100_fail "container sudo policy metadata is invalid: ${managed_username}"
  [[ "$(wc -l <"${policy_file}")" == 1 && "$(<"${policy_file}")" == "${expected}" ]] \
    || h100_fail "container sudo policy content is invalid: ${managed_username}"
  /usr/sbin/visudo -cf "${policy_file}" >/dev/null \
    || h100_fail "container sudo policy syntax is invalid: ${managed_username}"
}

h100_prepare_container_sudo_policy() {
  local managed_username=$1 policy_file policy_dir temporary
  h100_validate_username "${managed_username}"
  h100_require_container_data_root "${managed_username}"
  policy_file="$(h100_container_sudo_policy_file "${managed_username}")"
  policy_dir="$(dirname "${policy_file}")"
  [[ ! -e "${policy_dir}" || ( -d "${policy_dir}" && ! -L "${policy_dir}" ) ]] \
    || h100_fail "container sudo policy directory is unsafe: ${managed_username}"
  install -d -o root -g root -m 0700 "${policy_dir}"
  temporary="$(mktemp "${policy_dir}/.90-h100-dev-user.XXXXXX")"
  printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "${managed_username}" >"${temporary}"
  chown root:root "${temporary}"
  chmod 0440 "${temporary}"
  /usr/sbin/visudo -cf "${temporary}" >/dev/null \
    || { rm -f -- "${temporary}"; h100_fail 'generated container sudo policy is invalid'; }
  mv -f -- "${temporary}" "${policy_file}"
  h100_require_container_sudo_policy "${managed_username}"
}

h100_validate_port() {
  local candidate_port=${1:-}
  [[ "${candidate_port}" =~ ^[0-9]+$ ]] || h100_fail 'port must be numeric'
  ((candidate_port >= 1024 && candidate_port <= 65535)) \
    || h100_fail 'port must be between 1024 and 65535'
}

h100_workspace_mode_is_private() {
  local workspace_path=$1 workspace_mode
  workspace_mode="$(stat -c '%a' "${workspace_path}")" || return 1
  [[ "${workspace_mode}" =~ ^[0-7]+$ ]] || return 1
  (((8#${workspace_mode} & 0007) == 0))
}

h100_validate_image_ref() {
  local candidate_image=${1:-}
  local image_leaf
  [[ -n "${candidate_image}" && "${candidate_image}" =~ ^[A-Za-z0-9._/@:+-]+$ ]] \
    || h100_fail 'invalid image reference'
  if [[ "${candidate_image}" == *@sha256:* ]]; then
    return 0
  fi
  image_leaf=${candidate_image##*/}
  [[ "${image_leaf}" == *:* ]] \
    || h100_fail 'image reference must contain a fixed tag or digest'
  [[ "${image_leaf##*:}" != latest ]] || h100_fail 'latest image tag is forbidden'
}

h100_acquire_lock() {
  local mode=${1:-exclusive}
  if [[ "${H100_PLATFORM_LOCK_HELD:-0}" == 1 ]]; then
    flock -n 9 >/dev/null 2>&1 \
      || h100_fail 'inherited platform lock marker has no valid lock descriptor'
    return 0
  fi
  exec 9>"${H100_LOCK_FILE}"
  if [[ "${mode}" == shared ]]; then
    flock -s 9
  else
    flock -x 9
  fi
  export H100_PLATFORM_LOCK_HELD=1
}

h100_audit() {
  local h100_audit_action_value=$1
  local h100_audit_target_value=$2
  local h100_audit_outcome_value=$3
  local h100_audit_rc_value=$4
  local h100_audit_actor_value=${SUDO_USER:-root}
  printf '%s actor=%s action=%s target=%s outcome=%s rc=%s\n' \
    "$(date --iso-8601=seconds)" \
    "${h100_audit_actor_value}" \
    "${h100_audit_action_value}" \
    "${h100_audit_target_value}" \
    "${h100_audit_outcome_value}" \
    "${h100_audit_rc_value}" \
    >>"${H100_AUDIT_LOG}"
}

h100_install_audit_trap() {
  H100_AUDIT_ACTION=$1
  H100_AUDIT_TARGET=$2
  H100_AUDIT_OUTCOME=FAILED
  trap 'rc=$?; trap - EXIT; h100_audit "${H100_AUDIT_ACTION}" "${H100_AUDIT_TARGET}" "${H100_AUDIT_OUTCOME}" "${rc}"; exit "${rc}"' EXIT
}

h100_mark_success() {
  H100_AUDIT_OUTCOME=SUCCESS
}

h100_container_name() {
  printf 'gpu-dev-%s\n' "$1"
}

h100_config_dir() {
  printf '%s/config/dev-containers/%s\n' "${H100_PLATFORM_ROOT}" "$1"
}

h100_compose_file() {
  printf '%s/compose.yml\n' "$(h100_config_dir "$1")"
}

h100_require_managed_user() {
  local managed_username=$1
  local managed_uid managed_gid managed_backing
  getent passwd "${managed_username}" >/dev/null \
    || h100_fail "host user does not exist: ${managed_username}"
  managed_uid="$(id -u "${managed_username}")"
  managed_gid="$(id -g "${managed_username}")"
  managed_backing="${H100_DATA_ROOT}/users/${managed_username}/workspace"
  [[ -d "${H100_DATA_ROOT}/users/${managed_username}/home" ]] \
    || h100_fail "managed home is missing: ${managed_username}"
  [[ -d "${managed_backing}" && ! -L "${managed_backing}" ]] \
    || h100_fail "managed workspace backing is missing: ${managed_username}"
  [[ "$(stat -c '%u:%g' "${managed_backing}")" == "${managed_uid}:${managed_gid}" ]] \
    && h100_workspace_mode_is_private "${managed_backing}" \
    || h100_fail "managed workspace ownership or mode is invalid: ${managed_username}"
  h100_require_workspace_alias "${managed_username}"
}

h100_require_workspace_alias() {
  local managed_username=$1 managed_uid managed_gid
  managed_uid="$(id -u "${managed_username}")"
  managed_gid="$(id -g "${managed_username}")"
  [[ -x "${H100_WORKSPACE_ALIAS_TOOL}" && ! -L "${H100_WORKSPACE_ALIAS_TOOL}" ]] \
    || h100_fail "workspace alias verifier is unavailable: ${managed_username}"
  "${H100_WORKSPACE_ALIAS_TOOL}" verify \
    "${managed_username}" "${managed_uid}" "${managed_gid}" >/dev/null \
    || h100_fail "canonical workspace alias verification failed: ${managed_username}"
}

h100_require_home_alias() {
  local managed_username=$1 managed_uid managed_gid
  managed_uid="$(id -u "${managed_username}")"
  managed_gid="$(id -g "${managed_username}")"
  [[ -x "${H100_HOME_ALIAS_TOOL}" && ! -L "${H100_HOME_ALIAS_TOOL}" ]] \
    || h100_fail "home alias verifier is unavailable: ${managed_username}"
  "${H100_HOME_ALIAS_TOOL}" verify \
    "${managed_username}" "${managed_uid}" "${managed_gid}" >/dev/null \
    || h100_fail "canonical home alias verification failed: ${managed_username}"
}

h100_require_pilot_gpu_isolation_if_managed() {
  local managed_username=$1
  local managed_uid
  local pilot_state_file="/etc/h100-platform/users/${managed_username}.state"
  managed_uid="$(id -u "${managed_username}")"
  if [[ ! -f "${H100_GPU_ISOLATION_REGISTRY}" ]]; then
    [[ ! -e "${pilot_state_file}" ]] \
      || h100_fail 'Pilot state exists but GPU isolation registry is missing'
    return 0
  fi
  if awk -v name="${managed_username}" -v uid="${managed_uid}" '
      $0 !~ /^[[:space:]]*(#|$)/ && $1 == name && $2 == uid { found=1 }
      END { exit found ? 0 : 1 }
    ' "${H100_GPU_ISOLATION_REGISTRY}"; then
    [[ -x "${H100_GPU_ISOLATION_TOOL}" ]] \
      || h100_fail 'Pilot GPU isolation verifier is unavailable'
    "${H100_GPU_ISOLATION_TOOL}" verify "${managed_username}"
    return 0
  fi
  if [[ -e "${pilot_state_file}" ]] \
    || awk -v name="${managed_username}" -v uid="${managed_uid}" '
      $0 !~ /^[[:space:]]*(#|$)/ && ($1 == name || $2 == uid) { found=1 }
      END { exit found ? 0 : 1 }
    ' "${H100_GPU_ISOLATION_REGISTRY}"; then
    h100_fail 'Pilot user GPU isolation registry mapping is absent or inconsistent'
  fi
}

h100_require_managed_compose() {
  local managed_username=$1
  local managed_compose
  local compose_metadata
  managed_compose="$(h100_compose_file "${managed_username}")"
  [[ -f "${managed_compose}" && ! -L "${managed_compose}" ]] \
    || h100_fail "managed Compose file is missing: ${managed_username}"
  compose_metadata="$(stat -c '%U:%G:%a' "${managed_compose}")"
  [[ "${compose_metadata}" == 'root:gpu-platform-admin:640' ]] \
    || h100_fail "managed Compose ownership or mode is invalid: ${managed_username}"
}

h100_wait_healthy() {
  local target_container=$1
  local health_attempts=${2:-120}
  local health_state
  local _attempt
  for _attempt in $(seq 1 "${health_attempts}"); do
    health_state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${target_container}")"
    if [[ "${health_state}" == healthy ]]; then
      return 0
    fi
    if [[ "${health_state}" == exited || "${health_state}" == dead ]]; then
      return 1
    fi
    sleep 1
  done
  return 1
}

h100_require_slurm_drained() {
  local state
  mapfile -t h100_slurm_states < <(sinfo -h -N -o '%T')
  ((${#h100_slurm_states[@]} > 0)) || h100_fail 'Slurm returned no node state'
  for state in "${h100_slurm_states[@]}"; do
    [[ "${state}" == drain* ]] || h100_fail "Slurm node is not drained: ${state}"
  done
}
