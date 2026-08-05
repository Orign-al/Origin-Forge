# H100 受控单机 Pilot 管理员手册

状态：Pilot-1C 已完成专属 `user-<UID>.slice` 瞬时验证并纠正 Pilot-1B 的设备编号假阴性；策略已按要求回滚为 `auto`，节点保持 DRAIN。禁止创建 Pilot 用户或 RESUME，等待 per-user 持久策略明确审批。

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

1. 单节点 Pilot 候选：只为每个获批 Pilot UID 部署独立的 `user-<UID>.slice DevicePolicy=closed`，并让用户创建脚本负责 drop-in、daemon-reload、验证、Guard 指标和逐 UID 回滚。Pilot-1C 只验证了 runtime 策略，持久化尚未获批。
2. 不授予计算节点通用 shell，建立逐用户认证、强制命令、禁转发、完整审计的 Slurm 提交网关。
3. 部署经 TLS、短期 per-user JWT、撤销、限流和审计验证的 `slurmrestd`。
4. 引入独立登录节点，计算节点启用 `pam_slurm_adopt` 与 `PrologFlags=contain`，拒绝无 allocation 会话。

验收必须同时证明作业外身份无法枚举/打开 GPU，1-GPU job 只见一张，2-GPU job 只见两张，作业退出后访问撤销。不要用 `chmod` 临时值、隐藏二进制、组策略或用户承诺代替技术控制。

### Pilot-1B 试验记录

本轮只在 DRAIN 节点做了 runtime 试验，没有写入持久 drop-in：

- systemd 259、cgroup v2 层级符合预期；SSH 在 `user.slice`，Slurm/Docker/DCGM 在 `system.slice`。
- transient `DevicePolicy=closed` canary、普通命令兼容性、新 SSH/PTY、作业外 `nvidia-smi`/设备拒绝、system.slice 管理员通道均通过。
- CPU Job 8 通过，作业 cgroup 为 `/system.slice/slurmstepd.scope/job_8/step_0/user/task_0`。
- 单 GPU Job 10 的 `nvidia-smi` 只见 UUID `GPU-c8377945-df2c-5761-8798-66385611808b`，但探针固定测试 `/dev/nvidia0`；该轮没有记录 UUID 对应 minor，因此当时的 allocated-device 结论不可证明。
- 已按失败路径 DRAIN，运行时策略恢复 `auto`，持久 `/etc/systemd/system/user.slice.d/50-h100-gpu-isolation.conf` 不存在；两个 transient rollback units 已停止。

Pilot-1C 已完成该调查并证明 `/dev/nvidia0` 是 Job 10 的 unallocated GPU；旧报告保留并通过独立勘误纠正。

### Pilot-1C 设备映射与 per-user slice 记录

- 宿主 NVML 0/1/2/3 分别映射到 Linux minors 1/0/3/2；Job 10 的 UUID 实际对应 `/dev/nvidia1`，所以旧 `/dev/nvidia0` 的 `EPERM` 是预期隔离。
- `DevicePolicy=auto` 下，裸 Job 11、Pyxis Job 12、四并发 Jobs 13～16 均做到 allocated minor 可 `O_RDWR`、其余三张 `EPERM`、真实 CUDA context 成功。
- Job 17 的 task 位于 `/system.slice/slurmstepd.scope/job_17/...`；Slurm BPF 程序 ID 1492 对 char major 195 精确允许 minor 1、拒绝 0/3/2，strace 相符。
- `user-1001.slice DevicePolicy=closed` 瞬时策略下，新 SSH/PTY 可用，作业外四个 GPU open 全拒绝且 `cuInit` 为 `CUDA_ERROR_NO_DEVICE`；裸 Job 18、Pyxis Job 19、四并发 Jobs 20～23 全部通过。
- 四并发作业运行期间，受限 SSH 仍不能打开任何 per-GPU 节点或建立 CUDA context。
- root-only 自动回滚成功；最终 `user-1001.slice DevicePolicy=auto`，无全局/per-UID 持久 drop-in，无 Guard，无 Pilot 用户。节点 Reason 为 `gpu device mapping and transient isolation validation complete`。

因此当前运行态仍是 `DIRECT GPU BYPASS STILL POSSIBLE`，因为测试策略已按授权回滚；这不否定瞬时方案通过。不要在未批准前手工写 drop-in、部署 Guard、创建用户或 RESUME。

建议持久化方案：为每个获批 Pilot 用户生成独立 `user-<UID>.slice.d/50-h100-gpu-isolation.conf`，不修改全局 `user.slice`；逐 UID 验证、逐 UID 回滚，并在独立登录节点上线后迁移到 `pam_slurm_adopt`。下一审批语句为：`允许将GPU隔离改为Pilot用户专属user-UID.slice持久策略，并更新用户创建脚本`。

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

只有提交入口技术 Gate、作业外拒绝、作业内 open/CUDA、上述验收全部通过，且管理员再次明确批准后，才可打印 `CONTROLLED SINGLE-NODE PILOT STARTED`。这不代表平台全面生产就绪。
