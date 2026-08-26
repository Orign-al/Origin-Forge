#!/usr/bin/env bash
set -euo pipefail

readonly portal_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly platform_root="$(cd "${portal_root}/.." && pwd)"
readonly rehearsal_uid=29991
readonly rehearsal_gid=29991
readonly rehearsal_user=workspace-rehearsal

fail() {
  printf 'REHEARSAL FAILED: %s\n' "$*" >&2
  exit 1
}

cleanup_outer() {
  local cleanup_status=$?
  if [[ -n "${rehearsal_root:-}" ]]; then
    case "${rehearsal_root}" in
      /tmp/h100-workspace-systemd-rehearsal.*)
        rm -rf -- "${rehearsal_root}"
        ;;
      *)
        printf 'refusing to clean unexpected rehearsal root: %s\n' "${rehearsal_root}" >&2
        cleanup_status=1
        ;;
    esac
  fi
  exit "${cleanup_status}"
}

if [[ "${1:-}" != --inside-mount-namespace ]]; then
  [[ "${EUID}" == 0 ]] || fail 'run as root in a disposable test environment'
  rehearsal_root="$(mktemp -d /tmp/h100-workspace-systemd-rehearsal.XXXXXX)"
  trap cleanup_outer EXIT
  unshare --mount --propagation private \
    "${BASH_SOURCE[0]}" --inside-mount-namespace "${rehearsal_root}" "${platform_root}"
  printf 'SYSTEMD-EQUIVALENT WORKSPACE REHEARSAL PASS\n'
  exit 0
fi

[[ "${EUID}" == 0 ]] || fail 'mount namespace setup requires root'
readonly namespace_root=${2:?missing namespace root}
readonly candidate_root=${3:?missing candidate root}
[[ "${namespace_root}" == /tmp/h100-workspace-systemd-rehearsal.* ]] \
  || fail 'namespace root is outside the disposable contract'
[[ -f "${candidate_root}/scripts/h100-workspace-alias" ]] \
  || fail 'candidate workspace alias is unavailable'

install -d -m 0755 \
  "${namespace_root}/dev" \
  "${namespace_root}/etc/systemd/system" \
  "${namespace_root}/proc" \
  "${namespace_root}/rehearsal/bin" \
  "${namespace_root}/run/lock" \
  "${namespace_root}/srv/gpu-platform/users/${rehearsal_user}/workspace" \
  "${namespace_root}/storage" \
  "${namespace_root}/sys" \
  "${namespace_root}/tmp" \
  "${namespace_root}/usr" \
  "${namespace_root}/var/log"
chmod 01777 "${namespace_root}/tmp"

mount --rbind /usr "${namespace_root}/usr"
mount --make-rslave "${namespace_root}/usr"
mount -o remount,bind,ro "${namespace_root}/usr"
mount -t tmpfs -o rw,nosuid,nodev,mode=0755 workspace-local \
  "${namespace_root}/usr/local"
mount --rbind /dev "${namespace_root}/dev"
mount --make-rslave "${namespace_root}/dev"
mount -t proc proc "${namespace_root}/proc"
mount --rbind /sys "${namespace_root}/sys"
mount --make-rslave "${namespace_root}/sys"

ln -s usr/bin "${namespace_root}/bin"
ln -s usr/lib "${namespace_root}/lib"
ln -s usr/lib64 "${namespace_root}/lib64"
ln -s usr/sbin "${namespace_root}/sbin"
ln -s /proc/mounts "${namespace_root}/etc/mtab"
install -m 0644 /etc/nsswitch.conf "${namespace_root}/etc/nsswitch.conf"
install -m 0644 /etc/ld.so.cache "${namespace_root}/etc/ld.so.cache"
printf 'root:x:0:0:root:/root:/bin/bash\n%s:x:%s:%s:Workspace Rehearsal:/workspace:/bin/bash\n' \
  "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
  >"${namespace_root}/etc/passwd"
printf 'root:x:0:\n%s:x:%s:\n' "${rehearsal_user}" "${rehearsal_gid}" \
  >"${namespace_root}/etc/group"

install -d -m 0755 \
  "${namespace_root}/usr/local/lib/h100-platform" \
  "${namespace_root}/usr/local/sbin"
install -m 0640 "${candidate_root}/scripts/h100-platform-common.sh" \
  "${namespace_root}/usr/local/lib/h100-platform/h100-platform-common.sh"
install -m 0750 "${candidate_root}/scripts/h100-workspace-alias" \
  "${namespace_root}/usr/local/sbin/h100-workspace-alias"
