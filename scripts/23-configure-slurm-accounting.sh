#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -eq 0 ]]; then
    printf '%s\n' 'Run this script as codexops; it uses sudo for Slurm accounting administration.' >&2
    exit 2
fi

run_id=${1:?usage: 23-configure-slurm-accounting.sh RUN_ID}
if [[ ! ${run_id} =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    printf 'Invalid RUN_ID: %s\n' "${run_id}" >&2
    exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
platform_dir=$(dirname -- "${script_dir}")
record_dir=${platform_dir}/reports/slurm-accounting-${run_id}.d
report_file=${platform_dir}/reports/slurm-accounting-${run_id}.md
backup_file=${platform_dir}/backups/slurm-acct-db-before-associations-${run_id}.sql
hostname_expected=sagsh100server

if [[ ! -f ${platform_dir}/reports/slurm-base-${run_id}.md ]]; then
    printf '%s\n' 'The passed Slurm base report is missing.' >&2
    exit 1
fi
if [[ -e ${record_dir} || -e ${report_file} || -e ${backup_file} ]]; then
    printf '%s\n' 'Refusing to overwrite an accounting record, report, or database backup.' >&2
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
node_state=$(sinfo -h -n "${hostname_expected}" -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf 'Node is not drained before accounting changes: %s\n' "${node_state}" >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'Jobs exist unexpectedly before accounting bootstrap.' >&2
    exit 1
fi

mapfile -t clusters < <(sudo sacctmgr -nP show cluster format=Cluster)
if (( ${#clusters[@]} != 1 )) || [[ ${clusters[0]} != h100 ]]; then
    printf '%s\n' 'The accounting database does not contain exactly one h100 cluster.' >&2
    exit 1
fi

for account in company platform-admin; do
    count=$(sudo sacctmgr -nP show account where name="${account}" format=Account \
        | awk -v name="${account}" '$0 == name { count++ } END { print count + 0 }')
    if [[ ${count} != 0 ]]; then
        printf 'Account already exists unexpectedly: %s\n' "${account}" >&2
        exit 1
    fi
done
for qos in general core admin; do
    count=$(sudo sacctmgr -nP show qos where name="${qos}" format=Name \
        | awk -v name="${qos}" '$0 == name { count++ } END { print count + 0 }')
    if [[ ${count} != 0 ]]; then
        printf 'QOS already exists unexpectedly: %s\n' "${qos}" >&2
        exit 1
    fi
done
assoc_count=$(sudo sacctmgr -nP show assoc where user=codexops cluster=h100 \
    format=User,Account | awk -F'|' '$1 == "codexops" { count++ } END { print count + 0 }')
if [[ ${assoc_count} != 0 ]]; then
    printf '%s\n' 'A codexops association already exists unexpectedly.' >&2
    exit 1
fi

sudo mariadb-dump --single-transaction --quick slurm_acct_db >"${backup_file}"
sudo chown root:gpu-platform-admin "${backup_file}"
sudo chmod 0640 "${backup_file}"

sudo sacctmgr -i add qos general MaxTRESPU=gres/gpu=1 \
    >"${record_dir}/add-qos-general.txt" 2>&1
sudo sacctmgr -i add qos core MaxTRESPU=gres/gpu=2 \
    >"${record_dir}/add-qos-core.txt" 2>&1
sudo sacctmgr -i add qos admin MaxTRESPU=gres/gpu=4 \
    >"${record_dir}/add-qos-admin.txt" 2>&1

sudo sacctmgr -i add account company Cluster=h100 \
    Description='Company users' Organization=company Fairshare=1 \
    QOS=general,core DefaultQOS=general \
    >"${record_dir}/add-account-company.txt" 2>&1
sudo sacctmgr -i add account platform-admin Cluster=h100 Parent=company \
    Description='Platform administrators' Organization=company Fairshare=1 \
    QOS=general,core,admin DefaultQOS=admin \
    >"${record_dir}/add-account-platform-admin.txt" 2>&1

sudo sacctmgr -i add user codexops Cluster=h100 Account=platform-admin \
    DefaultAccount=platform-admin AdminLevel=Admin Fairshare=1 \
    QOS=general,core,admin DefaultQOS=admin \
    >"${record_dir}/add-user-codexops.txt" 2>&1

sudo sacctmgr -nP show cluster format=Cluster >"${record_dir}/clusters.txt"
sudo sacctmgr -nP show account format=Account,ParentName,Organization,DefaultQOS,QOS \
    >"${record_dir}/accounts.txt"
sudo sacctmgr -nP show assoc where cluster=h100 \
    format=Cluster,Account,User,ParentName,Fairshare,DefaultQOS,QOS \
    >"${record_dir}/account-associations.txt"
sudo sacctmgr -nP show qos format=Name,MaxTRESPU >"${record_dir}/qos.txt"
sudo sacctmgr -nP show user where name=codexops WithAssoc \
    format=User,DefaultAccount,AdminLevel,Cluster,Account,DefaultQOS,QOS \
    >"${record_dir}/codexops-user.txt"
sudo sacctmgr -nP show assoc where user=codexops cluster=h100 \
    format=Cluster,Account,User,Fairshare,DefaultQOS,QOS \
    >"${record_dir}/codexops-association.txt"

for expected in 'general|gres/gpu=1' 'core|gres/gpu=2' 'admin|gres/gpu=4'; do
    if ! grep -Fxq "${expected}" "${record_dir}/qos.txt"; then
        printf 'QOS validation failed: %s\n' "${expected}" >&2
        exit 1
    fi
done
if ! awk -F'|' '$2 == "company" && $3 == "" && $4 == "root" && $6 == "general" { found = 1 } END { exit !found }' \
    "${record_dir}/account-associations.txt"; then
    printf '%s\n' 'The company account hierarchy/default QOS is invalid.' >&2
    exit 1
fi
if ! awk -F'|' '$2 == "platform-admin" && $3 == "" && $4 == "company" && $6 == "admin" { found = 1 } END { exit !found }' \
    "${record_dir}/account-associations.txt"; then
    printf '%s\n' 'The platform-admin account hierarchy/default QOS is invalid.' >&2
    exit 1
fi
if ! awk -F'|' '
    $1 == "codexops" && $2 == "platform-admin" && $3 == "Administrator" &&
    $4 == "h100" && $5 == "platform-admin" && $6 == "admin" { found = 1 }
    END { exit !found }
' "${record_dir}/codexops-user.txt"; then
    printf '%s\n' 'The codexops administrator association is invalid.' >&2
    exit 1
fi

node_state=$(sinfo -h -n "${hostname_expected}" -o '%T')
if [[ ${node_state} != drain* && ${node_state} != drained* ]]; then
    printf 'Node left DRAIN during accounting bootstrap: %s\n' "${node_state}" >&2
    exit 1
fi
if [[ $(squeue -h | wc -l) != 0 ]]; then
    printf '%s\n' 'A job appeared unexpectedly during accounting bootstrap.' >&2
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
- Account company: created under root, default QOS general
- Account platform-admin: created under company, default QOS admin
- User association: codexops / platform-admin
- Admin level: Administrator (configured with the 25.11.7 canonical input value Admin)
- QOS general: MaxTRESPU=gres/gpu=1
- QOS core: MaxTRESPU=gres/gpu=2
- QOS admin: MaxTRESPU=gres/gpu=4
- FairShare: account/user shares set and priority/multifactor enabled
- Accounting backup: ${backup_file}
- Jobs present: no
- Node state: ${node_state}

Status: SLURM BASE PASSED
EOF

printf '%s\n' 'SLURM BASE PASSED'
printf '%s\n' 'SLURM NODE REMAINS DRAINED'
