# H100 受控单机 Pilot 管理员手册

状态：Pilot-1D 已安装精确 `user-<UID>.slice` 持久隔离框架、Guard 与告警；当前 managed users=0，Guard timer disabled/inactive，`user-1001.slice DevicePolicy=auto`，节点保持 DRAIN，等待最多 3 名用户清单审批。

## 不变量

- MIG 保持 Disabled；不修改驱动、Kernel、BIOS/BMC、PCIe、GRES、Mellanox 参数、MTU、FEC、防火墙或监控公网监听。
- 不部署 NFS/SMB，不接第二节点，不做多节点 NCCL 或 RDMA/RoCE 生产验收。
- 最多 3 名 Pilot 用户；必须逐批批准，不能自动创建第 4 名。
- 长期 Docker 无 GPU、Docker/MUNGE socket、host network/PID/IPC 或 privileged。
- Docker Hub 延期；镜像只能来自本地、NGC、批准的 GHCR/Quay 或离线 archive，并记录 digest。
- 所有用户/容器变更使用既有 `h100-*` 管理脚本；这些脚本要求节点先 DRAIN。

## 当前 Gate 与已实现控制

2026-08-05 实测表明，无特权 `nobody` 在 Slurm 作业外能枚举全部 4 张 GPU，并能打开 `0666` 的 NVIDIA 设备节点。`ConstrainDevices=yes` 只隔离 Slurm job cgroup，宿主普通登录会话不受其保护。

Pilot-1D 已获批并实现第一种方案的框架；以下仍是架构边界：

1. 单节点 Pilot 当前控制：只为每个获批 Pilot UID 部署独立的 `user-<UID>.slice DevicePolicy=closed`，由两阶段用户脚本负责 drop-in、daemon-reload、验证、Guard 指标和逐 UID 回滚。当前无 Pilot 用户，所以没有真实 UID drop-in。
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

Pilot-1D 已安装经批准框架；`codexops` 回归后仍按要求恢复 `DevicePolicy=auto`。当前没有普通 Pilot 登录身份，因此不得把管理员会话可访问 GPU 描述成已上线用户绕过；也不得声称真实用户隔离已验收。不要手工写 drop-in、创建用户或 RESUME。

持久框架只生成独立 `user-<UID>.slice.d/50-h100-gpu-isolation.conf`，不修改全局 `user.slice` 或 `user-.slice`；逐 UID 验证和回滚。下一审批语句为：`允许创建清单中的Pilot用户，并为每个用户应用专属user-UID.slice GPU隔离策略`。

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

输出创建计划并等待原文批准：`允许创建清单中的Pilot用户，并为每个用户应用专属user-UID.slice GPU隔离策略`。没有该原文批准不得 stage。

## 精确 GPU 隔离管理工具

只通过以下工具管理策略，所有修改操作使用 sudo：

```bash
sudo h100-user-gpu-isolation plan USER
sudo h100-user-gpu-isolation apply USER
sudo h100-user-gpu-isolation verify USER
sudo h100-user-gpu-isolation status USER
sudo h100-user-gpu-isolation self-test USER
sudo h100-user-gpu-isolation list
sudo h100-user-gpu-isolation remove USER
```

工具从 `getent passwd` 解析 UID，自行生成 `user-<UID>.slice` 和精确 drop-in 路径；拒绝 root、origin-al、codexops、系统 UID、不存在账号和 sudo/docker/video/render/admin 组成员。唯一的不存在账号例外是已登记离职 tombstone 的最终 `remove`：必须证明登记 UID 也不存在且从未复用。`apply` 前要求无会话/进程，使用 flock、备份和原子 rename；账号仍存在时，`remove` 要求 nologin、密码锁定、无会话/进程，并且只删除内容仍为固定两行的本工具文件。不要手工创建全局 `user.slice.d`/`user-.slice.d`，不要添加任何 NVIDIA `DeviceAllow`，不要用 chmod/chown、video/render/gpu 组或 Prolog 动态 chown。

核查：

```bash
sudo systemctl cat user-UID.slice
sudo systemctl show user-UID.slice -p ControlGroup -p DevicePolicy -p DeviceAllow
sudo h100-user-gpu-isolation self-test USER
```