install -m 0660 /dev/null "${namespace_root}/var/log/h100-platform-audit.log"
install -m 0600 /dev/null \
  "${namespace_root}/rehearsal/enable-worker-namespace-rehearsal"
chown -R "${rehearsal_uid}:${rehearsal_gid}" \
  "${namespace_root}/srv/gpu-platform/users/${rehearsal_user}"
chmod 0700 \
  "${namespace_root}/srv/gpu-platform/users/${rehearsal_user}" \
  "${namespace_root}/srv/gpu-platform/users/${rehearsal_user}/workspace"

cat >"${namespace_root}/etc/systemd/system/h100-portal-worker.service" <<'EOF'
[Unit]
Description=Disposable workspace rehearsal Worker

[Service]
Type=oneshot
ExecStart=/bin/true
RemainAfterExit=yes
EOF
cat >"${namespace_root}/etc/systemd/system/h100-portal-api.service" <<'EOF'
[Unit]
Description=Disposable workspace rehearsal API

[Service]
Type=oneshot
ExecStart=/bin/true
RemainAfterExit=yes
EOF

cat >"${namespace_root}/rehearsal/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

unit_field() {
  local field_name=$1 unit_file=$2
  /usr/bin/mawk -F= -v field_name="${field_name}" \
    '$1 == field_name {print substr($0, index($0, "=") + 1)}' \
    "${unit_file}"
}

