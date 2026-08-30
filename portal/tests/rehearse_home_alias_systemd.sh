#!/usr/bin/env bash
set -euo pipefail

portal_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly portal_root
platform_root="$(cd "${portal_root}/.." && pwd)"
readonly platform_root
readonly rehearsal_uid=29992
readonly rehearsal_gid=29992
readonly rehearsal_user=home-rehearsal

fail() {
  printf 'HOME REHEARSAL FAILED: %s\n' "$*" >&2
  exit 1
}

cleanup_outer() {
  local cleanup_status=$?
  if [[ -n "${rehearsal_root:-}" ]]; then
    case "${rehearsal_root}" in
      /tmp/h100-home-systemd-rehearsal.*) rm -rf -- "${rehearsal_root}" ;;
      *)
        printf 'refusing to clean unexpected rehearsal root: %s\n' \
          "${rehearsal_root}" >&2
        cleanup_status=1
        ;;
    esac
  fi
  exit "${cleanup_status}"
}

if [[ "${1:-}" != --inside-mount-namespace ]]; then
  [[ "${EUID}" == 0 ]] || fail 'run as root in a disposable test environment'
  rehearsal_root="$(mktemp -d /tmp/h100-home-systemd-rehearsal.XXXXXX)"
  trap cleanup_outer EXIT
  unshare --mount --propagation private \
    "${BASH_SOURCE[0]}" --inside-mount-namespace "${rehearsal_root}" "${platform_root}"
  printf 'SYSTEMD-EQUIVALENT HOME ALIAS REHEARSAL PASS\n'
  exit 0
fi

[[ "${EUID}" == 0 ]] || fail 'mount namespace setup requires root'
readonly namespace_root=${2:?missing namespace root}
readonly candidate_root=${3:?missing candidate root}
[[ "${namespace_root}" == /tmp/h100-home-systemd-rehearsal.* ]] \
  || fail 'namespace root is outside the disposable contract'
[[ -f "${candidate_root}/scripts/h100-home-alias" ]] \
  || fail 'candidate home alias is unavailable'

install -d -m 0755 \
  "${namespace_root}/dev" \
  "${namespace_root}/etc/systemd/system" \
  "${namespace_root}/proc" \
  "${namespace_root}/rehearsal/bin" \
  "${namespace_root}/run/lock" \
  "${namespace_root}/srv/gpu-platform/users/${rehearsal_user}/home" \
  "${namespace_root}/storage" \
  "${namespace_root}/sys" \
  "${namespace_root}/tmp" \
  "${namespace_root}/usr" \
  "${namespace_root}/var/log"
chmod 01777 "${namespace_root}/tmp"

mount --rbind /usr "${namespace_root}/usr"
mount --make-rslave "${namespace_root}/usr"
mount -o remount,bind,ro "${namespace_root}/usr"
mount -t tmpfs -o rw,nosuid,nodev,mode=0755 home-local \
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
printf 'root:x:0:0:root:/root:/bin/bash\n%s:x:%s:%s:Home Rehearsal:/home/%s:/bin/bash\n' \
  "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" "${rehearsal_user}" \
  >"${namespace_root}/etc/passwd"
printf 'root:x:0:\n%s:x:%s:\n' "${rehearsal_user}" "${rehearsal_gid}" \
  >"${namespace_root}/etc/group"

install -d -m 0755 \
  "${namespace_root}/usr/local/lib/h100-platform" \
  "${namespace_root}/usr/local/sbin"
install -m 0640 "${candidate_root}/scripts/h100-platform-common.sh" \
  "${namespace_root}/usr/local/lib/h100-platform/h100-platform-common.sh"
install -m 0750 "${candidate_root}/scripts/h100-home-alias" \
  "${namespace_root}/usr/local/sbin/h100-home-alias"
install -m 0660 /dev/null "${namespace_root}/var/log/h100-platform-audit.log"
install -m 0600 /dev/null \
  "${namespace_root}/rehearsal/enable-home-alias-namespace-rehearsal"
chown -R "${rehearsal_uid}:${rehearsal_gid}" \
  "${namespace_root}/srv/gpu-platform/users/${rehearsal_user}"
chmod 0700 \
  "${namespace_root}/srv/gpu-platform/users/${rehearsal_user}" \
  "${namespace_root}/srv/gpu-platform/users/${rehearsal_user}/home"

cat >"${namespace_root}/etc/systemd/system/h100-portal-worker.service" <<'EOF'
[Unit]
Description=Disposable home rehearsal Worker

