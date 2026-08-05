# 长期 Docker 到 Slurm 的安全提交通道

状态：Pilot-1D 已获批并安装 Pilot 用户专属持久 GPU 隔离框架；当前没有 Pilot 用户、没有活动的 per-UID 策略，Guard timer 保持 disabled/inactive，Slurm 保持 DRAIN，等待最多 3 名用户清单审批。

## 边界与不变量

- 长期个人 Docker 默认无 GPU；GPU 计算只能由 Slurm 分配。
- 不向个人 Docker 挂载 MUNGE socket、`munge.key`、Slurm 管理 secret、Docker Socket 或宿主管理员 SSH 私钥。
- 不把 `codexops` 管理私钥复制到任何普通用户容器。
- 用户身份必须在提交入口重新认证，并映射到自己的 Slurm association、Account 与 QOS。
- 个人容器内的 root 只代表该容器内 root；它不是宿主机或 Slurm 管理员身份。
- 当前节点保持 DRAIN；任何生产 RESUME 仍需管理员明确批准。

## 方案比较

| 方案 | 身份与凭据 | 优点 | 主要风险与控制 | 当前建议 |
|---|---|---|---|---|
| A：个人容器通过 SSH 连接宿主提交入口 | 用户自己的 SSH 密钥；不得使用管理员密钥 | 用户可从 VS Code/容器工作流直接提交；沿用 sshd 审计和 Unix 身份 | 用户私钥位于其持久化 home，容器 root 可读取；必须使用个人密钥、严格 known_hosts、禁用 agent 共享管理员身份，并限制宿主登录权限 | 可作为后续便利方案，先做单用户原型和威胁评审 |
| B：`slurmrestd` + JWT | 短期、最小权限 JWT；服务端 JWT signing key 不进入个人容器 | API 友好，便于门户和自动化；可与未来 JumpServer/内部服务集成 | 需 TLS、密钥轮换、token 过期/撤销、请求审计、限流和 Slurm 版本兼容；错误配置会扩大控制面暴露 | 当前不部署；待有集中身份系统和 secret 生命周期管理后评估 |
| C：用户直接 SSH 登录宿主后提交 | 用户个人 SSH 公钥，由宿主 sshd 认证 | 无需把 MUNGE/JWT/管理员凭据放入容器；Slurm CLI 与 Unix 用户天然一致 | `DevicePolicy=auto` 时普通会话可绕过 Slurm；必须在首次登录前为该获批 UID 部署并验证独立的 `DevicePolicy=closed` | Pilot-1D 已安装框架；须逐用户 stage/activate，当前无用户策略 |

## Pilot-1 实测 Gate（2026-08-05）

在节点完成 CPU、单 GPU、双 GPU 回归并处于空闲状态后，使用现有 `nobody` 身份做了无负载验证，没有创建测试用户：

- 未发现 `pam_slurm_adopt` 或等价的 SSH 会话收容策略；`PrologFlags` 为空。
- `/dev/nvidia0`～`3`、`/dev/nvidiactl` 和 `/dev/nvidia-uvm` 均为 `root:root 0666`。
- 作业外的 `nobody` 能通过 NVML 枚举全部 4 张 H100。
- 作业外的 `nobody` 能以读写方式打开 NVIDIA 设备节点；测试只打开文件描述符，没有产生 GPU 负载。
- 既有 `gpu-dev-codexops` 容器仍无 GPU DeviceRequest、Docker Socket、MUNGE socket/key 或敏感宿主挂载；问题位于宿主登录路径，不位于长期容器模板。

因此，Slurm 的 `ConstrainDevices=yes` 已正确隔离**作业内**分配，但不能限制普通宿主登录会话。节点已按 Gate 重新 DRAIN，Reason 为 `direct GPU bypass possible outside Slurm`。

## 最小整改路径

在创建任何 Pilot 用户前，管理员应单独审批并实现一种可验证的技术控制：

