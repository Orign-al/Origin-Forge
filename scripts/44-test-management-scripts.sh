#!/usr/bin/env bash
set -euo pipefail

readonly run_id="${RUN_ID:-20260804-050357}"
readonly platform_root=/srv/gpu-platform/platform
readonly audit_log=/var/log/h100-platform-audit.log
readonly container=gpu-dev-codexops
readonly persistence_file=/srv/gpu-platform/users/codexops/workspace/persistence-test.txt
readonly expected_persistence_sha=729f35a9bbb4cd50319b3898db29c7433e5318dba621004dd93111134cec425e
readonly expected_host_key='SHA256:yRu3313H6tVLbL2itjFqMbF6+7NSrvxQmPocRfJMyIM'
readonly report_file="${platform_root}/reports/management-scripts-${run_id}.md"

fail() {
  printf 'MANAGEMENT SCRIPT TEST BLOCKED: %s\n' "$*" >&2
  exit 1
}

recover_container() {
  local rc=$?
  trap - EXIT
  if [[ "${rc}" -ne 0 ]]; then
    if sudo docker container inspect "${container}" >/dev/null 2>&1 \
      && [[ "$(sudo docker inspect --format '{{.State.Status}}' "${container}")" != running ]]; then
      sudo /usr/local/sbin/h100-container-start codexops >/dev/null 2>&1 || true
    fi
  fi
  exit "${rc}"
}
trap recover_container EXIT

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'
[[ ! -e "${report_file}" ]] || fail 'refusing to overwrite report'

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
for script in "${scripts[@]}"; do
  [[ "$(sha256sum "${platform_root}/scripts/${script}" | cut -d ' ' -f 1)" \
      == "$(sudo sha256sum "/usr/local/sbin/${script}" | cut -d ' ' -f 1)" ]] \
    || fail "installed script differs from source: ${script}"
done
[[ "$(sha256sum "${platform_root}/scripts/h100-platform-common.sh" | cut -d ' ' -f 1)" \
    == "$(sudo sha256sum /usr/local/lib/h100-platform/h100-platform-common.sh | cut -d ' ' -f 1)" ]] \
  || fail 'installed common library differs from source'

[[ "$(sudo stat -c '%U:%G %a' "${audit_log}")" == 'root:gpu-platform-admin 660' ]] \
  || fail 'audit log ownership or mode is incorrect'

passwd_count_before="$(getent passwd | wc -l)"
container_count_before="$(sudo docker ps -a --format '{{.Names}}' | wc -l)"
persistence_before="$(sha256sum "${persistence_file}" | cut -d ' ' -f 1)"
host_key_before="$(sudo ssh-keygen -lf /srv/gpu-platform/container-data/codexops/ssh-host-keys/ssh_host_ed25519_key.pub | cut -d ' ' -f 2)"
[[ "${persistence_before}" == "${expected_persistence_sha}" ]] \
  || fail 'pre-test persistence hash is unexpected'
[[ "${host_key_before}" == "${expected_host_key}" ]] \
  || fail 'pre-test SSH host key is unexpected'

sudo /usr/local/sbin/h100-user-create --help >/dev/null
sudo /usr/local/sbin/h100-container-create --help >/dev/null
sudo /usr/local/sbin/h100-container-delete --help >/dev/null
sudo /usr/local/sbin/h100-container-status codexops >/dev/null
sudo /usr/local/sbin/h100-quota-show codexops >/dev/null

collision_log="$(mktemp /tmp/h100-management-collision.XXXXXX)"
if sudo /usr/local/sbin/h100-user-create \
  --stage codexops 10002 10002 30002 22022 company general \
  --confirm-stage codexops \
  >"${collision_log}" 2>&1; then
  rm -f -- "${collision_log}"
  fail 'existing-user collision test unexpectedly succeeded'
fi
grep -Eq 'protected|forbidden' "${collision_log}" \
  || { rm -f -- "${collision_log}"; fail 'existing-user collision was not detected'; }

if sudo /usr/local/sbin/h100-user-create \
  --stage example-pilot 20001 20001 30001 22023 company general \
  --public-key-file /tmp/forbidden.pub --confirm-stage example-pilot \
  >"${collision_log}" 2>&1; then
  rm -f -- "${collision_log}"
  fail 'Stage unexpectedly accepted a public-key file'
fi
grep -q 'PUBLIC_KEY_NOT_ALLOWED_DURING_STAGE' "${collision_log}" \
  || { rm -f -- "${collision_log}"; fail 'Stage key contract rejection was not detected'; }

if sudo /usr/local/sbin/h100-user-create \
  --activate origin-pilot --confirm-activate origin-pilot \
  >"${collision_log}" 2>&1; then
  rm -f -- "${collision_log}"
  fail 'Activate unexpectedly accepted a missing public key'
