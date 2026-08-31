#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

readonly portal_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly repository_root="$(cd -- "${portal_root}/.." && pwd -P)"
readonly image=python:3.14.4-bookworm
readonly network=h100-cli-ingress-rehearsal
readonly unrelated_network=h100-cli-ingress-unrelated
readonly ingress_container=h100-cli-ingress-rehearsal-proxy
readonly dev_container=h100-cli-ingress-rehearsal-dev
readonly unrelated_container=h100-cli-ingress-rehearsal-unrelated
readonly rehearsal_subnet=172.22.240.0/28
readonly unrelated_subnet=172.22.241.0/28
runtime="$(mktemp -d /tmp/h100-cli-ingress-rehearsal.XXXXXX)"

cleanup() {
  docker rm --force \
    "${unrelated_container}" "${dev_container}" "${ingress_container}" >/dev/null 2>&1 || true
  docker network rm "${unrelated_network}" "${network}" >/dev/null 2>&1 || true
  case "${runtime}" in
    /tmp/h100-cli-ingress-rehearsal.*) rm -rf -- "${runtime}" ;;
  esac
}
trap cleanup EXIT

docker image inspect "${image}" >/dev/null
! docker container inspect "${ingress_container}" >/dev/null 2>&1
! docker container inspect "${dev_container}" >/dev/null 2>&1
! docker network inspect "${network}" >/dev/null 2>&1

docker network create --internal --subnet "${rehearsal_subnet}" "${network}" >/dev/null
docker network create --internal --subnet "${unrelated_subnet}" "${unrelated_network}" >/dev/null
docker run --detach --pull never \
  --name "${ingress_container}" \
  --network "${network}" \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --volume "${repository_root}:/candidate:ro" \
  --volume "${runtime}:/rehearsal" \
  "${image}" sleep infinity >/dev/null

ingress_ip="$(docker inspect --format "{{with index .NetworkSettings.Networks \"${network}\"}}{{.IPAddress}}{{end}}" "${ingress_container}")"
subnet="$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' "${network}")"
bridge="br-$(docker network inspect --format '{{.Id}}' "${network}" | cut -c1-12)"
cat >"${runtime}/ingress.json" <<EOF
{"version":1,"service":"H100 Portal","api_compatibility":"h100.cli.v1","listen_port":18082,"upstream":"http://127.0.0.1:18081","listeners":[{"network":"${network}","bridge":"${bridge}","subnet":"${subnet}","gateway":"${ingress_ip}"}],"users":{"cli-user":"${network}"}}
EOF
cat >"${runtime}/cli.json" <<EOF
{"version":1,"portal_url":"http://${ingress_ip}:18082/api/v1","service":"H100 Portal","api_compatibility":"h100.cli.v1"}
EOF
chmod 0444 "${runtime}/ingress.json" "${runtime}/cli.json"
install -d -m 0777 "${runtime}/workspace" "${runtime}/home"
printf 'set -euo pipefail\nprintf "rehearsal\\n"\n' >"${runtime}/workspace/rehearsal.sh"
chmod 0644 "${runtime}/workspace/rehearsal.sh"

docker exec --detach "${ingress_container}" \
  python3 /candidate/portal/tests/fixtures/cli_ingress_rehearsal_api.py
docker exec --detach "${ingress_container}" \
  python3 /candidate/portal/deploy/scripts/cli_ingress_proxy.py \
    --config /rehearsal/ingress.json --testing-allow-untrusted-config

for _attempt in $(seq 1 50); do
  if docker exec "${ingress_container}" python3 -c \
    "import urllib.request; urllib.request.urlopen('http://${ingress_ip}:18082/api/v1/cli/identity', timeout=1).read()" \
    >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done
docker exec "${ingress_container}" python3 -c \
  "import urllib.request; urllib.request.urlopen('http://${ingress_ip}:18082/api/v1/cli/identity', timeout=2).read()"

docker run --detach --pull never \
  --name "${dev_container}" \
  --network "${network}" \
  --user 1000:1000 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --env HOME=/home/cli-user \
  --volume "${portal_root}/apps/cli/h100:/usr/local/bin/h100:ro" \
  --volume "${runtime}/cli.json:/etc/h100/cli.json:ro" \
  --volume "${runtime}/workspace:/workspace" \
  --volume "${runtime}/home:/home/cli-user" \
  "${image}" sleep infinity >/dev/null
started_before="$(docker inspect --format '{{.State.StartedAt}}' "${dev_container}")"
restart_before="$(docker inspect --format '{{.RestartCount}}' "${dev_container}")"

docker exec "${dev_container}" h100 --version | grep -Fx 'h100 1.0.0'
token='h100_cli_RRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRRR'
printf '%s\n' "${token}" | docker exec --interactive "${dev_container}" \
  h100 auth login --token-stdin --json >"${runtime}/login.json"
! grep -Fq "${token}" "${runtime}/login.json"
docker exec "${dev_container}" h100 auth status --json | grep -Fq '"authenticated":true'
docker exec "${dev_container}" h100 job submit /workspace/rehearsal.sh --json | grep -Fq '"command":"job.submit"'
docker exec "${dev_container}" h100 job list --json | grep -Fq '"command":"job.list"'
docker exec "${dev_container}" h100 job status 149 --json | grep -Fq '"command":"job.status"'
docker exec "${dev_container}" h100 job logs 149 --json | grep -Fq '"command":"job.logs"'
docker exec "${dev_container}" h100 job logs 149 --follow | grep -Fq 'container-origin ingress rehearsal'
docker exec "${dev_container}" h100 job cancel 149 --json | grep -Fq '"command":"job.cancel"'
docker exec "${dev_container}" h100 job list --json | grep -Fq '"jobs"'

docker exec "${dev_container}" python3 -c \
  "import urllib.error,urllib.request; u='http://${ingress_ip}:18082/api/v1/admin/users'; r=urllib.request.Request(u,headers={'Authorization':'Bearer ${token}'}); exec('try:\n urllib.request.urlopen(r,timeout=2)\n raise SystemExit(1)\nexcept urllib.error.HTTPError as e:\n raise SystemExit(0 if e.code==404 else 1)')"

docker run --detach --pull never \
  --name "${unrelated_container}" \
  --network "${unrelated_network}" \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  "${image}" sleep infinity >/dev/null
docker exec "${unrelated_container}" python3 -c \
  "import socket; s=socket.socket(); s.settimeout(2); raise SystemExit(0 if s.connect_ex(('${ingress_ip}',18082)) != 0 else 1)"

docker inspect "${dev_container}" | jq -e '
  .[0].HostConfig.Privileged == false
  and .[0].HostConfig.NetworkMode != "host"
  and .[0].HostConfig.PidMode != "host"
  and .[0].HostConfig.IpcMode != "host"
  and ((.[0].HostConfig.CapAdd // []) | length == 0)
  and ((.[0].Mounts // []) | map(.Destination) | index("/var/run/docker.sock") | not)
  and ((.[0].Mounts // []) | map(.Destination) | index("/run/h100-portal/worker.sock") | not)
' >/dev/null
[[ "$(docker inspect --format '{{.State.StartedAt}}' "${dev_container}")" == "${started_before}" ]]
[[ "$(docker inspect --format '{{.RestartCount}}' "${dev_container}")" == "${restart_before}" ]]
docker exec "${dev_container}" h100 auth logout --json | grep -Fq '"credential_removed":true'

printf 'PRIVATE CLI INGRESS REHEARSAL PASS network=%s subnet=%s ingress=%s:18082 restarts=0\n' \
  "${network}" "${subnet}" "${ingress_ip}"
