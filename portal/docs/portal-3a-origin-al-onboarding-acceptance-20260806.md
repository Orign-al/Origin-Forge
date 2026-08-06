# Portal-3A：Origin-al 独立计算身份 Onboarding 计划与验收报告

## 结论

本阶段只完成了 Origin-al 的独立计算身份规划、Portal 数据库 DRAFT 和
只读/dry-run 验证。没有创建 Linux 用户或组，也没有写入任何宿主机资源。
当前停在管理员 Stage 审批 Gate。

## 执行元数据

| 项目                                  | 值                                                                              |
| ------------------------------------- | ------------------------------------------------------------------------------- |
| RUN_ID                                | `20260806-200435`                                                               |
| 开始时间                              | `2026-08-06 20:04:35 +08:00`（运行目录时间）                                    |
| 最后回归时间                          | `2026-08-06 21:28:09 +08:00`                                                    |
| 服务器                                | `sagsh100server`                                                                |
| Kernel                                | `7.0.0-28-generic`                                                              |
| 起始 Git HEAD                         | `2a74947fb2bbe6b4f8c13db491e3e1d66a8a91eb`                                      |
| Portal-3A 实现验证 HEAD（报告提交前） | `9c821c1c2e4e353baaa1aeb0c811f921039acec4`                                      |
| Monorepo                              | `/srv/gpu-platform/platform/portal`                                             |
| 运行证据                              | `/srv/gpu-platform/platform/reports/portal3-plan-20260806-200435/`（root-only） |
| 数据库备份                            | `backups/portal3-plan-20260806-200435/h100_portal.pre-portal3a.dump`            |

锁定运行版本：Node.js `v24.19.0`、pnpm `11.20.0`、Python `3.14.4`、FastAPI
`0.141.1`、PostgreSQL server `18.4`。数据库为独立的 `h100_portal`，不复用
SlurmDBD/MariaDB。

报告归档提交会在本文件提交后由最终 `git log` 记录；上面的实现 HEAD 是
测试和部署所针对的代码树，避免在报告中制造自引用 commit hash。

## 身份分离

网页账号保持：

- 登录显示名：`Origin-al`
- 规范化登录名：`origin-al`
- 角色：`platform_owner`
- 网页状态：`ACTIVE`
- 网页密码状态：`SET`
- 资源 onboarding：`NOT_ENROLLED`
- Linux 管理映射：`origin-al`

当前 Linux 管理身份只读核验为 UID/GID `1000/1000`、shell `/bin/bash`，并保留
原有管理职责组（包括 `sudo`、`adm`、`lxd`）。因此现有账号不适合作为普通
Pilot 计算身份；Portal 明确不会把它转换为计算账号。没有修改 shell、UID/GID、
Linux 密码、SSH 配置或 authorized_keys。

建议的独立普通计算身份为 `origin-pilot`。Linux 用户、同名组、Portal managed
identity、Slurm association、容器和遗留目录均未发现。

## 资源建议（全部只提出、不预留）

| 资源                   | 建议值                                     | 状态                      |
| ---------------------- | ------------------------------------------ | ------------------------- |
| Unix username          | `origin-pilot`                             | `PROPOSED — NOT RESERVED` |
| UID / GID              | `20001 / 20001`                            | `PROPOSED — NOT RESERVED` |
| XFS project ID         | `30001`                                    | `PROPOSED — NOT RESERVED` |
| 容器                   | `gpu-dev-origin-pilot`                     | 不创建                    |
| 容器 SSH 端口          | `22023`，未来仅绑定 `10.82.36.1:<PORT>:22` | `PROPOSED — NOT RESERVED` |
| Slurm account / QOS    | `company / general`                        | association 不创建        |
| GPU 上限               | `1`                                        | 由现有 QOS 验证，未授予   |
| quota                  | `300 GB hard`                              | 不创建                    |
| 容器 CPU / 内存 / PIDs | `8 / 32 GB / 4096`                         | 不创建                    |
| 容器 GPU               | `none`                                     | 不创建                    |