[Service]
Type=oneshot
ExecStart=/bin/true
RemainAfterExit=yes
EOF
cat >"${namespace_root}/etc/systemd/system/h100-portal-api.service" <<'EOF'
[Unit]
Description=Disposable home rehearsal API

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
    '$1 == field_name {print substr($0, index($0, "=") + 1)}' "${unit_file}"
}

worker_exec() {
  local worker_pid=$1
  shift
  nsenter --target "${worker_pid}" --mount \
    --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- "$@"
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
    units=(/etc/systemd/system/storage-homes-*.mount)
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
      worker_park=/run/rehearsal-worker-home-park
      worker_exec "${worker_pid}" install -d -o root -g root -m 0700 "${worker_park}"
      worker_exec "${worker_pid}" mount --move /storage/homes "${worker_park}"
      worker_exec "${worker_pid}" mount --bind "${mount_what}" "${mount_where}"
      worker_exec "${worker_pid}" mount --move "${worker_park}" /storage/homes
      worker_exec "${worker_pid}" rmdir -- "${worker_park}"
      # The private writable parent exposes its own empty placeholder while
      # mountinfo retains the newly propagated host record underneath it.
      worker_exec "${worker_pid}" install -d -o root -g root -m 0700 "${mount_where}"
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
      worker_park=/run/rehearsal-worker-home-park
      worker_exec "${worker_pid}" install -d -o root -g root -m 0700 "${worker_park}"
      worker_exec "${worker_pid}" mount --move /storage/homes "${worker_park}"
      if worker_exec "${worker_pid}" findmnt --noheadings --mountpoint \
        "${mount_where}" >/dev/null 2>&1; then
        worker_exec "${worker_pid}" umount "${mount_where}"
      fi
      worker_exec "${worker_pid}" mount --move "${worker_park}" /storage/homes
      worker_exec "${worker_pid}" rmdir -- "${worker_park}"
      if worker_exec "${worker_pid}" test -d "${mount_where}" \
        && worker_exec "${worker_pid}" test ! -L "${mount_where}" \
        && [[ -z "$(worker_exec "${worker_pid}" find "${mount_where}" \
          -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        worker_exec "${worker_pid}" rmdir -- "${mount_where}"
      fi
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

cat >"${namespace_root}/rehearsal/run.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

readonly alias_tool=/usr/local/sbin/h100-home-alias
readonly rehearsal_user=home-rehearsal
readonly rehearsal_uid=29992
readonly rehearsal_gid=29992
readonly backing=/srv/gpu-platform/users/home-rehearsal/home
readonly canonical=/storage/homes/29992
readonly unit=/etc/systemd/system/storage-homes-29992.mount
readonly audit_log=/var/log/h100-platform-audit.log
readonly rehearsal_host_pid=$$

export PATH=/rehearsal/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export SUDO_USER=home-certifier

diagnostics() {
  local diagnostic_status=$?
  if ((diagnostic_status != 0)); then
    printf '%s\n' '--- audit log ---' >&2
    cat "${audit_log}" >&2 || true
    printf '%s\n' '--- failure stderr ---' >&2
    cat /rehearsal/propagated-failure.stderr >&2 || true
    cat /rehearsal/nonempty.stderr >&2 || true
    printf '%s\n' '--- host mount state ---' >&2
    findmnt --noheadings --target /storage/homes >&2 || true
    stat "${canonical}" "${unit}" >&2 || true
  fi
  exit "${diagnostic_status}"
}
trap diagnostics EXIT

# Baseline systemd unit, inode, ownership, options, and removal contract.
"${alias_tool}" prepare "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
[[ "$(stat -c '%u:%g:%a' /storage/homes)" == 0:0:711 ]]
[[ "$(stat -c '%d:%i' "${backing}")" == "$(stat -c '%d:%i' "${canonical}")" ]]
"${alias_tool}" verify "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
"${alias_tool}" remove "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
  --confirm-remove "${rehearsal_user}"
[[ ! -e "${canonical}" && ! -e "${unit}" ]]

# Reproduce a running Worker's private writable parent. The mock systemctl
# propagates the host record below this parent, leaving an empty visible path.
unshare --mount --propagation unchanged /bin/bash -c '
  set -euo pipefail
  mount -t tmpfs -o rw,nosuid,nodev,mode=0711 worker-homes /storage/homes
  mount --make-private /storage/homes
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
worker_pid="$(</run/rehearsal-worker-pid)"

H100_HOME_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_HOME_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  nsenter --target "${worker_pid}" --mount \
    --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
  "${alias_tool}" prepare "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}"
findmnt --noheadings --mountpoint "${canonical}" >/dev/null
nsenter --target "${worker_pid}" --mount \
  --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
  test "$(stat -c '%d:%i' "${backing}")" = \
    "$(nsenter --target "${worker_pid}" --mount \
      --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
      stat -c '%d:%i' "${canonical}")"
worker_records="$(nsenter --target "${worker_pid}" --mount \
  --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
  findmnt --noheadings --raw --output TARGET,VFS-OPTIONS --mountpoint "${canonical}")"
(("$(wc -l <<<"${worker_records}")" >= 2))
visible_options="${worker_records##*$'\n'}"
visible_options="${visible_options#* }"
for option in rw nosuid nodev; do
  tr ',' '\n' <<<"${visible_options}" | grep -Fxq "${option}"
done
# Remove the visible owner bind layers, then prove a non-empty covered
# placeholder remains fail closed and does not tear down the host unit.
for _ in {1..8}; do
  worker_identity="$(nsenter --target "${worker_pid}" --mount \
    --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
    stat -c '%d:%i' "${canonical}")"
  [[ "${worker_identity}" == "$(stat -c '%d:%i' "${backing}")" ]] || break
  nsenter --target "${worker_pid}" --mount \
    --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
    umount "${canonical}"
