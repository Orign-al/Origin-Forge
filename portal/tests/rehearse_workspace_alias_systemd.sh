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
    if [[ -f /run/rehearsal-worker-pid ]]; then
      worker_pid="$(</run/rehearsal-worker-pid)"
      worker_storage_park=/run/rehearsal-worker-storage-park
      nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        install -d -o root -g root -m 0700 "${worker_storage_park}"
      nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        mount --move /storage/users "${worker_storage_park}"
      nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        mount --bind "${mount_what}" "${mount_where}"
      nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        mount --move "${worker_storage_park}" /storage/users
      nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        rmdir -- "${worker_storage_park}"
      # systemd's production ReadWritePaths namespace receives two propagated
      # bind records before the Host security remount. Recreate that exact
      # shadowing shape deterministically without touching the outer host.
      nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        install -d -o root -g root -m 0700 "${mount_where}"
      for _ in 1 2; do
        nsenter --target "${worker_pid}" --mount \
          --root="/proc/${worker_pid}/root" \
          --wd="/proc/${worker_pid}/cwd" -- \
          mount --bind "${mount_what}" "${mount_where}"
      done
    fi
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
    if [[ -f /run/rehearsal-worker-pid ]]; then
      worker_pid="$(</run/rehearsal-worker-pid)"
      worker_storage_park=/run/rehearsal-worker-storage-park
      nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        install -d -o root -g root -m 0700 "${worker_storage_park}"
      nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        mount --move /storage/users "${worker_storage_park}"
      if nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        findmnt --noheadings --mountpoint "${mount_where}" >/dev/null 2>&1; then
        nsenter --target "${worker_pid}" --mount \
          --root="/proc/${worker_pid}/root" \
          --wd="/proc/${worker_pid}/cwd" -- \
          umount "${mount_where}"
      fi
      nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        mount --move "${worker_storage_park}" /storage/users
      nsenter --target "${worker_pid}" --mount \
        --root="/proc/${worker_pid}/root" \
        --wd="/proc/${worker_pid}/cwd" -- \
        rmdir -- "${worker_storage_park}"
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
if [[ -f /run/rehearsal-covered-target && "$*" == *"--target /storage/users/29991"* ]]; then
  translated_arguments=()
  for argument in "$@"; do
    if [[ "${argument}" == --target ]]; then
      translated_arguments+=(--mountpoint)
    else
      translated_arguments+=("${argument}")
    fi
  done
  exec /rehearsal/bin/findmnt.real "${translated_arguments[@]}"
fi
exec /rehearsal/bin/findmnt.real "$@"
EOF
chmod 0755 "${namespace_root}/rehearsal/bin/findmnt"
install -m 0755 /usr/bin/findmnt "${namespace_root}/rehearsal/bin/findmnt.real"

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
    printf '%s\n' '--- injected failure ---' >&2
    cat /rehearsal/propagated-failure.stderr >&2 || true
    printf '%s\n' '--- collision recovery ---' >&2
    cat /rehearsal/nonempty.stderr >&2 || true
    cat /rehearsal/nonempty-recovery.stderr >&2 || true
    cat /rehearsal/covered-target-state >&2 || true
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

# Reproduce the production Worker namespace: systemd's private writable layer
# resolves an empty placeholder while mountinfo retains the host alias below
# it. The mock systemctl above moves this private layer aside only while it
# simulates propagation of the host mount event.
touch /run/rehearsal-covered-target
unshare --mount --propagation unchanged /bin/bash -c '
  set -euo pipefail
  mount -t tmpfs -o rw,nosuid,nodev,mode=0711 worker-storage /storage/users
  mount --make-private /storage/users
  mount --bind /rehearsal/bin/findmnt /usr/bin/findmnt
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
nsenter --target "${worker_namespace_pid}" --mount \
  --root="/proc/${worker_namespace_pid}/root" \
  --wd="/proc/${worker_namespace_pid}/cwd" -- \
  test ! -e "${canonical}"
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

