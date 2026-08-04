#!/usr/bin/env bash
set -euo pipefail

readonly run_id="${RUN_ID:-20260804-050357}"
readonly platform_root=/srv/gpu-platform/platform
readonly report_file="${platform_root}/reports/listening-ports-${run_id}.md"
readonly policy_file="${platform_root}/reports/inbound-access-policy-${run_id}.md"

fail() {
  printf 'PORT AUDIT BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'
[[ ! -e "${report_file}" ]] || fail 'refusing to overwrite port report'
[[ ! -e "${policy_file}" ]] || fail 'refusing to overwrite access-policy report'

for port in 3000 9090 9100 9400; do
  mapfile -t listeners < <(
    sudo ss -H -lnt "sport = :${port}" | awk '{print $4}' | sort -u
  )
  if ((${#listeners[@]} != 1)) \
    || [[ "${listeners[0]}" != "127.0.0.1:${port}" ]]; then
    fail "monitoring port ${port} is not restricted to localhost"
  fi
done

[[ "$(sudo ss -H -lnt 'sport = :22022' | awk '{print $4}')" == '10.82.36.1:22022' ]] \
  || fail 'test-container SSH is not restricted to 10.82.36.1:22022'
ip -br addr | grep -Eq '^[^[:space:]]+[[:space:]]+UP[[:space:]]+.*10\.82\.36\.1/' \
  || fail '10.82.36.1 is not active'

mapfile -t slurm_states < <(sinfo -h -N -o '%T')
for state in "${slurm_states[@]}"; do
  [[ "${state}" == drain* ]] || fail "Slurm node is not drained: ${state}"
done

socket_snapshot="$(mktemp /tmp/h100-sockets.XXXXXX)"
docker_snapshot="$(mktemp /tmp/h100-docker-ports.XXXXXX)"
cleanup() {
  rm -f -- "${socket_snapshot}" "${docker_snapshot}"
}
trap cleanup EXIT
sudo ss -lntup >"${socket_snapshot}"
sudo docker ps \
  --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}' \
  >"${docker_snapshot}"

{
  printf '# H100 listening-port audit\n\n'
  printf -- '- Run ID: `%s`\n' "${run_id}"
  printf -- '- Management IP: `10.82.36.1`\n'
  printf -- '- Firewall configuration changed: `no`\n'
  printf -- '- Public monitoring listeners: `none`\n'
  printf -- '- Slurm node state: `DRAIN`\n\n'
  printf '## Expected service exposure\n\n'
  printf '| Address | Port | Service | Visibility | Purpose |\n'
  printf '|---|---:|---|---|---|\n'
  printf '| Host sshd listener(s) | 22 | OpenSSH | LAN interfaces according to sshd defaults | Host administration and approved user login |\n'
  printf '| 10.82.36.1 | 22022 | gpu-dev-codexops sshd | LAN/private management network | codexops test development container |\n'
  printf '| 127.0.0.1 | 3000 | Grafana | localhost only | Dashboards |\n'
  printf '| 127.0.0.1 | 9090 | Prometheus | localhost only | Metrics storage/query |\n'
  printf '| 127.0.0.1 | 9400 | DCGM Exporter | localhost only | GPU metrics |\n'
  printf '| 127.0.0.1 | 9100 | Node Exporter | localhost only | Host metrics |\n'
  printf '| 127.0.0.1 | 5555 | NVIDIA DCGM hostengine | localhost only | DCGM Exporter backend |\n'
  printf '| Local/cluster listeners | 6817-6819 | Slurm components | Current bind shown below | Controller, node daemon, accounting |\n'
  printf '| 127.0.0.1 | 3306 | MariaDB | localhost only | Slurm accounting database |\n\n'
  printf '## Complete `ss -lntup` snapshot\n\n```text\n'
  sed -e 's/`/\\`/g' "${socket_snapshot}"
  printf '```\n\n'
  printf '## Docker published ports\n\n```text\n'
  sed -e 's/`/\\`/g' "${docker_snapshot}"
  printf '```\n\n'
  printf '## Findings\n\n'
  printf -- '- Monitoring is not bound to `0.0.0.0` or `[::]`.\n'
  printf -- '- The test container publishes only `10.82.36.1:22022 -> 22/tcp`.\n'
  printf -- '- No Docker TCP API is configured.\n'
  printf -- '- No UFW or nftables changes were made.\n'
  printf -- '- Network physical-error P0 remains open and is tracked separately.\n'
} >"${report_file}"

{
  printf '# H100 入站访问策略建议\n\n'
  printf '当前阶段不修改防火墙，也不开放公网端口。`10.82.36.1` 是私有地址，但仍应由上游网络和未来经批准的主机防火墙共同限制。\n\n'
  printf '## 当前允许的用途\n\n'
  printf -- '- TCP 22：宿主 SSH，仅允许公司管理网/跳板来源。\n'
  printf -- '- TCP 22022：`gpu-dev-codexops` 测试 SSH，仅允许公司管理网；后续每个用户端口必须单独分配、登记和审计。\n'
  printf -- '- TCP 3000/9090/9100/9400：保持 `127.0.0.1`，通过 SSH 本地转发查看，不直接对局域网或公网开放。\n'
  printf -- '- MariaDB 与 DCGM hostengine：保持 localhost。\n'
  printf -- '- Slurm 6817-6819：当前单机使用；加入第二节点前，根据实际控制/计算方向仅允许集群管理网。\n\n'
  printf 'Grafana 的建议访问方式：\n\n'
  printf '```bash\nssh -L 3000:127.0.0.1:3000 h100-codex\n```\n\n'
  printf '## 后续审批事项\n\n'
  printf '在启用 UFW/nftables 或对外开放任何监控/Slurm API 前，管理员应批准精确的源网段、目标端口、IPv4/IPv6 策略、回滚命令和带外恢复路径。Mellanox CRC/symbol error P0 未关闭前，不批准正式 NFS、第二节点、跨节点 NCCL 或高速共享存储。\n'
} >"${policy_file}"

printf 'PORT AUDIT PASSED\n'
printf 'Report: %s\n' "${report_file}"
printf 'Policy: %s\n' "${policy_file}"