Worker 重新读取了 passwd/group、项目配置、监听端口、Docker 和 Slurm 结构化
适配器。`user.plan` 返回 18 项验证 `PASS`、`conflicts=[]`；没有写入 UID/GID、
project ID、端口或任何登记文件。

## GPU 隔离计划

未来只针对批准的 UID 使用精确 unit：

```text
user-20001.slice
/etc/systemd/system/user-20001.slice.d/50-h100-gpu-isolation.conf
```

drop-in 计划内容只有：

```ini
[Slice]
DevicePolicy=closed
```

计划不含 `DeviceAllow`，不修改全局 `user.slice`、`user-.slice`，不影响
`origin-al`、`codexops` 或系统服务。当前精确 drop-in 和两个全局 drop-in 均不存在。

## Portal DRAFT、Worker 与审计

- 目标：`origin-pilot`，`target_type=compute_identity`。
- 匹配的 Portal Operation：`user.plan`，状态 `DRAFT`，风险 `MEDIUM`。
- Worker 结果：`status=DRY_RUN`、`plan_status=READY`、`execution_enabled=false`。
- 幂等复核：`h100-portal-admin plan-origin-pilot` 发现既有 DRAFT，未创建重复计划。
- 受控 Worker 对所有已知写操作仍强制 `dry_run=true`；非 dry-run 返回
  `WRITE_EXECUTION_DISABLED`。
- 匹配审计事件：`operation.draft`，actor=`origin-al`，role=`platform_owner`，
  只记录 `operation_type=user.plan` 和 `execution_mode=dry-run`。
- 生产快照显示 `managed_linux_users=0`；没有 `origin-pilot` managed identity。

页面已增加“计算资源”区域，明确显示管理身份不会被转换，并显示候选 UID/GID、
project、quota、Slurm、GPU policy、容器和回滚步骤。`Stage` 与 `Activate` 按钮
保持禁用；没有 SSH 公钥时显示 `SSH KEY REQUIRED BEFORE ACTIVATION`。

## Stage / Activate 计划（未执行）

Stage 计划顺序：创建锁定且 `/usr/sbin/nologin` 的普通账号、首次登录前应用精确
UID slice、通过 GPU deny self-test、建立受控目录和 quota、建立
`company/general` association、创建无 GPU 长期容器，并保持 `STAGED`，不安装
公钥。

Activate 计划顺序：重新验证隔离和权限、要求批准的 `ssh-ed25519` 公钥、安装公钥、
启用普通 shell、启用 Guard timer、完成本人登录及 CPU/单 GPU Slurm 验收后才标记
`ACTIVE`。当前没有公钥，因此即使未来批准 Stage，也不能直接 Activate。

失败回滚只处理本次事务创建的精确 drop-in、registry、quota、association 和容器
配置；保持账号锁定/`nologin`，保留数据，不恢复 Slurm 调度，也不触碰 `origin-al`。

## 工程与验收结果

| 检查                       | 结果                                            |
| -------------------------- | ----------------------------------------------- |
| `pnpm format:check`        | 通过                                            |
| `pnpm lint`                | 通过                                            |
| `pnpm typecheck`           | 通过                                            |
| Vitest                     | `8 passed`                                      |
| Next production build      | 通过（Next 16.3.0）                             |
| Ruff check / format        | 通过                                            |
| MyPy                       | 通过（29 个源文件）                             |
| Pytest                     | `37 passed`（1 个上游弃用警告）                 |
| Playwright 功能回归        | `23 passed`；其余快照套件按无快照条件跳过       |
| Portal-2 真实视觉快照      | `20 passed`（10 页面 × 2 分辨率）               |
| Portal-3A 计算计划视觉快照 | `2 passed`（1366×768、1920×1080）               |
| `systemd-analyze verify`   | 通过；仅报告无关的 XFS `CPUAccounting` 弃用警告 |
| Alembic                    | `3b7f1c2d9e40 (head)`                           |

截图自动检查确认无渐变、玻璃拟态、AI 助手文案、横向溢出或超过 8px 的卡片圆角；
人工抽查确认中文排版、表格密度、状态色和禁用危险按钮符合规范。

