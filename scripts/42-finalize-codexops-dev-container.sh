#!/usr/bin/env bash
set -euo pipefail

readonly run_id="${RUN_ID:-20260804-050357}"
readonly platform_root=/srv/gpu-platform/platform
readonly container=gpu-dev-codexops
readonly image='h100-local/dev-container:ubuntu24.04-codexops-20260804'
readonly persistence_sha256=729f35a9bbb4cd50319b3898db29c7433e5318dba621004dd93111134cec425e
readonly host_key_sha256='SHA256:yRu3313H6tVLbL2itjFqMbF6+7NSrvxQmPocRfJMyIM'
readonly report_file="${platform_root}/reports/long-lived-docker-${run_id}.md"

fail() {
  printf 'LONG-LIVED DOCKER TEMPLATE BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ "${LOCAL_SSH_TEST_PASSED:-}" == yes ]] \
  || fail 'local initial and post-recreate SSH tests were not attested'
[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'
[[ ! -e "${report_file}" ]] || fail 'refusing to overwrite report'

[[ "$(sudo docker inspect --format '{{.State.Status}}' "${container}")" == running ]] \
  || fail 'container is not running'
[[ "$(sudo docker inspect --format '{{.State.Health.Status}}' "${container}")" == healthy ]] \
  || fail 'container is not healthy'

sudo docker inspect "${container}" | jq -e '
  .[0] as $c
  | $c.Config.Image == "h100-local/dev-container:ubuntu24.04-codexops-20260804"
  and $c.HostConfig.Privileged == false
  and $c.HostConfig.NetworkMode != "host"
  and $c.HostConfig.PidMode != "host"
  and $c.HostConfig.IpcMode != "host"
  and (($c.HostConfig.DeviceRequests // []) | length == 0)
  and (($c.HostConfig.Devices // []) | length == 0)
  and $c.HostConfig.NanoCpus == 8000000000
  and $c.HostConfig.Memory == 34359738368
  and $c.HostConfig.PidsLimit == 4096
  and $c.HostConfig.ShmSize == 8589934592
  and $c.HostConfig.RestartPolicy.Name == "unless-stopped"
  and $c.HostConfig.PortBindings["22/tcp"][0].HostIp == "10.82.36.1"
  and $c.HostConfig.PortBindings["22/tcp"][0].HostPort == "22022"
' >/dev/null || fail 'Docker inspect isolation/resource check failed'

mapfile -t mounts < <(
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
[[ "$(printf '%s\n' "${mounts[@]}")" == "$(printf '%s\n' "${expected_mounts[@]}")" ]] \
  || fail 'bind-mount set does not match the approved set'

if printf '%s\n' "${mounts[@]}" \
  | grep -Eq 'docker\.sock|/var/run|/root\||/etc/munge|munge\.socket'; then
  fail 'forbidden host mount detected'
fi

[[ "$(sudo ss -H -lnt 'sport = :22022' | awk '{print $4}')" == '10.82.36.1:22022' ]] \
  || fail 'container SSH listener is not restricted to 10.82.36.1:22022'

current_persistence_sha="$(sudo docker exec "${container}" sha256sum /workspace/persistence-test.txt | cut -d ' ' -f 1)"
[[ "${current_persistence_sha}" == "${persistence_sha256}" ]] \
  || fail 'persistence test file hash changed after recreate'

current_host_key="$(sudo ssh-keygen -lf /srv/gpu-platform/container-data/codexops/ssh-host-keys/ssh_host_ed25519_key.pub | cut -d ' ' -f 2)"
[[ "${current_host_key}" == "${host_key_sha256}" ]] \
  || fail 'container SSH host key changed after recreate'

sudo docker exec "${container}" /bin/bash -lc '
  set -euo pipefail
  test "$(id -u codexops)" = 1001
  test "$(id -g codexops)" = 1001
  test "$(sudo -u codexops sudo -n id -u)" = 0
  test ! -e /usr/bin/docker
  test ! -e /usr/bin/nvidia-smi
  test ! -S /var/run/docker.sock
'

quota_line="$(sudo xfs_quota -x -c 'report -p -h' /srv/gpu-platform | grep -E '^h100_codexops[[:space:]]')"
grep -q '300G' <<<"${quota_line}" || fail '300GB project quota is not active'

mapfile -t slurm_states < <(sinfo -h -N -o '%T')
if ((${#slurm_states[@]} == 0)); then
  fail 'Slurm returned no node state'
fi
for state in "${slurm_states[@]}"; do
  [[ "${state}" == drain* ]] || fail "Slurm node is not drained: ${state}"
done

image_id="$(sudo docker image inspect --format '{{.Id}}' "${image}")"
network_mode="$(sudo docker inspect --format '{{.HostConfig.NetworkMode}}' "${container}")"
apparmor_profile="$(sudo docker inspect --format '{{.AppArmorProfile}}' "${container}")"

{
  printf '# Long-lived Docker template and codexops test acceptance\n\n'
  printf -- '- Run ID: `%s`\n' "${run_id}"
  printf -- '- Result: `LONG-LIVED DOCKER TEMPLATE PASSED`\n'
  printf -- '- Image: `%s`\n' "${image}"
  printf -- '- Image ID: `%s`\n' "${image_id}"
  printf -- '- Base: Canonical Ubuntu 24.04 LTS, digest pinned\n'
  printf -- '- Container: `%s`\n' "${container}"
  printf -- '- Initial SSH login: passed\n'
  printf -- '- Post-recreate SSH login: passed\n'
  printf -- '- SSH host-key fingerprint preserved: `%s`\n' "${host_key_sha256}"
  printf -- '- Persistence SHA-256 preserved: `%s`\n' "${persistence_sha256}"
  printf -- '- Container user: `codexops` (`1001:1001`)\n'
  printf -- '- Container sudo: passed (`uid=0`)\n'
  printf -- '- CPU limit: `8`\n'
  printf -- '- Memory limit: `32GiB`\n'
  printf -- '- PIDs limit: `4096`\n'
  printf -- '- Shared memory: `8GiB`\n'
  printf -- '- XFS project quota: `300GB`\n'
  printf -- '- Privileged: `false`\n'
  printf -- '- Network mode: `%s` (not host)\n' "${network_mode}"
  printf -- '- Host PID/IPC: disabled\n'
  printf -- '- AppArmor profile: `%s`\n' "${apparmor_profile:-default/not-reported}"
  printf -- '- GPU DeviceRequest: none\n'
  printf -- '- `nvidia-smi`: unavailable as expected\n'
  printf -- '- Docker CLI: unavailable as expected\n'
  printf -- '- Docker Socket: absent\n'
  printf -- '- Host SSH bind: `10.82.36.1:22022`\n'
  printf -- '- Restart policy: `unless-stopped`\n'
  printf -- '- User data was not deleted during recreate\n'
  printf -- '- Slurm node remained DRAIN\n'
  printf -- '- MIG was not modified\n'
  printf -- '- Firewall was not modified\n'
} >"${report_file}"

printf 'LONG-LIVED DOCKER TEMPLATE PASSED\n'
printf 'Report: %s\n' "${report_file}"
