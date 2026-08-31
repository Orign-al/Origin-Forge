#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

readonly portal_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly repository_root="$(cd -- "${portal_root}/.." && pwd -P)"
readonly image=python:3.14.4-bookworm
readonly running_user=origin-al
readonly stopped_user=nobody
readonly running_container=gpu-dev-origin-al
readonly stopped_container=gpu-dev-nobody
readonly running_network=h100-dev-origin-al_default
readonly stopped_network=h100-dev-nobody_default
readonly running_subnet=172.22.240.0/28
readonly stopped_subnet=172.22.241.0/28

if [[ "${1:-}" == --inner ]]; then
  runtime=$2
  mount --bind "${runtime}/local-lib" /usr/local/lib
  mount --bind "${runtime}/local-sbin" /usr/local/sbin
  mount --bind "${runtime}/systemctl" /usr/bin/systemctl
  mount --bind "${runtime}/run" /run/h100-cli-ingress
  /usr/local/sbin/h100-cli-rollout --all
  exit 0
fi

runtime="$(mktemp -d /tmp/h100-cli-hot-config.XXXXXX)"
created_run_dir=0
cleanup() {
  docker rm --force "${running_container}" "${stopped_container}" >/dev/null 2>&1 || true
  docker network rm "${running_network}" "${stopped_network}" >/dev/null 2>&1 || true
  if [[ "${created_run_dir}" == 1 ]]; then
    rmdir /run/h100-cli-ingress >/dev/null 2>&1 || true
  fi
  case "${runtime}" in
    /tmp/h100-cli-hot-config.*) rm -rf -- "${runtime}" ;;
  esac
}
trap cleanup EXIT

[[ "$(docker ps --all --quiet --filter label=h100.dev.user | wc -l)" == 0 ]] \
  || { printf 'unrelated h100.dev.user containers exist on rehearsal host\n' >&2; exit 1; }
docker image inspect "${image}" >/dev/null
! docker container inspect "${running_container}" >/dev/null 2>&1
! docker container inspect "${stopped_container}" >/dev/null 2>&1
! docker network inspect "${running_network}" >/dev/null 2>&1
! docker network inspect "${stopped_network}" >/dev/null 2>&1

install -d -m 0755 \
  "${runtime}/local-lib/h100-platform" \
  "${runtime}/local-sbin" \
  "${runtime}/run"
install -m 0640 "${repository_root}/scripts/h100-platform-common.sh" \
  "${runtime}/local-lib/h100-platform/h100-platform-common.sh"
install -m 0555 "${portal_root}/apps/cli/h100" \
  "${runtime}/local-lib/h100-platform/h100-cli"
install -m 0550 "${portal_root}/deploy/scripts/cli_ingress_config.py" \
  "${runtime}/local-sbin/h100-cli-ingress-config"
install -m 0750 "${repository_root}/scripts/h100-cli-ingress-reconcile" \
  "${runtime}/local-sbin/h100-cli-ingress-reconcile"
install -m 0750 "${repository_root}/scripts/h100-cli-rollout" \
  "${runtime}/local-sbin/h100-cli-rollout"
cat >"${runtime}/systemctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod 0755 "${runtime}/systemctl"
if [[ ! -d /run/h100-cli-ingress ]]; then
  install -d -o root -g root -m 0755 /run/h100-cli-ingress
  created_run_dir=1
fi

docker network create --subnet "${running_subnet}" "${running_network}" >/dev/null
docker network create --subnet "${stopped_subnet}" "${stopped_network}" >/dev/null
docker run --detach --pull never \
  --name "${running_container}" \
  --network "${running_network}" \
  --label "h100.dev.user=${running_user}" \
  --label "h100.dev.uid=$(id -u "${running_user}")" \
  --label "h100.dev.gid=$(id -g "${running_user}")" \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  "${image}" sleep infinity >/dev/null
docker create \
  --name "${stopped_container}" \
  --network "${stopped_network}" \
  --label "h100.dev.user=${stopped_user}" \
  --label "h100.dev.uid=$(id -u "${stopped_user}")" \
  --label "h100.dev.gid=$(id -g "${stopped_user}")" \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  "${image}" sleep infinity >/dev/null

started_before="$(docker inspect --format '{{.State.StartedAt}}' "${running_container}")"
restart_before="$(docker inspect --format '{{.RestartCount}}' "${running_container}")"
unshare --mount --propagation private "$0" --inner "${runtime}"

docker exec "${running_container}" test -x /usr/local/bin/h100
docker exec "${running_container}" test -r /etc/h100/cli.json
[[ "$(docker exec "${running_container}" stat -c '%U:%G:%a' /usr/local/bin/h100)" == root:root:555 ]]
[[ "$(docker exec "${running_container}" stat -c '%U:%G:%a' /etc/h100/cli.json)" == root:root:444 ]]
docker exec "${running_container}" h100 --version | grep -Fx 'h100 1.0.0'
docker exec "${running_container}" python3 -c \
  "import json; p=json.load(open('/etc/h100/cli.json')); assert p['portal_url'].endswith(':18082/api/v1'); assert p['service']=='H100 Portal'"

docker cp "${stopped_container}:/usr/local/bin/h100" "${runtime}/stopped-h100"
docker cp "${stopped_container}:/etc/h100/cli.json" "${runtime}/stopped-cli.json"
[[ "$(stat -c '%a' "${runtime}/stopped-h100")" == 555 ]]
[[ "$(stat -c '%a' "${runtime}/stopped-cli.json")" == 444 ]]
[[ "$(sha256sum "${runtime}/stopped-h100" | awk '{print $1}')" \
  == "$(sha256sum "${portal_root}/apps/cli/h100" | awk '{print $1}')" ]]
python3 -c \
  "import json; p=json.load(open('${runtime}/stopped-cli.json')); assert p['portal_url'].endswith(':18082/api/v1')"

[[ "$(docker inspect --format '{{.State.Status}}' "${stopped_container}")" == created ]]
[[ "$(docker inspect --format '{{.State.StartedAt}}' "${running_container}")" == "${started_before}" ]]
[[ "$(docker inspect --format '{{.RestartCount}}' "${running_container}")" == "${restart_before}" ]]
printf 'HOT CLI CONFIG ROLLOUT PASS running=1 stopped=1 restarts=0\n'