1. 单节点 Pilot 的已批准控制：只为每个获批 Pilot UID 创建独立的 `user-<UID>.slice` 持久 drop-in，设置 `DevicePolicy=closed`；两阶段用户脚本负责首次登录前生成、验证和逐 UID 回滚，Guard 证明作业外 open/CUDA 被拒绝且 Slurm GRES 数量一致。框架已部署，但当前没有登记用户，所以没有活动策略。
2. 更强边界：普通用户不获得计算节点通用 shell，只通过按 Unix 用户重新认证的受控提交网关调用 `sbatch`、`srun`、`squeue`、`sacct` 和 `scancel`。网关必须拒绝任意宿主命令、端口/agent/X11 转发，并保留用户与 Job ID 审计。
3. 可选：部署经 TLS、短期 per-user token、撤销和审计验证的 `slurmrestd` 提交入口；JWT signing key 仅保存在 root-only 服务端路径。
4. 多节点阶段的标准方案：设置独立登录节点；计算节点使用 `pam_slurm_adopt`/`PrologFlags=contain`，拒绝无作业 SSH 会话并将有作业会话纳入对应 job cgroup。

不得仅通过改为 `video`/`render` 组、临时 `chmod`、用户承诺或隐藏 `nvidia-smi` 来声称已隔离。整改验收至少要证明：作业外普通身份无法枚举或打开 GPU，单 GPU 作业内只见一张，双 GPU 作业内只见两张，作业结束后访问随之撤销。

## 原方案 C 流程（当前停止）

以下流程仅保留为历史设计说明；在提交入口技术 Gate 通过前不得执行：

1. 管理员经批准后创建员工宿主账号，只安装该员工的个人公钥，不授予宿主 sudo 或 docker 组权限。
2. 为该用户建立 Slurm association、Account 与 QOS；默认遵守每用户 GPU 上限。
3. 用户通过宿主 SSH 登录，在持久化工作区准备作业脚本，通过 `srun`/`sbatch` 提交。
4. GPU 作业用 Pyxis/Enroot 的固定镜像引用运行；Slurm cgroup v2 与 `ConstrainDevices` 控制可见 GPU。
5. 审计关联 Unix 用户、SSH 登录、Slurm Job ID、Account/QOS 与容器镜像引用。

## 方案 A 的安全原型条件

- 只能使用该普通用户自己的 SSH 密钥；密钥由用户自行管理，不由平台复制管理员密钥。
- 容器内维护专用、严格校验的宿主 known_hosts；禁止 `StrictHostKeyChecking=no`。
- 不使用 SSH agent 转发管理员 agent；若启用用户 agent，必须明确其可见密钥范围。
- 宿主 sshd 继续按 Unix 用户认证；容器不能直接访问 MUNGE socket。
- 提交入口只接受 Slurm CLI/受控命令；不授予 docker、sudo 或任意宿主挂载能力。
- 先对一个非管理员测试用户做原型；当前 `codexops` 管理身份不用于模拟普通用户模型。

## 方案 B 上线前的最低条件

- 使用当前 Slurm 版本官方支持的 `slurmrestd` 与 JWT 配置，固定接口版本。
- JWT signing key 仅保存在 root-only 服务端路径，不进入镜像、Compose、Git 或日志。
- token 必须短期、可轮换、可撤销，并绑定用户/Account；禁止共享管理员 token。
- 在受控反向代理后启用 TLS、认证、限流和完整审计；默认只监听 localhost，外部开放需单独审批。
- 完成故障模式、权限绕过、token 泄漏与重放测试后再考虑生产。

## 当前结论

Pilot-1D 已把方案 C 实现为精确 per-UID 框架，但没有为 `origin-al`、`codexops`、任何系统账号或不存在 UID 写入持久策略。当前不修改设备权限、PAM、sshd、个人 Docker 挂载或 secret 管理，不创建员工账号，并保持 Slurm DRAIN。下一 Gate 是管理员提供并批准最多 3 名 Pilot 用户清单；只有每个用户完成 STAGED、隔离 self-test、单独 ACTIVATE 和用户本人验收后，才能另行请求 RESUME。

## Pilot-1B `user.slice` 试验结果（2026-08-05）