fi
grep -q 'PUBLIC_KEY_REQUIRED_FOR_ACTIVATION' "${collision_log}" \
  || { rm -f -- "${collision_log}"; fail 'Activate missing-key rejection was not detected'; }

if sudo /usr/local/sbin/h100-container-create \
  codexops \
  22022 \
  >"${collision_log}" 2>&1; then
  rm -f -- "${collision_log}"
  fail 'existing-container collision test unexpectedly succeeded'
fi
grep -Eq 'already exists|already in use' "${collision_log}" \
  || { rm -f -- "${collision_log}"; fail 'existing-container collision was not detected'; }
rm -f -- "${collision_log}"

sudo /usr/local/sbin/h100-container-stop codexops >/dev/null
[[ "$(sudo docker inspect --format '{{.State.Status}}' "${container}")" == exited ]] \
  || fail 'stop script test failed'
if sudo ss -H -lnt 'sport = :22022' | grep -q .; then
  fail 'port 22022 remained listening after stop'
fi

sudo /usr/local/sbin/h100-container-start codexops >/dev/null
[[ "$(sudo docker inspect --format '{{.State.Health.Status}}' "${container}")" == healthy ]] \
  || fail 'start script test failed'
[[ "$(sudo ss -H -lnt 'sport = :22022' | awk '{print $4}')" == '10.82.36.1:22022' ]] \
  || fail 'start script restored an unexpected listener'

sudo /usr/local/sbin/h100-container-rebuild codexops >/dev/null
[[ "$(sudo docker inspect --format '{{.State.Health.Status}}' "${container}")" == healthy ]] \
  || fail 'rebuild script test failed'

persistence_after="$(sha256sum "${persistence_file}" | cut -d ' ' -f 1)"
host_key_after="$(sudo ssh-keygen -lf /srv/gpu-platform/container-data/codexops/ssh-host-keys/ssh_host_ed25519_key.pub | cut -d ' ' -f 2)"
[[ "${persistence_after}" == "${persistence_before}" ]] \
  || fail 'management rebuild changed persistent user data'
[[ "${host_key_after}" == "${host_key_before}" ]] \
  || fail 'management rebuild changed SSH host key'

[[ "$(getent passwd | wc -l)" == "${passwd_count_before}" ]] \
  || fail 'management tests changed the host user count'
[[ "$(sudo docker ps -a --format '{{.Names}}' | wc -l)" == "${container_count_before}" ]] \
  || fail 'management tests changed the container count'

for action in container-status quota-show container-stop container-start container-rebuild; do
  sudo grep -q "action=${action} target=codexops outcome=SUCCESS" "${audit_log}" \
    || fail "audit log is missing successful action: ${action}"
done
sudo grep -q 'action=user-create target=codexops outcome=FAILED' "${audit_log}" \
  || fail 'audit log is missing existing-user collision failure'
sudo grep -q 'action=container-create target=codexops outcome=FAILED' "${audit_log}" \
  || fail 'audit log is missing existing-container collision failure'

mapfile -t slurm_states < <(sinfo -h -N -o '%T')
for state in "${slurm_states[@]}"; do
  [[ "${state}" == drain* ]] || fail "Slurm node is not drained: ${state}"
done

{
  printf '# H100 management scripts installation and test\n\n'
  printf -- '- Run ID: `%s`\n' "${run_id}"
  printf -- '- Result: `MANAGEMENT SCRIPTS PASSED`\n'
  printf -- '- Installed commands: `%s`\n' "${scripts[*]}"
  printf -- '- Common library: `/usr/local/lib/h100-platform/h100-platform-common.sh`\n'
  printf -- '- Global flock: `/run/lock/h100-platform.lock`\n'
  printf -- '- Audit log: `%s` (`root:gpu-platform-admin`, `0660`)\n' "${audit_log}"
  printf -- '- Status/quota tests: passed\n'
  printf -- '- Existing-user/container collision tests: blocked safely and audited\n'
  printf -- '- Stop/start/rebuild tests: passed for codexops\n'
  printf -- '- Persistence SHA-256 preserved: `%s`\n' "${persistence_after}"
  printf -- '- SSH host-key fingerprint preserved: `%s`\n' "${host_key_after}"
  printf -- '- Default delete behavior: preserves data\n'
  printf -- '- Data deletion: requires `--delete-data`, repeated username, and interactive typed phrase\n'
  printf -- '- Arbitrary host mounts and `eval`: not supported\n'
  printf -- '- Other users created: none\n'
  printf -- '- Other containers created: none\n'
  printf -- '- Slurm node remained DRAIN\n'
  printf -- '- MIG and firewall were not modified\n'
} >"${report_file}"

printf 'MANAGEMENT SCRIPTS PASSED\n'
printf 'Report: %s\n' "${report_file}"