# Force verify_alias to fail after the host mount has propagated. Even if
# Worker alias cleanup itself exits, the outer EXIT trap must still disable the
# host unit and remove the mountpoint/unit.
if H100_WORKSPACE_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_WORKSPACE_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  REHEARSAL_FAIL_AFTER_MOUNT=1 \
  nsenter --target "${worker_namespace_pid}" --mount \
    --root="/proc/${worker_namespace_pid}/root" \
    --wd="/proc/${worker_namespace_pid}/cwd" -- \
  "${alias_tool}" prepare "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
  >/rehearsal/propagated-failure.stdout 2>/rehearsal/propagated-failure.stderr; then
  fail 'propagated post-mount validation failure unexpectedly succeeded'
fi
grep -Fq 'workspace mount unit metadata is invalid' \
  /rehearsal/propagated-failure.stderr
! findmnt --noheadings --mountpoint "${canonical}" >/dev/null 2>&1
nsenter --target "${worker_namespace_pid}" --mount \
  --root="/proc/${worker_namespace_pid}/root" \
  --wd="/proc/${worker_namespace_pid}/cwd" -- \
  test ! -e "${canonical}"
[[ ! -e "${canonical}" && ! -e "${unit}" ]]
[[ "$(grep -c 'action=workspace-alias.*outcome=FAILED rc=1' "${audit_log}")" == 1 ]]
remove_required_directories

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
worker_mount_records="$(nsenter --target "${worker_namespace_pid}" --mount \
  --root="/proc/${worker_namespace_pid}/root" \
  --wd="/proc/${worker_namespace_pid}/cwd" -- \
  findmnt --noheadings --raw --output TARGET,VFS-OPTIONS \
  --mountpoint "${canonical}")"
(("$(wc -l <<<"${worker_mount_records}")" >= 2))
visible_worker_options="${worker_mount_records##*$'\n'}"
visible_worker_options="${visible_worker_options#* }"
for option in rw nosuid nodev; do
  tr ',' '\n' <<<"${visible_worker_options}" | grep -Fxq "${option}"
done
H100_WORKSPACE_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_WORKSPACE_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  "${alias_tool}" verify "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
# Once the visible alias layers are gone, a non-empty private placeholder must
# remain a collision even though every covered record has the approved source.
for _ in {1..8}; do
  if [[ "$(nsenter --target "${worker_namespace_pid}" --mount \
      --root="/proc/${worker_namespace_pid}/root" \
      --wd="/proc/${worker_namespace_pid}/cwd" -- \
      stat -c '%d:%i' "${canonical}")" != \
    "$(stat -c '%d:%i' "${backing}")" ]]; then
    break
  fi
  nsenter --target "${worker_namespace_pid}" --mount \
    --root="/proc/${worker_namespace_pid}/root" \
    --wd="/proc/${worker_namespace_pid}/cwd" -- \
    umount "${canonical}"
done
nsenter --target "${worker_namespace_pid}" --mount \
  --root="/proc/${worker_namespace_pid}/root" \
  --wd="/proc/${worker_namespace_pid}/cwd" -- \
  /usr/bin/findmnt --noheadings --raw --output TARGET,VFS-OPTIONS \
  --target "${canonical}" >/rehearsal/covered-target-state
grep -Fq "${canonical} " /rehearsal/covered-target-state
nsenter --target "${worker_namespace_pid}" --mount \
  --root="/proc/${worker_namespace_pid}/root" \
  --wd="/proc/${worker_namespace_pid}/cwd" -- \
  touch "${canonical}/unexpected-entry"
if H100_WORKSPACE_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_WORKSPACE_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  "${alias_tool}" remove "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
    --confirm-remove "${rehearsal_user}" \
    >/rehearsal/nonempty.stdout 2>/rehearsal/nonempty.stderr; then
  fail 'non-empty Worker placeholder unexpectedly passed removal'
fi
grep -Fq 'WORKER_REMOVE_COLLISION' /rehearsal/nonempty.stderr
findmnt --noheadings --mountpoint "${canonical}" >/dev/null
[[ -f "${unit}" ]]
nsenter --target "${worker_namespace_pid}" --mount \
  --root="/proc/${worker_namespace_pid}/root" \
  --wd="/proc/${worker_namespace_pid}/cwd" -- \
  rm -- "${canonical}/unexpected-entry"
H100_WORKSPACE_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_WORKSPACE_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  "${alias_tool}" remove "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
    --confirm-remove "${rehearsal_user}" \
    >/rehearsal/nonempty-recovery.stdout 2>/rehearsal/nonempty-recovery.stderr