systemd 259 + unified cgroup v2 的层级前提成立：SSH 会话在 `user.slice`，`slurmd`、`slurmstepd.scope`、Docker、DCGM 和监控在 `system.slice`。Transient `DevicePolicy=closed` canary 通过，普通 shell/PTY/DNS/HTTPS 也通过；运行时策略下新 SSH 会话和 `nobody` 的所有 NVIDIA 节点 open 均被 `EPERM` 拒绝，管理员 system.slice transient 能看到 4 卡并通过 DCGM。

但是，临时 RESUME 后 Job 10 的探针固定打开 `/dev/nvidia0` 并得到 `EPERM`；当时没有记录可见 UUID 对应的 Linux minor，因而不能证明它打开的是 allocated 设备。该轮因此按保守失败路径没有创建持久 drop-in、Guard 或管理员辅助脚本，并执行了自动 DRAIN 和回滚。

该结论由下面的 Pilot-1C 物理设备映射调查勘误；旧报告保留，不删除。

## Pilot-1C 物理设备映射勘误与瞬时验证（2026-08-05）

宿主机映射证明 NVML index 与 Linux minor 不是同一编号：NVML 0/1/2/3 分别映射到 `/dev/nvidia1`、`/dev/nvidia0`、`/dev/nvidia3`、`/dev/nvidia2`。Job 10 可见 UUID `GPU-c8377945-df2c-5761-8798-66385611808b` 的实际 minor 为 1，所以其 allocated 节点是 `/dev/nvidia1`；固定测试的 `/dev/nvidia0` 属于另一张未分配 GPU。对 `/dev/nvidia0` 的 `EPERM` 是正确隔离，而不是 allocated GPU 被拒绝。Job 10 的旧结论属于 device-identity false negative。

Pilot-1C 使用 UUID、PCI Bus ID、NVML index、procfs `Device Minor` 和设备 major/minor 建立完整映射，并以 `O_RDONLY`、`O_WRONLY`、`O_RDWR`、真实 CUDA Driver API context 和限定范围的 strace 复测：

- `DevicePolicy=auto` 下裸 Slurm Job 11 与离线固定 SquashFS 的 Pyxis Job 12 均只允许实际 allocated minor 1，拒绝其余 minors，CUDA context 成功并 `COMPLETED 0:0`。
- 四并发 Jobs 13～16 覆盖四个 UUID/minor；每个作业只能打开自己的 per-GPU 节点，四个 CUDA context 均成功。
- Job 17 的有效 cgroup 路径是 `/system.slice/slurmstepd.scope/job_17/...`。Slurm `cgroup_device` 程序 ID 1492 明确允许 char major 195/minor 1、拒绝 minors 0/3/2；control/UVM 节点保持允许，strace 与程序完全一致。
- 仅对 `user-1001.slice` 瞬时设置 `DevicePolicy=closed` 后，新 SSH/PTY 正常，作业外四个 per-GPU 节点均 `EPERM`，`cuInit` 返回 `CUDA_ERROR_NO_DEVICE`。
- 从受限会话提交的裸 Job 18、Pyxis Job 19 和四并发 Jobs 20～23 全部通过；四作业运行期间，提交会话仍不能打开任何 GPU 或创建 CUDA context。
- 测试结束后已恢复 `user-1001.slice DevicePolicy=auto`、取消 transient timer/service，并以精确 Reason 重新 DRAIN。没有写入全局或 per-UID 持久 drop-in，没有部署 Guard，也没有创建 Pilot 用户。

建议把单节点 Pilot 的 GPU 隔离从全局 `user.slice` 改为每个获批 Pilot 用户独立的 `user-<UID>.slice DevicePolicy=closed`，并由既有用户创建脚本生成、验证和逐 UID 回滚。这样不会改变 `origin-al`，管理员账户也可独立管理；Slurm 作业继续位于 `system.slice/slurmstepd.scope`。未来独立登录节点上线后，计算节点仍应迁移到 `pam_slurm_adopt`/`PrologFlags=contain`。

