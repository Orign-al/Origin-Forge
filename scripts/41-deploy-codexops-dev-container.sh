#!/usr/bin/env bash
set -euo pipefail

readonly run_id="${RUN_ID:-20260804-050357}"
readonly platform_root=/srv/gpu-platform/platform
readonly source_compose="${platform_root}/templates/dev-container/compose.codexops.yml"
readonly config_dir="${platform_root}/config/dev-containers/codexops"
readonly compose_file="${config_dir}/compose.yml"
readonly image='h100-local/dev-container:ubuntu24.04-codexops-20260804'
readonly container=gpu-dev-codexops
readonly management_ip=10.82.36.1
readonly ssh_port=22022
readonly user_root=/srv/gpu-platform/users/codexops
readonly host_key_dir=/srv/gpu-platform/container-data/codexops/ssh-host-keys

stamp="$(date +%Y%m%d-%H%M%S-%N)"
backup_dir="${platform_root}/backups/codexops-dev-container-${stamp}"
report_file="${platform_root}/reports/codexops-dev-container-deploy-${run_id}.md"

fail() {
  printf 'CODEXOPS DEV CONTAINER BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'
[[ -f "${source_compose}" ]] || fail 'source Compose file is missing'
[[ ! -e "${compose_file}" ]] || fail 'refusing to overwrite existing Compose file'
[[ ! -e "${report_file}" ]] || fail 'refusing to overwrite deployment report'

for path in \
  "${user_root}/home" \
  "${user_root}/workspace" \
  "${user_root}/shared"; do
  [[ -d "${path}" ]] || fail "required bind source is missing: ${path}"
done
sudo test -d "${host_key_dir}" \
  || fail "required root-only bind source is missing: ${host_key_dir}"

ip -br addr | grep -Eq "^[^[:space:]]+[[:space:]]+UP[[:space:]]+.*${management_ip}/" \
  || fail "management IP is not active: ${management_ip}"
if sudo ss -H -lnt "sport = :${ssh_port}" | grep -q .; then
  fail "TCP port is already in use: ${ssh_port}"
fi
if sudo docker container inspect "${container}" >/dev/null 2>&1; then
  fail "container already exists: ${container}"
fi

sudo docker image inspect "${image}" \
  | jq -e '
      .[0].Config.Labels["h100.dev.user"] == "codexops"
      and .[0].Config.Labels["h100.dev.uid"] == "1001"
      and .[0].Config.Labels["h100.dev.gid"] == "1001"
      and .[0].Config.Labels["h100.base.digest"] == "sha256:019e8eb29a85e74d64925745884f2ec79aa27e3feab36353d24656f4d6b89467"
    ' >/dev/null \
  || fail 'development image labels are invalid'

quota_report="$(sudo xfs_quota -x -c 'report -p -h' /srv/gpu-platform)"
grep -E '^h100_codexops[[:space:]]' <<<"${quota_report}" | grep -q '300G' \
  || fail 'codexops 300GB project quota is not active'

mapfile -t slurm_states < <(sinfo -h -N -o '%T')
if ((${#slurm_states[@]} == 0)); then
  fail 'Slurm returned no node state'
fi
for state in "${slurm_states[@]}"; do
  [[ "${state}" == drain* ]] || fail "Slurm node is not drained: ${state}"
done

install -d -m 2770 "${backup_dir}"
printf 'Compose target and container were absent before deployment.\n' \
  >"${backup_dir}/state"

sudo install -d -o root -g gpu-platform-admin -m 0750 "${config_dir}"
sudo install -o root -g gpu-platform-admin -m 0640 \
  "${source_compose}" \
  "${compose_file}"

sudo docker compose \
  --project-directory "${config_dir}" \
  --file "${compose_file}" \
  config --quiet
sudo docker compose \
  --project-directory "${config_dir}" \
  --file "${compose_file}" \
  up --detach --no-build

healthy=no
for _ in $(seq 1 120); do
  status="$(sudo docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container}")"
  if [[ "${status}" == healthy ]]; then
    healthy=yes
    break
  fi
  if [[ "${status}" == exited || "${status}" == dead ]]; then
    break
  fi
  sleep 1
done
if [[ "${healthy}" != yes ]]; then
  sudo docker logs --tail 100 "${container}" >&2 || true
  fail "container did not become healthy"
fi

sudo docker inspect "${container}" | jq -e '
  .[0] as $c
  | $c.HostConfig.Privileged == false
  and $c.HostConfig.NetworkMode != "host"
  and $c.HostConfig.PidMode != "host"
  and $c.HostConfig.IpcMode != "host"
  and (($c.HostConfig.DeviceRequests // []) | length == 0)
  and (($c.HostConfig.Devices // []) | length == 0)
  and $c.HostConfig.NanoCpus == 8000000000
  and $c.HostConfig.Memory == 34359738368
  and $c.HostConfig.PidsLimit == 4096
  and $c.HostConfig.RestartPolicy.Name == "unless-stopped"
  and $c.HostConfig.PortBindings["22/tcp"][0].HostIp == "10.82.36.1"
  and $c.HostConfig.PortBindings["22/tcp"][0].HostPort == "22022"
' >/dev/null || fail 'container isolation or resource validation failed'

mapfile -t actual_mounts < <(
  sudo docker inspect "${container}" \
    | jq -r '.[0].Mounts[] | [.Source,.Destination,(.RW|tostring)] | join("|")' \
    | sort
)
expected_mounts=(
  '/srv/gpu-platform/container-data/codexops/ssh-host-keys|/etc/ssh/persistent|true'
  '/srv/gpu-platform/users/codexops/home|/home/codexops|true'
  '/srv/gpu-platform/users/codexops/shared|/shared|true'
  '/srv/gpu-platform/users/codexops/workspace|/workspace|true'
)
if [[ "$(printf '%s\n' "${actual_mounts[@]}")" != "$(printf '%s\n' "${expected_mounts[@]}")" ]]; then
  fail 'container bind-mount set is unexpected'
fi

if printf '%s\n' "${actual_mounts[@]}" \
  | grep -Eq 'docker\.sock|/var/run|/etc\||/root\||munge'; then
  fail 'forbidden host mount detected'
fi

listen_address="$(sudo ss -H -lnt "sport = :${ssh_port}" | awk '{print $4}')"
[[ "${listen_address}" == "${management_ip}:${ssh_port}" ]] \
  || fail "SSH port is not bound only to ${management_ip}:${ssh_port}"

sudo docker exec "${container}" /bin/bash -lc '
  set -euo pipefail
  test "$(id -u codexops)" = 1001
  test "$(id -g codexops)" = 1001
  test "$(sudo -u codexops sudo -n id -u)" = 0
  test ! -e /usr/bin/docker
  test ! -e /usr/bin/nvidia-smi
  test ! -S /var/run/docker.sock
  test -d /workspace
  test -d /shared
'

host_key_fingerprint="$(sudo ssh-keygen -lf "${host_key_dir}/ssh_host_ed25519_key.pub")"
image_id="$(sudo docker image inspect --format '{{.Id}}' "${image}")"

{
  printf '# codexops long-lived development container deployment\n\n'
  printf -- '- Run ID: `%s`\n' "${run_id}"
  printf -- '- Result: `CODEXOPS DEV CONTAINER DEPLOY PASSED`\n'
  printf -- '- Container: `%s`\n' "${container}"
  printf -- '- Image: `%s`\n' "${image}"
  printf -- '- Image ID: `%s`\n' "${image_id}"
  printf -- '- SSH listener: `%s:%s`\n' "${management_ip}" "${ssh_port}"
  printf -- '- SSH host-key fingerprint: `%s`\n' "${host_key_fingerprint}"
  printf -- '- CPU limit: `8`\n'
  printf -- '- Memory limit: `32GiB`\n'
  printf -- '- PIDs limit: `4096`\n'
  printf -- '- Project quota: `300GB`\n'
  printf -- '- Privileged: `false`\n'
  printf -- '- Host network/PID/IPC: disabled\n'
  printf -- '- GPU device requests: none\n'
  printf -- '- Docker socket: not mounted\n'
  printf -- '- Docker CLI/daemon: not installed\n'
  printf -- '- Host NVIDIA driver tools: not installed\n'
  printf -- '- Compose file: `%s` (`root:gpu-platform-admin`, mode `0640`)\n' "${compose_file}"
  printf -- '- Backup record: `%s`\n' "${backup_dir}"
  printf -- '- Slurm node remained DRAIN\n'
  printf -- '- MIG was not modified\n'
  printf -- '- Firewall was not modified\n'
} >"${report_file}"

printf 'CODEXOPS DEV CONTAINER DEPLOY PASSED\n'
printf 'SSH host-key fingerprint: %s\n' "${host_key_fingerprint}"
printf 'Report: %s\n' "${report_file}"