done
nsenter --target "${worker_pid}" --mount \
  --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
  touch "${canonical}/unexpected-entry"
if H100_HOME_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_HOME_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  "${alias_tool}" remove "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
    --confirm-remove "${rehearsal_user}" \
    >/rehearsal/nonempty.stdout 2>/rehearsal/nonempty.stderr; then
  printf 'non-empty Worker placeholder unexpectedly passed removal\n' >&2
  exit 1
fi
grep -Fq 'WORKER_REMOVE_COLLISION' /rehearsal/nonempty.stderr
findmnt --noheadings --mountpoint "${canonical}" >/dev/null
[[ -f "${unit}" ]]
nsenter --target "${worker_pid}" --mount \
  --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
  rm -- "${canonical}/unexpected-entry"
H100_HOME_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_HOME_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  "${alias_tool}" remove "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
    --confirm-remove "${rehearsal_user}"
[[ ! -e "${canonical}" && ! -e "${unit}" ]]
nsenter --target "${worker_pid}" --mount \
  --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
  test ! -e "${canonical}"

# A post-mount validation failure must remove both the hidden Worker record
# and the host unit without leaving a mountpoint or owner data mutation.
if H100_HOME_ALIAS_NAMESPACE_REHEARSAL=1 \
  H100_HOME_ALIAS_REHEARSAL_HOST_PID="${rehearsal_host_pid}" \
  REHEARSAL_FAIL_AFTER_MOUNT=1 \
  nsenter --target "${worker_pid}" --mount \
    --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
  "${alias_tool}" prepare "${rehearsal_user}" "${rehearsal_uid}" "${rehearsal_gid}" \
  >/rehearsal/propagated-failure.stdout 2>/rehearsal/propagated-failure.stderr; then
  printf 'post-mount failure unexpectedly succeeded\n' >&2
  exit 1
fi
grep -Fq 'home mount unit metadata is invalid' /rehearsal/propagated-failure.stderr
[[ ! -e "${canonical}" && ! -e "${unit}" ]]
nsenter --target "${worker_pid}" --mount \
  --root="/proc/${worker_pid}/root" --wd="/proc/${worker_pid}/cwd" -- \
  test ! -e "${canonical}"

kill "${worker_launcher_pid}"
wait "${worker_launcher_pid}" || true
rm -f -- /run/rehearsal-worker-pid /run/rehearsal-worker-ready
[[ "$(grep -c 'action=home-alias.*outcome=SUCCESS rc=0' "${audit_log}")" == 5 ]]
[[ "$(grep -c 'action=home-alias.*outcome=FAILED rc=1' "${audit_log}")" == 2 ]]
printf 'home_alias_inode=PASS owner=PASS options=PASS worker_shadow_mount=PASS cleanup=PASS\n'
EOF
chmod 0755 "${namespace_root}/rehearsal/run.sh"

chroot "${namespace_root}" /usr/bin/env -i \
  HOME=/root LANG=C \
  PATH=/rehearsal/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  /bin/bash /rehearsal/run.sh
cat "${namespace_root}/rehearsal/systemd-analyze.log"