## 两阶段创建用户、quota、association 和容器

在维护窗口先 DRAIN 并确认队列为空。使用既有脚本，不手工并行实现：

```bash
sudo h100-user-create --plan USER UID GID PROJECT_ID SSH_PORT ACCOUNT QOS
sudo h100-user-create --stage USER UID GID PROJECT_ID SSH_PORT ACCOUNT QOS \
  --confirm-stage USER
sudo h100-user-create --status USER
```

`--stage` 不接受公钥参数，创建锁定密码、`/usr/sbin/nologin`、无 `authorized_keys` 的账号；隔离 self-test 通过后才创建 300GB quota、`general` QOS association 和默认无 GPU 的长期容器。Stage 成功只输出 STAGED，用户仍不能登录，SSH key 状态为 `REQUIRED_BEFORE_ACTIVATION`。传入 `--public-key-file` 必须以 `PUBLIC_KEY_NOT_ALLOWED_DURING_STAGE` 拒绝。任何失败都保持 nologin、停止容器、回滚 association/project 映射/精确策略并保留可能创建的数据供人工审计。

独立复核 STAGED 报告和审批后，只允许 Root Worker 使用目标分离的受控 bundle 执行：

```bash
sudo h100-user-create --activate USER \
  --host-public-key-file /var/lib/h100-portal/ssh-key-staging/<REQUEST_UUID>.host.pub \
  --container-public-key-file /var/lib/h100-portal/ssh-key-staging/<REQUEST_UUID>.container.pub \
  --confirm-activate USER
sudo h100-user-create --status USER
```

上述命令是 Worker 与管理工具之间的契约，不是管理员手工旁路步骤。Activate 缺少公钥时在任何修改前以 `PUBLIC_KEY_REQUIRED_FOR_ACTIVATION` 拒绝；只接受 Root Worker 受控 staging 目录中的 UUID 文件，不接受浏览器宿主路径、私钥、密码或占位密钥。Activate 再次验证策略、无高权组、容器无 GPU 和公钥指纹，安装 `authorized_keys` 后启动 Guard，最后才开放 `/bin/bash`。密码保持锁定。用户本人随后验证 SSH；管理员不得用用户私钥代测。在 SSH 客户端 Gate 通过前，Slurm 保持 DRAIN，且不得提交 Pilot 作业。

若服务器端后置条件或 Portal ACTIVE 持久化失败，Worker 使用固定
`--rollback-activate USER --confirm-rollback-activate USER` 恢复 nologin、密码锁定、两处
authorized_keys absent 与容器 stopped，同时保留 identity、GPU policy、quota、Slurm
association 和用户数据。

## Guard 日常操作与失败

```bash
sudo systemctl start h100-gpu-bypass-guard.service
sudo journalctl -u h100-gpu-bypass-guard.service --since today --no-pager
systemctl is-enabled h100-gpu-bypass-guard.timer
systemctl is-active h100-gpu-bypass-guard.timer
curl -fsS http://127.0.0.1:9100/metrics | grep '^h100_gpu_'
curl -fsS http://127.0.0.1:9090/api/v1/alerts
```

无受管用户时 timer 必须 disabled/inactive，手工 service 应输出 `NO MANAGED PILOT USERS`。首名用户 Stage 的账号、精确策略、self-test、quota、association 和无 GPU 容器全部验收成功后脚本才 enable timer；Activate 只重新验证 Guard。Guard 失败会在节点尚未 DRAIN 时写精确 Reason 并 DRAIN；若已有 DRAIN，则保留原 Reason。它永不自动 RESUME、删除策略或杀用户进程。先查看 policy、registry、transient probe、GPU/GRES/MIG 和当前作业，再人工修复；不得通过放开全部 NVIDIA 设备消除告警。

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

为既有 Development Container 开启容器内用户 sudo 是一次会重建该用户容器的独立变更，
必须先取得该用户中断授权并逐个执行：

```bash
sudo h100-container-sudo-enable USER --confirm=USER
```