该持久化审批已在 Pilot-1D 收到并落实为框架。当前下一审批语句为：`允许创建清单中的Pilot用户，并为每个用户应用专属user-UID.slice GPU隔离策略`。未收到该语句前，不创建用户、不创建真实 UID drop-in、不启用 Guard timer、不创建用户容器、不 RESUME 节点。

## Pilot-1D 精确 per-UID 持久设计

每个获批 Pilot 用户只使用数字 UID 生成以下唯一目标：

```text
/etc/systemd/system/user-<NUMERIC_UID>.slice.d/50-h100-gpu-isolation.conf
```

文件内容只能是：

```ini
[Slice]
DevicePolicy=closed
```

不写任何 `DeviceAllow`。特别禁止 `/etc/systemd/system/user.slice.d/50-h100-gpu-isolation.conf` 和 `/etc/systemd/system/user-.slice.d/50-h100-gpu-isolation.conf`：前者会影响管理员和所有登录用户，后者会隐式覆盖所有 UID 模板实例，二者都不满足逐用户审批和逐用户回滚。用户名只用于管理员界面；unit 和路径永远由 `getent passwd` 返回的数字 UID 生成。

SSH 会话位于 `/user.slice/user-<UID>.slice/...`，因此继承该 device BPF；Slurm 作业仍由 `slurmd/slurmstepd` 创建在 `/system.slice/slurmstepd.scope/...`，不会继承登录 slice 的拒绝策略，并继续由 `task/cgroup` 与 `ConstrainDevices=yes` 精确放行 allocated GPU。Docker、DCGM、Prometheus、Grafana 和 Node Exporter 继续位于 `system.slice`。GPU 的 NVML index、UUID 和 Linux minor 不是同一编号；作业内设备身份必须用 UUID/procfs minor 关联，不能把逻辑 GPU 0 当作 `/dev/nvidia0`。

## 首次登录前事务

`h100-user-create` 不再默认一次完成全部动作：

1. `--plan` 只验证候选 UID/GID、project ID、端口、Slurm account/QOS 和公钥，不修改系统。
2. `--stage` 创建锁定密码且 shell 为 `/usr/sbin/nologin` 的账号，不安装 `authorized_keys`；先应用精确 UID 策略并执行 device-open/CUDA transient self-test，再创建 quota、Slurm association 和默认无 GPU 的长期容器。
3. Stage 任一步失败时保持账号不可登录、停止容器、回滚 association、project 映射和本工具创建的精确策略；数据目录不被静默删除。
4. `--activate` 需要独立重复审批；再次验证策略、高权限组和容器无 GPU，先在不可登录状态安装公钥并启动 Guard，再启容器，最后才切换到 `/bin/bash`。失败则回到 STAGED、移走公钥、停止容器，同时保留数据和 GPU 策略。

## Guard、自动 DRAIN 与回滚

受管用户登记位于 `/etc/h100-platform/gpu-isolated-users`，只保存安全用户名与数字 UID，使用 flock、备份、同目录临时文件和原子 rename。Guard 必须由 `h100-gpu-bypass-guard.service` 在 `system.slice` 运行；它通过 `systemd-run --system --slice=user-<UID>.slice --uid=<UID>` 执行最终 open/CUDA 探针并验证实际 cgroup 路径，同时检查 `ConstrainDevices=yes`、4 张 NVIDIA GPU、4 个 Slurm GRES 和 MIG Disabled。失败只会 DRAIN，永不自动 RESUME，也不自动删除策略或终止用户进程。

第一个用户完成 ACTIVATE 前，timer 必须保持 disabled/inactive。没有受管用户时，手工 Guard 成功表示框架和全局 GPU/Slurm 计数正常，但明确输出 `NO MANAGED PILOT USERS`，不能声称任何真实用户已通过隔离。回滚只操作本工具管理的精确文件；全局 slice、NVIDIA 节点权限和未知 drop-in 不动。

独立登录节点上线后，应把计算节点迁移到 `pam_slurm_adopt`/`PrologFlags=contain`，拒绝无 allocation 的计算节点 SSH 并将获准会话纳入 job cgroup。当前方案是单节点 Pilot 的 GPU 设备边界，不是完整 CPU、内存、网络或宿主多租户沙箱。
