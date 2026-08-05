# H100 受控单机 Pilot 管理员手册

状态：Pilot-1 提交入口 Gate 未通过，节点已重新 DRAIN。禁止创建 Pilot 用户或再次 RESUME，直到作业外 GPU 访问得到技术阻断并经管理员重新批准。

## 不变量

- MIG 保持 Disabled；不修改驱动、Kernel、BIOS/BMC、PCIe、GRES、Mellanox 参数、MTU、FEC、防火墙或监控公网监听。
- 不部署 NFS/SMB，不接第二节点，不做多节点 NCCL 或 RDMA/RoCE 生产验收。
- 最多 3 名 Pilot 用户；必须逐批批准，不能自动创建第 4 名。
- 长期 Docker 无 GPU、Docker/MUNGE socket、host network/PID/IPC 或 privileged。
- Docker Hub 延期；镜像只能来自本地、NGC、批准的 GHCR/Quay 或离线 archive，并记录 digest。
- 所有用户/容器变更使用既有 `h100-*` 管理脚本；这些脚本要求节点先 DRAIN。

## 当前阻断与最小整改

2026-08-05 实测表明，无特权 `nobody` 在 Slurm 作业外能枚举全部 4 张 GPU，并能打开 `0666` 的 NVIDIA 设备节点。`ConstrainDevices=yes` 只隔离 Slurm job cgroup，宿主普通登录会话不受其保护。

单独审批并实现以下一种方案后再复测：

1. 不授予计算节点通用 shell，建立逐用户认证、强制命令、禁转发、完整审计的 Slurm 提交网关。
2. 部署经 TLS、短期 per-user JWT、撤销、限流和审计验证的 `slurmrestd`。
3. 引入独立登录节点，计算节点启用 `pam_slurm_adopt` 与 `PrologFlags=contain`，拒绝无 allocation 会话。

验收必须同时证明作业外身份无法枚举/打开 GPU，1-GPU job 只见一张，2-GPU job 只见两张，作业退出后访问撤销。不要用 `chmod` 临时值、隐藏二进制、组策略或用户承诺代替技术控制。

## 日常状态检查

```bash
date --iso-8601=seconds
uptime
systemctl --failed
nvidia-smi
dcgmi discovery -l
sinfo -Nel
squeue -a
df -hT
sudo xfs_quota -x -c 'report -p -h' /srv/gpu-platform
sudo docker ps
curl -fsS http://127.0.0.1:9090/-/ready
curl -fsS http://127.0.0.1:9090/api/v1/alerts
curl -fsS http://127.0.0.1:9100/metrics | grep '^h100_mellanox_'
sudo journalctl -k --since yesterday --no-pager \
  | grep -Ei 'NVRM: Xid|PCIe Bus Error|AER:.*error|fallen off' || true
sacct -S yesterday \
  --format=JobID,JobName,User,Account,Partition,State,ExitCode,Elapsed,AllocTRES
```

同时检查长时间作业、失败作业、用户 quota、Docker/Enroot 容量、Prometheus targets/active alerts 和 Mellanox counter 增量。只生成本机报告；不自动发送邮件、Slack/Webhook，也不自动删除数据、镜像、cache 或日志。

容量阈值建议：70% warning、80% high、90% critical；文件系统只读、quota 异常或 `/srv/gpu-platform` 达危险阈值时 DRAIN。

## DRAIN 与恢复

立即 DRAIN：GPU 消失、`nvidia-smi`/DCGM 失败、新 Xid、GPU AER、fallen off bus、GRES 数量不符、cgroup 隔离失效、作业外 GPU 占用、未知 GPU 进程、Slurm 核心服务异常、文件系统只读、quota 异常、管理链路 link-down 影响服务、用户容器出现 privileged/Docker Socket、监控整体失效。

```bash
NODE_NAME=$(hostname -s)
sudo scontrol update NodeName="$NODE_NAME" State=DRAIN Reason="EXACT_REASON"
scontrol show node "$NODE_NAME"
```

Mellanox CRC/symbol 单独增长只告警记录；若同时出现 link-down、SSH 中断、作业网络失败、文件传输错误或服务不可达，则 DRAIN。

