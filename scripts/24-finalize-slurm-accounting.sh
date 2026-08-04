#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for accounting validation only.' >&2
    exit 2
fi

run_id=${1:?usage: 24-finalize-slurm-accounting.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
record_dir=${platform_dir}/reports/slurm-accounting-finalize-${run_id}.d
report_file=${platform_dir}/reports/slurm-accounting-${run_id}.md
backup_file=${platform_dir}/backups/slurm-acct-db-before-associations-${run_id}.sql
hostname_expected=sagsh100server

if [[ ! -d ${platform_dir}/reports/slurm-accounting-${run_id}.d || ! -f ${backup_file} ]]; then
    printf '%s\n' 'The accounting transaction record or pre-change backup is missing.' >&2
    exit 1
fi
if [[ -e ${record_dir} || -e ${report_file} ]]; then
    printf '%s\n' 'Refusing to overwrite an accounting finalization record or report.' >&2
    exit 1
fi

sudo -n true
install -d -m 2770 "${record_dir}"

for unit in munge mariadb slurmdbd slurmctld slurmd; do
    if ! systemctl is-active --quiet "${unit}"; then
        printf 'Required service is not active: %s\n' "${unit}" >&2
        exit 1
    fi
done

sudo sacctmgr -nP show cluster format=Cluster >"${record_dir}/clusters.txt"
sudo sacctmgr -nP show account format=Account,Organization >"${record_dir}/accounts.txt"
sudo sacctmgr -nP show qos format=Name,MaxTRESPU >"${record_dir}/qos.txt"
sudo sacctmgr -nP show user where name=codexops WithAssoc \
    format=User,DefaultAccount,AdminLevel,Cluster,Account,DefaultQOS,QOS \
    >"${record_dir}/codexops-user.txt"
sudo sacctmgr -nP show assoc where cluster=h100 \
    format=Cluster,Account,User,ParentName,Fairshare,DefaultQOS,QOS \
    >"${record_dir}/associations.txt"

if [[ $(grep -Fxc 'h100' "${record_dir}/clusters.txt") != 1 ]]; then
    printf '%s\n' 'Cluster validation failed.' >&2
    exit 1
fi
for expected in 'general|gres/gpu=1' 'core|gres/gpu=2' 'admin|gres/gpu=4'; do
    if ! grep -Fxq "${expected}" "${record_dir}/qos.txt"; then
        printf 'QOS validation failed: %s\n' "${expected}" >&2
        exit 1
    fi
done
if ! awk -F'|' '
    $1 == "h100" && $2 == "company" && $3 == "" && $4 == "root" &&
    $5 == 1 && $6 == "general" && $7 == "core,general" { found = 1 }
    END { exit !found }
' "${record_dir}/associations.txt"; then
    printf '%s\n' 'The company association hierarchy or QOS policy is invalid.' >&2
    exit 1
fi
if ! awk -F'|' '
    $1 == "h100" && $2 == "platform-admin" && $3 == "" && $4 == "company" &&
    $5 == 1 && $6 == "admin" && $7 == "admin,core,general" { found = 1 }
    END { exit !found }
' "${record_dir}/associations.txt"; then
    printf '%s\n' 'The platform-admin association hierarchy or QOS policy is invalid.' >&2
    exit 1
fi
if ! awk -F'|' '
    $1 == "codexops" && $2 == "platform-admin" && $3 == "Administrator" &&
    $4 == "h100" && $5 == "platform-admin" && $6 == "admin" &&
    $7 == "admin,core,general" { found = 1 }
    END { exit !found }
' "${record_dir}/codexops-user.txt"; then
    printf '%s\n' 'The codexops administrator/default QOS record is invalid.' >&2
    exit 1
fi
if ! awk -F'|' '
    $1 == "h100" && $2 == "platform-admin" && $3 == "codexops" &&
    $5 == 1 && $6 == "admin" && $7 == "admin,core,general" { found = 1 }
    END { exit !found }
' "${record_dir}/associations.txt"; then
    printf '%s\n' 'The codexops association policy is invalid.' >&2
    exit 1
fi

node_state=$(sinfo -h -n "${hostname_expected}" -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf 'Node is not drained after accounting bootstrap: %s\n' "${node_state}" >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly after accounting bootstrap.' >&2
    exit 1
fi

sinfo -Nel >"${record_dir}/sinfo-Nel.txt"
scontrol show node "${hostname_expected}" >"${record_dir}/scontrol-show-node.txt"
squeue >"${record_dir}/squeue.txt"

cat >"${report_file}" <<EOF
# Slurm accounting and QOS

- Run ID: ${run_id}
- Cluster: h100
- Root account retained: yes
- Account company: parent root, FairShare 1, default QOS general
- Account platform-admin: parent company, FairShare 1, default QOS admin
- User association: codexops / platform-admin
- Admin level: Administrator
- codexops default account: platform-admin
- codexops allowed QOS: admin, core, general
- QOS general: MaxTRESPU=gres/gpu=1
- QOS core: MaxTRESPU=gres/gpu=2
- QOS admin: MaxTRESPU=gres/gpu=4
- Accounting backup: ${backup_file}
- Jobs present: no
- Node state: ${node_state}

Status: SLURM BASE PASSED
EOF

printf '%s\n' 'SLURM BASE PASSED'
printf '%s\n' 'SLURM NODE REMAINS DRAINED'
