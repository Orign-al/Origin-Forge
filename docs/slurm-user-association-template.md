# H100 普通 Pilot 用户 Slurm association 模板

账号、XFS project quota、长期容器和 Slurm association 分开实施，以便每一步独立审计和回滚。`h100-user-create` 不隐式修改 Slurm accounting 数据库。

## 普通用户策略

- Cluster：`h100`
- Account：`company`
- FairShare：`1`
- Allowed QOS：`general,core`
- Default QOS：`general`
- `general` 最多 1 张 GPU；`core` 最多 2 张 GPU
- 不授予 `admin` QOS 或 Slurm Administrator 权限

## 管理员执行模板

先把 `pilot_user` 设置为已审批、已由 `h100-user-create` 创建的真实用户名。不要直接复制尖括号占位符。

```bash
set -euo pipefail

pilot_user='REPLACE_WITH_APPROVED_USERNAME'
[[ "${pilot_user}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]
getent passwd "${pilot_user}" >/dev/null
test -d "/srv/gpu-platform/users/${pilot_user}/workspace"

test -z "$(sacctmgr -nP show assoc where cluster=h100 user="${pilot_user}" format=User)"

sudo sacctmgr -i add user "${pilot_user}" \
  Cluster=h100 \
  Account=company \
  Fairshare=1 \
  QOS=general,core \
  DefaultQOS=general

sacctmgr -nP show assoc \
  where cluster=h100 user="${pilot_user}" \
  format=Cluster,Account,User,Fairshare,DefaultQOS,QOS
```

实施前应备份 Slurm accounting 数据库，并在平台报告目录记录命令时间、执行人、用户名和脱敏后的 association 输出。不得把数据库密码写入命令、日志或 Git。

## 验证清单

- association 只位于 `company`；
- DefaultQOS 为 `general`；
- Allowed QOS 恰为 `general,core`；
- 用户不是 Slurm Administrator；
- 用户不属于宿主 `sudo`、`docker`、`gpu-platform-admin` 组；
- Slurm 节点状态不因创建 association 自动变化；
- 未经 Pilot RESUME 单独批准，不提交实际作业。

删除或变更 association 属于独立数据库变更，必须先备份并取得管理员批准；本模板不包含自动删除命令。