不得批量启用。工具要求宿主账号 nologin、密码锁定、无 Host `authorized_keys`、无高权组，
并验证容器非 privileged、非 host namespace、受限 AppArmor/seccomp、无 GPU、Docker/Worker/
MUNGE socket 或非白名单挂载；原镜像不重建，失败自动回滚。完成后复核容器内
`sudo -n id -u` 为 `0`、sudoers 只读、镜像 digest 未变及宿主登录面仍关闭。

容器删除默认保留数据：

```bash
sudo h100-container-delete USER
```

带数据删除是不可逆的独立审批动作；必须先完成离职/保留决策和备份验证，不在日常清理中执行。

## 停用、离职、UID 迁移与回滚

1. 在 Slurm 中禁止新作业，DRAIN，`scancel` 该用户作业并导出 association。
2. 把 shell 改为 `/usr/sbin/nologin`，锁定密码，移走/禁用 `authorized_keys`，终止登录会话。
3. 停止长期容器；保留数据、Compose、SSH host key 和 `user-<UID>.slice` GPU 限制。
4. 冻结/删除 association 前保留审计；完成数据归档和人工审批前不删除工作目录。
5. 最后才删除账号。确认旧 UID 没有被任何账号复用后，再按审批精确删除本工具 drop-in/登记；UID 和 project ID 均不得立即复用。
6. quota/QOS 调整前备份 `/etc/projects`、`/etc/projid` 和 association；验证差异、唯一性及回滚命令。

UID 禁止原地改号。按迁移处理：创建新 UID 的 STAGED 账号和新的 `user-NEWUID.slice`，迁移 ownership，完整验证后禁用旧 UID；完成归档/审计和旧账号删除后才移除旧策略。任何阶段失败都保留 Slurm DRAIN。

若 Pilot 变更失败，优先保持节点 DRAIN、禁用新增登录/作业并停止相关容器；恢复备份的非 secret 配置，验证 Git diff、服务、GPU、quota、监听和监控后再请求 RESUME。

## Pilot 上线验收

每名用户分别验证 CPU job、单 GPU Pyxis job、两 GPU 超额申请被 QOS 拒绝/保持 Pending 后取消、sacct Account/QOS 正确、GPU 释放、目录隔离、无 sudo/docker 高权组、容器无 GPU/敏感 socket。最终复核 4 GPU、MIG Disabled、DCGM Pass、无 Xid/AER、队列、systemd、监控、quota、监听端口及 Git secret 扫描。

只有提交入口技术 Gate、作业外拒绝、作业内 open/CUDA、上述验收全部通过，且管理员再次明确批准后，才可打印 `CONTROLLED SINGLE-NODE PILOT STARTED`。这不代表平台全面生产就绪。

### Portal-3F 首名真实用户验收

真实用户本人使用其保存的私钥完成 Host 与 Container 两项连接后，管理员才可使用固定
CLI 记录确认并运行首个 Pilot 验收：

```bash
h100-portal-admin portal3f-origin-pilot \
  --approval-text '允许进入 Portal-3F，记录 SSH Client Validation PASS 并执行首个 Slurm/GPU Pilot 验收。'
```

该入口不接收私钥、命令、路径、分区、镜像或资源参数。它创建两条独立 Operation：先在
Worker 重新验证 ACTIVE/INSTALLED、SSH public-key-only、容器 GPU NONE、Guard、quota、
association、DRAIN/空队列和健康状态后记录 Host/Container Client Validation `PASS`；再
执行固定的 CPU job 与单 GPU Pyxis/Enroot job。GPU job 使用固定 NGC digest 镜像和固定 C
探针，并在 CUDA context 存活期间从 `user-20001.slice` 并发验证作业外 GPU open/CUDA
均被拒绝。

所有路径最终都必须回到 DRAIN。成功只记录 `pilot_acceptance_status=PASSED`，不得输出
`CONTROLLED SINGLE-NODE PILOT STARTED`；正式保持 IDLE 仍需下一次明确审批。失败时只取消
本次唯一命名作业，保留用户数据、ACTIVE 身份、SSH Client Validation 记录和其他既有资源。