## 平台只读回归

- Portal web/API/worker socket/worker 与 PostgreSQL 均 `active`；API live/ready 正常。
- Web 监听 `10.10.10.2:18080`；API 监听 `127.0.0.1:18081`；PostgreSQL 使用
  Unix socket `/var/run/postgresql/.s.PGSQL.5432`。没有新增 `22023` 监听。
- 当前访问模式：`INTERNAL HTTP ACCEPTED BY ADMINISTRATOR — VIRTUAL NETWORK ONLY`；
  TLS 未启用且当前 Pilot 范围不要求，未声称已启用 HTTPS。
- systemd failed units：`0`。
- Slurm 节点 `sagsh100server` 仍为 `drained/DRAIN`，队列为空；没有执行 RESUME。
- 4 张 H100 均可见，MIG 均 `Disabled`，显存空闲、无 compute process；DCGM discovery
  4/4 active，DCGM diag GPU0–GPU3 均 `Pass`。
- Docker、Prometheus、Grafana、DCGM Exporter、Node Exporter 健康。
- Guard timer：`disabled` / `inactive`。
- `origin-pilot` passwd/group、association、quota、GPU drop-in、容器和端口均 absent。
- `origin-al` 当前仍为 `1000/1000:/bin/bash` 并保留管理组；本阶段未执行任何 Linux
  账号、密码、SSH 或授权密钥写操作。

## 未解决风险与边界

1. 内部 HTTP 仅因管理员批准而接受；若扩展到其他网络、VPN 或公网，必须重新评估并
   优先启用 HTTPS。
2. `NETWORK PHYSICAL P0` 仍按管理员接受的单节点风险延期。
3. Docker Hub 仍 `DEFERRED / RESTRICTED`，需替代 Registry。
4. PCI DOE 为 `P1 OBSERVATION`。
5. 无 NFS、无第二节点；不支持多节点 Slurm/NCCL/RDMA 生产使用。
6. 真正 Stage 前需独立审批，并重新审查 Root Worker sandbox 的受控写路径、备份和
   回滚；本阶段没有启用任何真实写 handler。

## Gate

下一审批语句必须为：

> 允许按 Portal-3A 已验证计划 Stage 独立计算用户 origin-pilot

在收到该语句前，不创建 Linux 用户/组、UID slice、quota、Slurm association、容器
或公钥，不启用 Guard timer，不恢复 Slurm。

```text
ORIGIN-AL MANAGEMENT IDENTITY PRESERVED
ORIGIN-AL COMPUTE IDENTITY SEPARATION PASSED
ORIGIN-PILOT USERNAME AVAILABLE
PORTAL USER PLAN DRY-RUN PASSED
PROPOSED UID: 20001
PROPOSED GID: 20001
PROPOSED PROJECT ID: 30001
PROPOSED SSH PORT: 22023
SSH KEY REQUIRED BEFORE ACTIVATION

ORIGIN-AL UNIX ACCOUNT NOT MODIFIED
ORIGIN-AL UNIX PASSWORD NOT MODIFIED
ORIGIN-AL SSH CONFIG NOT MODIFIED
ORIGIN-AL COMPUTE ONBOARDING NOT EXECUTED

SLURM NODE REMAINS DRAINED
PILOT LINUX USERS NOT CREATED
PERSISTENT PILOT UID POLICIES NOT CREATED
PILOT CONTAINERS NOT CREATED
GPU BYPASS GUARD TIMER NOT ENABLED
MIG NOT MODIFIED
FIREWALL NOT MODIFIED
NFS NOT DEPLOYED
SECOND NODE NOT ADDED

NETWORK PHYSICAL P0:
DEFERRED BY ADMINISTRATOR — RISK ACCEPTED FOR SINGLE-NODE PILOT

DOCKER HUB CONNECTIVITY:
DEFERRED — ALTERNATIVE REGISTRIES REQUIRED

PCI DOE STATUS:
P1 OBSERVATION

PORTAL-3A COMPUTE ONBOARDING PLAN READY
```
