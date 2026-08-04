#!/usr/bin/env bash
set -euo pipefail

readonly run_id="${RUN_ID:-20260804-050357}"
readonly platform_root=/srv/gpu-platform/platform
readonly stamp="$(date +%Y%m%d-%H%M%S)"
readonly report_file="${platform_root}/reports/final-acceptance-raw-${run_id}-${stamp}.txt"

fail() {
  printf 'FINAL ACCEPTANCE AUDIT BLOCKED: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -un)" == codexops ]] || fail 'must run as codexops'
sudo -n true || fail 'passwordless sudo is unavailable'
[[ ! -e "${report_file}" ]] || fail 'refusing to overwrite final audit record'

exec 3>&1
exec >"${report_file}" 2>&1

failures=0

run() {
  local title=$1
  shift
  local rc
  printf '\n===== %s =====\n' "${title}"
  printf 'command:'
  printf ' %q' "$@"
  printf '\n'
  set +e
  "$@"
  rc=$?
  set -e
  printf '\nexit_status=%s\n' "${rc}"
  if ((rc != 0)); then
    failures=$((failures + 1))
  fi
}

slurm_container_help() {
  srun --help | grep -i container
}

prometheus_ready() {
  curl -fsS --max-time 10 http://127.0.0.1:9090/-/ready
}

grafana_health() {
  curl -fsS --max-time 10 http://127.0.0.1:3000/api/health
}

dcgm_metrics_probe() {
  curl -fsS --max-time 15 http://127.0.0.1:9400/metrics >/dev/null
}

node_metrics_probe() {
  curl -fsS --max-time 15 http://127.0.0.1:9100/metrics >/dev/null
}

slurm_config_extract() {
  sudo grep -E '^(ClusterName|SlurmctldHost|SelectType|SelectTypeParameters|GresTypes|ProctrackType|TaskPlugin|JobAcctGatherType|AccountingStorageType|AccountingStorageEnforce|AccountingStorageTRES|SchedulerType|PriorityType|ReturnToService|SlurmctldParameters|NodeName|PartitionName)=' /etc/slurm/slurm.conf
  sudo grep -E '^(CgroupPlugin|ConstrainCores|ConstrainRAMSpace|ConstrainSwapSpace|ConstrainDevices)=' /etc/slurm/cgroup.conf
  sudo grep -E '^AutoDetect=' /etc/slurm/gres.conf
}

docker_security_extract() {
  sudo docker inspect gpu-dev-codexops --format '{{json .HostConfig}}'
  sudo docker inspect gpu-dev-codexops --format '{{json .Mounts}}'
  sudo docker inspect gpu-dev-codexops --format '{{json .NetworkSettings.Ports}}'
}

firewall_inventory() {
  if command -v ufw >/dev/null 2>&1; then
    sudo ufw status verbose
  else
    printf 'ufw_not_installed\n'
  fi
  if command -v nft >/dev/null 2>&1; then
    sudo nft list ruleset
  else
    printf 'nft_not_installed\n'
  fi
}

run 'date' date -Is
run 'hostnamectl' hostnamectl
run 'os-release' cat /etc/os-release
run 'kernel' uname -a
run 'uptime' uptime
run 'cpu' lscpu
run 'memory' free -h
run 'mounts' findmnt
run 'filesystem-capacity' df -hT
run 'physical-volumes' sudo pvs
run 'volume-groups' sudo vgs
run 'logical-volumes' sudo lvs
run 'swap' swapon --show
run 'docker-xfs-mount' findmnt -no SOURCE,FSTYPE,OPTIONS /var/lib/docker
run 'platform-xfs-mount' findmnt -no SOURCE,FSTYPE,OPTIONS /srv/gpu-platform
run 'docker-xfs-info' sudo xfs_info /var/lib/docker
run 'platform-xfs-info' sudo xfs_info /srv/gpu-platform
run 'docker-project-quota-state' sudo xfs_quota -x -c state /var/lib/docker
run 'platform-project-quota-state' sudo xfs_quota -x -c state /srv/gpu-platform
run 'platform-project-quota-report' sudo xfs_quota -x -c report /srv/gpu-platform
run 'gpu-pcie-inventory' lspci -nn
run 'nvidia-smi' nvidia-smi
run 'nvidia-list' nvidia-smi -L
run 'nvidia-query' nvidia-smi --query-gpu=index,uuid,pci.bus_id,name,memory.total,temperature.gpu,power.draw,ecc.mode.current,persistence_mode,compute_mode,mig.mode.current --format=csv,noheader
run 'nvidia-topology' nvidia-smi topo -m
run 'mig-gpu-instance-profiles' nvidia-smi mig -lgip
run 'mig-compute-instance-profiles' nvidia-smi mig -lcip
run 'nvidia-driver-version' cat /proc/driver/nvidia/version
run 'nvidia-module' modinfo nvidia
run 'docker-version' sudo docker version
run 'docker-info' sudo docker info
run 'docker-compose-version' sudo docker compose version
run 'nvidia-container-toolkit' nvidia-ctk --version
run 'dcgm-discovery' dcgmi discovery -l
run 'slurm-node-list' sinfo -Nel
run 'slurm-node' scontrol show node
run 'slurm-queue' squeue
run 'slurm-accounting-jobs' sacct -X --starttime 2026-08-04 --format=JobID,JobName,Partition,Account,AllocTRES,State,ExitCode
run 'slurm-clusters' sacctmgr -nP show cluster
run 'slurm-accounts' sacctmgr -nP show account
run 'slurm-qos' sacctmgr -nP show qos
run 'slurm-associations' sacctmgr -nP show assoc
run 'slurm-config' slurm_config_extract
run 'enroot-version' enroot version
run 'srun-version' srun --version
run 'srun-container-help' slurm_container_help
run 'prometheus-ready' prometheus_ready
run 'grafana-health' grafana_health
run 'dcgm-exporter-metrics' dcgm_metrics_probe
run 'node-exporter-metrics' node_metrics_probe
run 'development-container-inspect' sudo docker inspect gpu-dev-codexops
run 'development-container-security' docker_security_extract
run 'docker-containers' sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}'
run 'failed-units' systemctl --failed --no-pager
run 'error-journal' sudo journalctl -p err -b --no-pager
run 'listeners' sudo ss -lntup
run 'firewall-inventory' firewall_inventory
run 'account-codexops' id codexops
run 'account-origin-al' id origin-al
run 'docker-group' getent group docker
run 'ssh-config-validation' sudo sshd -t
run 'fstab-validation' sudo findmnt --verify --verbose
run 'ssh-enabled' systemctl is-enabled ssh
run 'management-addresses' ip -br addr
run 'routes' ip route
run 'noninteractive-sudo' sudo -n true
run 'platform-git-status' git -C "${platform_root}" status --short --branch
run 'platform-git-commit' git -C "${platform_root}" rev-parse HEAD

printf '\n===== AUDIT RESULT =====\n'
printf 'command_failures=%s\n' "${failures}"
printf 'report=%s\n' "${report_file}"

printf 'FINAL_ACCEPTANCE_RAW_REPORT=%s\n' "${report_file}" >&3
printf 'FINAL_ACCEPTANCE_COMMAND_FAILURES=%s\n' "${failures}" >&3

if ((failures != 0)); then
  exit 2
fi