case "${1:-}" in
  show)
    [[ "$*" == *"--property MainPID"* \
      && "$*" == *"--value h100-portal-worker.service"* ]] || exit 64
    if [[ -f /run/rehearsal-worker-pid ]]; then
      cat /run/rehearsal-worker-pid
    else
      printf '0\n'
    fi
    ;;
  daemon-reload)
    shopt -s nullglob
    units=(/etc/systemd/system/storage-users-*.mount)
    if ((${#units[@]} > 0)); then
      systemd-analyze verify "${units[@]}" >>/rehearsal/systemd-analyze.log 2>&1
    fi
    ;;
  enable)
    [[ "${2:-}" == --now && $# == 3 ]] || exit 64
    unit_name=$3
    unit_path="/etc/systemd/system/${unit_name}"
    [[ -f "${unit_path}" && ! -L "${unit_path}" ]]
    systemd-analyze verify "${unit_path}" >>/rehearsal/systemd-analyze.log 2>&1
    mount_what="$(unit_field What "${unit_path}")"
    mount_where="$(unit_field Where "${unit_path}")"
    mount_options="$(unit_field Options "${unit_path}")"
    [[ -d "${mount_what}" && -d "${mount_where}" ]]
    mount -o "${mount_options}" "${mount_what}" "${mount_where}"
    if [[ "${REHEARSAL_DELAY_FINDMNT:-0}" == 1 ]]; then
      printf '2\n' >/run/rehearsal-findmnt-delay
    fi
    install -d -m 0755 /etc/systemd/system/multi-user.target.wants
    ln -s "../${unit_name}" "/etc/systemd/system/multi-user.target.wants/${unit_name}"
    if [[ "${REHEARSAL_FAIL_AFTER_MOUNT:-0}" == 1 ]]; then
      chmod 0600 "${unit_path}"
    fi
    ;;
  disable)
    [[ "${2:-}" == --now && $# == 3 ]] || exit 64
    unit_name=$3
    unit_path="/etc/systemd/system/${unit_name}"
    mount_where="$(unit_field Where "${unit_path}")"
    if findmnt --noheadings --mountpoint "${mount_where}" >/dev/null 2>&1; then
      umount "${mount_where}"
    fi
    rm -f -- "/etc/systemd/system/multi-user.target.wants/${unit_name}"
    ;;
  *)
    printf 'unsupported rehearsal systemctl operation: %s\n' "$*" >&2
    exit 64
    ;;
esac
EOF
chmod 0755 "${namespace_root}/rehearsal/bin/systemctl"

cat >"${namespace_root}/rehearsal/bin/findmnt" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ -f /run/rehearsal-findmnt-delay && "$*" == *"--mountpoint /storage/users/29991"* ]]; then
  remaining="$(< /run/rehearsal-findmnt-delay)"
  if ((remaining > 0)); then
    printf '%s\n' "$((remaining - 1))" >/run/rehearsal-findmnt-delay
    exit 1
  fi
  rm -f -- /run/rehearsal-findmnt-delay
fi
exec /usr/bin/findmnt "$@"
EOF
chmod 0755 "${namespace_root}/rehearsal/bin/findmnt"

cat >"${namespace_root}/rehearsal/run.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

readonly alias_tool=/usr/local/sbin/h100-workspace-alias
readonly rehearsal_user=workspace-rehearsal
readonly rehearsal_uid=29991
readonly rehearsal_gid=29991
readonly backing=/srv/gpu-platform/users/workspace-rehearsal/workspace
readonly canonical=/storage/users/29991
readonly unit=/etc/systemd/system/storage-users-29991.mount
readonly audit_log=/var/log/h100-platform-audit.log
readonly rehearsal_host_pid=$$
readonly -a required_directories=(
  projects datasets outputs .portal .portal/job-scripts .portal/jobs
  .portal/templates .portal/logs .portal/runtime
)

export PATH=/rehearsal/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export SUDO_USER=workspace-certifier

fail() {
  printf 'INNER REHEARSAL FAILED: %s\n' "$*" >&2
  exit 1
}

rehearsal_diagnostics() {
  local diagnostic_status=$?
  if ((diagnostic_status != 0)); then
    printf '%s\n' '--- audit log ---' >&2
    cat "${audit_log}" >&2 || true
    printf '%s\n' '--- workspace tree ---' >&2
    find "${backing}" -maxdepth 3 -printf '%p|%y|%u:%g|%m\n' >&2 || true
    printf '%s\n' '--- canonical state ---' >&2
    findmnt --noheadings --mountpoint "${canonical}" >&2 || true
    stat "${canonical}" "${unit}" >&2 || true
  fi
  exit "${diagnostic_status}"
}
trap rehearsal_diagnostics EXIT

remove_required_directories() {
  local directory_index target
  for ((directory_index = ${#required_directories[@]} - 1; directory_index >= 0; directory_index--)); do
    target="${backing}/${required_directories[directory_index]}"
    [[ ! -e "${target}" ]] || rmdir -- "${target}"
  done
}

"${alias_tool}" prepare "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
[[ "$(stat -c '%u:%g:%a' /storage/users)" == 0:0:711 ]]
[[ "$(stat -c '%u:%g' "${canonical}")" == "${rehearsal_uid}:${rehearsal_gid}" ]]
[[ "$(stat -c '%d:%i' "${backing}")" == "$(stat -c '%d:%i' "${canonical}")" ]]
for option in rw nosuid nodev; do
  findmnt --noheadings --output OPTIONS --mountpoint "${canonical}" \
    | tr ',' '\n' | grep -Fxq "${option}"
done
[[ -L /etc/systemd/system/multi-user.target.wants/storage-users-29991.mount ]]
"${alias_tool}" verify "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
"${alias_tool}" remove "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
  --confirm-remove "${rehearsal_user}"
! findmnt --noheadings --mountpoint "${canonical}" >/dev/null 2>&1
[[ ! -e "${canonical}" && ! -e "${unit}" ]]
[[ "$(grep -c 'action=workspace-alias.*outcome=SUCCESS rc=0' "${audit_log}")" == 3 ]]
remove_required_directories

# A newly mounted unit can become visible to findmnt just after systemctl has
# reported success. The bounded convergence check must absorb that transition.
REHEARSAL_DELAY_FINDMNT=1 "${alias_tool}" prepare \
  "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
"${alias_tool}" verify "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
"${alias_tool}" remove "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
  --confirm-remove "${rehearsal_user}"
[[ "$(grep -c 'action=workspace-alias.*outcome=SUCCESS rc=0' "${audit_log}")" == 6 ]]
remove_required_directories

# Reproduce the production Worker namespace: a recursive writable bind of
# /storage/users captures the pre-existing children and hides a later host
# mount.  Keep that namespace alive so prepare can enter PID 1's namespace for
# the host mount and then mirror the exact bind into the Worker namespace.
unshare --mount --propagation unchanged /bin/bash -c '
  set -euo pipefail
  mount --rbind /storage/users /storage/users
  printf "%s\n" "$$" >/run/rehearsal-worker-pid
  touch /run/rehearsal-worker-ready
  exec /bin/sleep 300
' &
worker_launcher_pid=$!
for _ in {1..50}; do
  [[ -f /run/rehearsal-worker-ready ]] && break
  sleep 0.1
done
[[ -f /run/rehearsal-worker-ready && -f /run/rehearsal-worker-pid ]]
worker_namespace_pid="$(</run/rehearsal-worker-pid)"
id "${rehearsal_user}" >/dev/null
nsenter --target "${rehearsal_host_pid}" --mount \
  --root="/proc/${rehearsal_host_pid}/root" \
  --wd="/proc/${rehearsal_host_pid}/cwd" -- \
  id "${rehearsal_user}" >/dev/null
nsenter --target "${worker_namespace_pid}" --mount -- \
  nsenter --target "${rehearsal_host_pid}" --mount \
    --root="/proc/${rehearsal_host_pid}/root" \
    --wd="/proc/${rehearsal_host_pid}/cwd" -- \
    id "${rehearsal_user}" >/dev/null
H100_WORKSPACE_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_WORKSPACE_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  nsenter --target "${worker_namespace_pid}" --mount \
    --root="/proc/${worker_namespace_pid}/root" \
    --wd="/proc/${worker_namespace_pid}/cwd" -- \
  "${alias_tool}" prepare "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
findmnt --noheadings --mountpoint "${canonical}" >/dev/null
nsenter --target "${worker_namespace_pid}" --mount \
  --root="/proc/${worker_namespace_pid}/root" \
  --wd="/proc/${worker_namespace_pid}/cwd" -- \
  findmnt --noheadings --mountpoint "${canonical}" >/dev/null
H100_WORKSPACE_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_WORKSPACE_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  "${alias_tool}" verify "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
H100_WORKSPACE_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_WORKSPACE_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  "${alias_tool}" remove "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
    --confirm-remove "${rehearsal_user}"
! nsenter --target "${worker_namespace_pid}" --mount \
  --root="/proc/${worker_namespace_pid}/root" \
  --wd="/proc/${worker_namespace_pid}/cwd" -- \
  findmnt --noheadings --mountpoint "${canonical}" >/dev/null 2>&1
kill "${worker_launcher_pid}"
wait "${worker_launcher_pid}" || true
rm -f -- /run/rehearsal-worker-pid /run/rehearsal-worker-ready
[[ "$(grep -c 'action=workspace-alias.*outcome=SUCCESS rc=0' "${audit_log}")" == 9 ]]
remove_required_directories

# Preserve pre-existing job metadata while forcing a post-mount validation
# failure. Cleanup must remove only directories created by this attempt.
install -d -o "${rehearsal_uid}" -g "${rehearsal_gid}" -m 0700 \
  "${backing}/.portal" "${backing}/.portal/job-scripts" "${backing}/.portal/jobs"
printf 'preserve\n' >"${backing}/.portal/job-scripts/existing"
printf 'preserve\n' >"${backing}/.portal/jobs/existing"
chown "${rehearsal_uid}:${rehearsal_gid}" \
  "${backing}/.portal/job-scripts/existing" "${backing}/.portal/jobs/existing"

if REHEARSAL_FAIL_AFTER_MOUNT=1 "${alias_tool}" prepare \
  "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
  >/rehearsal/failure.stdout 2>/rehearsal/failure.stderr; then
  fail 'injected post-mount validation failure unexpectedly succeeded'
fi
grep -Fq 'workspace mount unit metadata is invalid' /rehearsal/failure.stderr
! grep -Fq 'readonly variable' /rehearsal/failure.stderr
grep -q 'action=workspace-alias.*outcome=FAILED rc=1' "${audit_log}"
! findmnt --noheadings --mountpoint "${canonical}" >/dev/null 2>&1
[[ ! -e "${canonical}" && ! -e "${unit}" ]]
[[ -f "${backing}/.portal/job-scripts/existing" ]]
[[ -f "${backing}/.portal/jobs/existing" ]]
for transient in projects datasets outputs .portal/templates .portal/logs .portal/runtime; do
  [[ ! -e "${backing}/${transient}" ]]
done

audit_lines_before_mismatch="$(wc -l <"${audit_log}")"
if "${alias_tool}" prepare "${rehearsal_user}" 29992 "${rehearsal_gid}" \
  >/rehearsal/mismatch.stdout 2>/rehearsal/mismatch.stderr; then
  fail 'UID mismatch unexpectedly succeeded'
fi
grep -Fq 'workspace identity differs from NSS' /rehearsal/mismatch.stderr
[[ "$(wc -l <"${audit_log}")" == "${audit_lines_before_mismatch}" ]]
[[ ! -e "${canonical}" && ! -e "${unit}" ]]

printf 'owner_parent=%s owner_alias=%s\n' \
  "$(stat -c '%u:%g:%a' /storage/users)" "${rehearsal_uid}:${rehearsal_gid}"
printf 'audit_success=9 audit_failure=1 cleanup=PASS uid_mismatch=PASS\n'
printf 'systemd_unit_verify=PASS bind_inode=PASS mount_options=rw,nosuid,nodev convergence=PASS worker_namespace=PASS\n'
EOF
chmod 0755 "${namespace_root}/rehearsal/run.sh"

chroot "${namespace_root}" /usr/bin/env -i \
  HOME=/root LANG=C PATH=/rehearsal/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  /bin/bash /rehearsal/run.sh
cat "${namespace_root}/rehearsal/systemd-analyze.log"