RESUME 前必须确认队列计划、GPU/DCGM/Xid/AER、GRES/cgroup、服务、quota、监控和提交入口 Gate，随后等待明确管理员批准：

```bash
sudo scontrol update NodeName="$NODE_NAME" State=RESUME
```

不得循环强制 RESUME；非 IDLE 时立即调查，必要时恢复 DRAIN。

## 用户名单 Gate

只接受最多 3 人的已批准 `inventory/pilot-users.yaml`。验证用户名、UID/GID、单条公钥格式与指纹、唯一 project ID、未占用且批准的 SSH 端口、现有 account/QOS、300GB quota 和无 secret。不得接收私钥、密码或自动猜测身份。

输出创建计划并等待原文批准：`允许创建清单中的 Pilot 用户`。当前直接 GPU 绕过未关闭时，即使有名单也不得执行。

## 创建用户、quota 和 association

在维护窗口先 DRAIN 并确认队列为空。使用既有脚本，不手工并行实现：

```bash
sudo h100-user-create USER PROJECT_ID /root-secure-path/USER.pub --confirm-create
sudo h100-quota-show USER
```

脚本创建禁用密码的宿主账号、私有组、authorized_keys、`/srv/gpu-platform/users/USER/{home,workspace,shared}` 和 300GB XFS project hard quota，不授予 sudo/docker/admin。公钥临时文件必须 root-only，完成后按安全政策处置，不能提交 Git。

随后用 `sacctmgr` 把用户关联到已批准 account 与 `general` QOS，并验证每用户 GPU 上限为 1；不得授予 admin/core 或 4 GPU。所有命令先用当前 Slurm 25.11 字段做只读确认，导出 association 作为回滚证据。

## 创建和管理长期容器

仅使用本地存在、身份标签匹配的固定镜像；脚本禁止自动拉取自定义镜像：

```bash
sudo h100-container-create USER SSH_PORT [PINNED_LOCAL_IMAGE]
sudo h100-container-status USER
sudo h100-container-stop USER
sudo h100-container-start USER
sudo h100-container-rebuild USER
```

创建后用 `docker inspect` 验证：无 GPU DeviceRequest/Devices、Privileged=false、非 host network/PID/IPC、8 CPU、32GiB、4096 PIDs、端口只绑定 `10.82.36.1`、只挂个人目录和持久 host-key 目录、无 Docker/MUNGE socket 或宿主敏感目录。用户本人用对应私钥验证两个入口；管理员不得用私钥冒充。

容器删除默认保留数据：

```bash
sudo h100-container-delete USER
```

带数据删除是不可逆的独立审批动作；必须先完成离职/保留决策和备份验证，不在日常清理中执行。

## 停用、离职与回滚

1. DRAIN 并阻止新作业；根据风险决定是否取消运行作业。
2. 锁定宿主账号并移走/禁用 authorized_keys；记录时间和批准人。
3. 停止长期容器，保留用户数据、Compose 和 SSH host key。
4. 删除或冻结 Slurm association/QOS 前导出当前状态。
5. 按公司保留策略决定本机数据归档、移交和最终删除；没有批准不得删除。
6. quota/QOS 调整前备份 `/etc/projects`、`/etc/projid` 和 association；验证差异、唯一性及回滚命令。

若 Pilot 变更失败，优先保持节点 DRAIN、禁用新增登录/作业并停止相关容器；恢复备份的非 secret 配置，验证 Git diff、服务、GPU、quota、监听和监控后再请求 RESUME。

## Pilot 上线验收

每名用户分别验证 CPU job、单 GPU Pyxis job、两 GPU 超额申请被 QOS 拒绝/保持 Pending 后取消、sacct Account/QOS 正确、GPU 释放、目录隔离、无 sudo/docker 高权组、容器无 GPU/敏感 socket。最终复核 4 GPU、MIG Disabled、DCGM Pass、无 Xid/AER、队列、systemd、监控、quota、监听端口及 Git secret 扫描。

只有提交入口技术 Gate 和上述验收全部通过，且管理员再次明确批准后，才可打印 `CONTROLLED SINGLE-NODE PILOT STARTED`。这不代表平台全面生产就绪。
