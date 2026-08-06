# Portal-3A：Origin-al 独立计算身份 Onboarding 计划

Portal 网页账号 `Origin-al`（规范化登录名 `origin-al`）继续是
`platform_owner` 管理/恢复身份。它映射到现有 Linux 用户 `origin-al`，UID 1000，
并承担宿主机管理职责。Portal-3A 不把该账号转换成计算账号，也不修改其 shell、
密码、sudo 权限、SSH 配置或授权密钥。

## 建议身份

| 项目       | 建议值                                                | 状态                      |
| ---------- | ----------------------------------------------------- | ------------------------- |
| 计算用户名 | `origin-pilot`                                        | 只规划，不创建            |
| UID/GID    | 从 Pilot 范围最低空闲值开始（当前示例为 20001/20001） | `PROPOSED — NOT RESERVED` |
| Project ID | 从 30000–39999 最低空闲值开始（当前示例为 30001）     | `PROPOSED — NOT RESERVED` |
| 容器       | `gpu-dev-origin-pilot`                                | 不创建                    |
| SSH 端口   | 22023 起的受控容器端口范围                            | `PROPOSED — NOT RESERVED` |
| Slurm      | account `company`，QOS `general`                      | association 不创建        |
| GPU 上限   | 1                                                     | 由 QOS 和后续审批共同约束 |
| quota      | 300 GB hard                                           | 不创建                    |
| 长期容器   | 8 CPU、32 GB、PIDs 4096、GPU none                     | 不创建                    |

候选值由 Root Worker 重新读取 `/etc/passwd`、`/etc/group`、平台状态、
`/etc/projects`、`/etc/projid`、监听端口和 Docker 白名单数据。候选值不会被
写入这些文件，也不会形成预留。

## GPU 隔离

未来 Stage 在首次登录前应用精确的
`user-<UID>.slice`：

```ini
[Slice]
DevicePolicy=closed
```

drop-in 目标为 `/etc/systemd/system/user-<UID>.slice.d/50-h100-gpu-isolation.conf`。
不得加入 `DeviceAllow`，不得创建全局 `user.slice` 或 `user-.slice` drop-in。

## Stage / Activate 顺序

Stage 计划（Portal-3A 只展示和 dry-run）：

1. 创建普通账号，保持 `/usr/sbin/nologin` 和锁定密码。
2. 应用精确 UID slice 并通过 GPU deny self-test。
3. 建立受控目录和 XFS project quota。
4. 建立 `company/general` association（最多 1 GPU）。
5. 创建无 GPU、非 privileged、无 Docker Socket 的长期容器。
6. 保持 `STAGED`，不安装 `authorized_keys`。

Activate 必须单独审批、二次确认并重新验证隔离。需要已批准的
`ssh-ed25519` 公钥；当前状态为 `SSH KEY REQUIRED BEFORE ACTIVATION`。随后才可
安装公钥、启用普通 shell、启用 Guard timer，并执行本人登录及单 GPU Slurm 验收。

## 回滚

Stage 或 Activate 失败时只恢复本次事务创建的精确 drop-in、registry、quota、
association 和容器配置，保留用户数据并保持账号禁用。回滚不恢复 Slurm 调度，
也不触碰 `origin-al`。

## 当前 Gate

当前仅允许 Portal `user.plan` dry-run 和数据库 `DRAFT`。没有 Linux 用户、Linux
组、UID drop-in、project quota、Slurm association、SSH 公钥、容器或新监听端口。
`origin-al` 仍为管理账号，Slurm 仍 `DRAIN`，Guard timer 保持 disabled/inactive。
下一步必须获得明确批准语句后才可 Stage：

> 允许按 Portal-3A 已验证计划 Stage 独立计算用户 origin-pilot