nsenter --target "${worker_namespace_pid}" --mount \
  --root="/proc/${worker_namespace_pid}/root" \
  --wd="/proc/${worker_namespace_pid}/cwd" -- \
  test ! -e "${canonical}"
kill "${worker_launcher_pid}"
wait "${worker_launcher_pid}" || true
rm -f -- /run/rehearsal-worker-pid /run/rehearsal-worker-ready
[[ "$(grep -c 'action=workspace-alias.*outcome=SUCCESS rc=0' "${audit_log}")" == 9 ]]
remove_required_directories

# A covered record from any other filesystem/root remains fail closed even
# when its visible placeholder is root-owned, mode 0700, and empty.
install -d -o root -g root -m 0700 \
  "${canonical}" /rehearsal/foreign-workspace
unshare --mount --propagation unchanged /bin/bash -c '
  set -euo pipefail
  mount -t tmpfs -o rw,nosuid,nodev,mode=0711 worker-storage /storage/users
  mount --make-private /storage/users
  install -d -o root -g root -m 0700 /run/rehearsal-worker-storage-park
  mount --move /storage/users /run/rehearsal-worker-storage-park
  mount --bind /rehearsal/foreign-workspace /storage/users/29991
  mount --move /run/rehearsal-worker-storage-park /storage/users
  rmdir -- /run/rehearsal-worker-storage-park
  mount --bind /rehearsal/bin/findmnt /usr/bin/findmnt
  printf "%s\n" "$$" >/run/rehearsal-worker-pid
  touch /run/rehearsal-worker-ready
  exec /bin/sleep 300
' &
foreign_worker_launcher_pid=$!
for _ in {1..50}; do
  [[ -f /run/rehearsal-worker-ready ]] && break
  sleep 0.1
done
[[ -f /run/rehearsal-worker-ready && -f /run/rehearsal-worker-pid ]]
foreign_worker_namespace_pid="$(</run/rehearsal-worker-pid)"
H100_WORKSPACE_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_WORKSPACE_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  "${alias_tool}" prepare "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
if H100_WORKSPACE_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_WORKSPACE_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  "${alias_tool}" remove "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
    --confirm-remove "${rehearsal_user}" \
    >/rehearsal/foreign.stdout 2>/rehearsal/foreign.stderr; then
  fail 'foreign covered Worker mount unexpectedly passed removal'
fi
grep -Fq 'WORKER_REMOVE_COLLISION' /rehearsal/foreign.stderr
findmnt --noheadings --mountpoint "${canonical}" >/dev/null
[[ -f "${unit}" ]]
kill "${foreign_worker_launcher_pid}"
wait "${foreign_worker_launcher_pid}" || true
rm -f -- \
  /run/rehearsal-covered-target \
  /run/rehearsal-worker-pid \
  /run/rehearsal-worker-ready
"${alias_tool}" remove "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
  --confirm-remove "${rehearsal_user}"
[[ ! -e "${canonical}" && ! -e "${unit}" ]]
rmdir -- /rehearsal/foreign-workspace
[[ "$(grep -c 'action=workspace-alias.*outcome=SUCCESS rc=0' "${audit_log}")" == 11 ]]
[[ "$(grep -c 'action=workspace-alias.*outcome=FAILED rc=1' "${audit_log}")" == 3 ]]
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
[[ "$(grep -c 'action=workspace-alias.*outcome=FAILED rc=1' "${audit_log}")" == 4 ]]

printf 'owner_parent=%s owner_alias=%s\n' \
  "$(stat -c '%u:%g:%a' /storage/users)" "${rehearsal_uid}:${rehearsal_gid}"
printf 'audit_success=11 audit_failure=4 cleanup=PASS uid_mismatch=PASS\n'
printf 'systemd_unit_verify=PASS bind_inode=PASS mount_options=rw,nosuid,nodev convergence=PASS worker_namespace=PASS propagation_normalization=PASS covered_same_source_removal=PASS covered_collision_rejection=PASS\n'
EOF
chmod 0755 "${namespace_root}/rehearsal/run.sh"

chroot "${namespace_root}" /usr/bin/env -i \
  HOME=/root LANG=C PATH=/rehearsal/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  /bin/bash /rehearsal/run.sh
cat "${namespace_root}/rehearsal/systemd-analyze.log"
