# H100 受控单机 Pilot 上线前报告

## 1. 执行摘要

- Phase：`P0-B / controlled single-node pilot readiness`
- Run ID：`20260805-032025`
- 开始时间：`2026-08-05T03:18:46+08:00`
- 验收截止时间：`2026-08-05T18:00:12+08:00`
- 主机：`sagsh100server`
- 管理 IP：`10.82.36.1`
- Kernel：`7.0.0-28-generic`
- 起始任务基线：`fc1e2cc17061cd0cd112fe14dd3521ea036e0cd3`
- Phase gate 准备 commit：`016b21240da39c8613211111d6f353685d5add52`
- 最终部署配置 commit：`d7a1dd41ab82ff2ae938936023dd1b7e258d21bc`
- 报告归档 commit：本文件归档后会推进一次 Git HEAD；准确哈希在交付消息中记录，运行配置仍对应上一行 commit。

结论：在管理员明确接受 Mellanox 物理链路风险、Docker Hub 使用替代镜像源、首批最多 2–3 名内部用户且仅运行单节点任务的前提下，平台满足“受控单机 Pilot”技术准入条件。此结论不是全面生产就绪声明，也不授权自动 RESUME Slurm 或创建用户。

## 2. 物理网络 P0：延期而非关闭

管理、默认路由和异常 Mellanox 接口均为 `ens1f0np0`（PCI `0000:61:00.0`）。管理 SSH 与外部流量依赖该链路。

整改前正式 301 秒窗口：

- CRC：`+530`，平均 `1.760797/s`；
- symbol：`+530`，平均 `1.760797/s`；
- link-down/discard：`+0`；
- 窗口 RX：`27,739 bytes`，TX：`4,869 bytes`。

本阶段末只读计数：

- `rx_crc_errors_phy=123003329`；
- `rx_symbol_err_phy=123003318`；
- `rx_discards_phy=0`；
- `tx_discards_phy=0`；
- `link_down_events_phy=0`；
- Prometheus 最近 5 分钟外推增量：CRC `10.526315789473683`、symbol `10.526315789473683`、link-down `0`。

没有清洁/重插连接器，没有更换光纤、DAC/AOC、任一侧模块、交换机端口、服务器端口或 NIC；没有处理后 10 分钟零错误复测，也没有 24 小时无告警证据。因此严禁标记物理链路通过。

```text
NETWORK PHYSICAL P0:
DEFERRED BY ADMINISTRATOR — RISK ACCEPTED FOR SINGLE-NODE PILOT
```

风险继续包括丢包、重传、超时和断连。关闭 P0 前仍禁止 NFS/SMB 共享工作目录、第二节点、跨节点 NCCL、多节点 Slurm、RDMA/RoCE 生产验收、高速共享存储和大规模外部数据同步。

## 3. Mellanox 监控与告警

已部署：

- `/usr/local/sbin/h100-mellanox-metrics`；
- `h100-mellanox-metrics.service`；
- 每 60 秒执行的 `h100-mellanox-metrics.timer`；
- Node Exporter textfile collector；
- Prometheus 规则组 `h100-mellanox-physical-link`。

验证结果：

- `bash -n`、ShellCheck、unit 解析和 promtool 均通过；
- timer enabled/active，最近一次 oneshot `Result=success`、退出码 0；
- Node Exporter 仍只监听 `127.0.0.1:9100`；
- 五个 `h100_mellanox_*` 指标均存在；
- CRC 和 symbol 规则为 `firing / ok`，与已知故障一致；
- link-down 规则为 `inactive / ok`；
- 未新增监听端口，未修改任何 NIC 参数。

备份：`/srv/gpu-platform/platform/backups/p0b-20260805-032025/collector-20260805-120700/`。

## 4. systemd-networkd-wait-online

根因是五个无载波接口被纳入启动在线等待：

- Intel：`ens27f0`、`ens27f1`、`ens27f2`、`ens27f3`；
- 第二 Mellanox 端口：`ens1f1np1`。

新增独立 overlay `/etc/netplan/99-h100-optional-interfaces.yaml`，只为上述五个接口设置 `optional: true`。文件中没有管理口 `ens1f0np0`、地址、路由或 DNS。变更使用 `netplan generate`、`networkctl reload`，且只 reconfigure 五个离线接口；没有执行 `netplan apply`，没有 reconfigure/down-up 管理口。

本机 netplan 通过 generator drop-in 只等待在线的 `ens1f0np0` 与 XClarity USB 接口 `enx8e3b4a390387`。最终 wait-online 为 `active (exited)`、`Result=success`、退出码 0；管理 IP 和默认路由指纹前后不变。

备份：`/srv/gpu-platform/platform/backups/p0b-20260805-032025/netplan-20260805-135000/etc-netplan.before/`。

## 5. Docker Hub 延期与镜像策略

正确的 Docker Hub 关键域名为：

- `registry-1.docker.io`；
- `auth.docker.io`；
- `production.cloudfront.docker.com`。

现有证据：Registry/Auth IPv4 TCP/TLS timeout；CloudFront TCP/TLS 可达并验证 Amazon CA；NGC、GHCR、Quay 均返回预期 HTTP 401。当前 DNS 为 `114.114.114.114`、`223.5.5.5`，解析高度可疑，但管理员未提供 Resolver B，因此没有把 DNS 污染声明为已确认根因。

本阶段没有修改 DNS、代理、Docker daemon、MTU、IPv6、`/etc/hosts`、TLS 校验或 registry mirror。

```text
DOCKER HUB CONNECTIVITY:
DEFERRED — ALTERNATIVE REGISTRIES REQUIRED
```

Pilot 镜像优先级：本地缓存 → NVIDIA NGC → GHCR → Quay → 管理员离线导入的 archive。完整 digest 清单见 `docs/pilot-image-inventory-20260805.md`。不得依赖 Docker Hub 实时拉取，不得使用未知 mirror、insecure registry、个人代理、VPN 或 SSH 隧道。

## 6. 用户、配额、容器与管理脚本

交互用户仍只有：

- `origin-al`；
- `codexops`。

docker 组没有成员。没有创建普通 Pilot 用户或其他员工容器。

codexops XFS project：

- project name：`h100_codexops`；
- project ID：`10001`；
- path：`/srv/gpu-platform/users/codexops`；
- project quota accounting/enforcement：ON；
- hard limit：`300G`。

长期容器 `gpu-dev-codexops` 回归为 healthy：

- `Privileged=false`；
- 非 host network/PID/IPC；
- 8 CPU、32 GiB RAM、4096 PIDs；
- 无 GPU DeviceRequest/Devices；
- 无 Docker Socket，容器内无 `/dev/nvidia0`；
- SSH 只映射 `10.82.36.1:22022`；
- 只挂载批准的 home/workspace/shared 和持久 SSH host-key 目录。

八个管理脚本完成源/安装 SHA-256 一致性、warning 级 ShellCheck、collision、status/quota、stop/start/rebuild、持久数据及 SSH host-key 回归。三个危险路径脚本把不可静态证明的 trap 字符串重构为显式 cleanup 函数，行为不变。运行报告：`reports/management-scripts-20260805-175500.md`。

备份：`/srv/gpu-platform/platform/backups/p0b-20260805-032025/management-20260805-174800/`。

普通用户 Slurm association 不隐式混入账号创建；模板位于 `docs/slurm-user-association-template.md`，普通用户默认 Account `company`、QOS `general,core`、DefaultQOS `general`，不授予 admin QOS 或 Administrator。

## 7. Slurm、Pyxis、Enroot 与整卡 GPU

当前状态：

- Slurm `25.11.7`；
- Enroot `4.2.1`；
- Pyxis container 参数可见；
- munge/slurmdbd/slurmctld/slurmd 均 active；
- cgroup v2，`ConstrainDevices=yes`；
- `AutoDetect=nvml`，节点登记 4 张 H100；
- 节点 `DRAIN`，原因 `platform bootstrap complete`；
- 队列为空，无 GPU 计算进程。

历史管理员验收证据仍存在且 accounting 一致：

- Job 2：1 GPU，`COMPLETED 0:0`，请求/分配 `gres/gpu=1`，容器内只见 1 张卡；
- Job 3：2 GPU，`COMPLETED 0:0`，请求/分配 `gres/gpu=2`，容器内只见 2 张卡；
- 镜像：NGC CUDA 13.2.0，固定 digest `sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a`；
- 测试后无 GPU 进程，队列为空，节点回到 DRAIN。

本阶段没有重新 RESUME，因此没有伪称执行新的调度作业。相关 Slurm/GRES/cgroup 配置未改变；本阶段用当前 accounting、工具链、服务、设备隔离和 GPU 健康完成回归复核。

## 8. GPU 与 PCI DOE

四张 NVIDIA H100 PCIe 均可见：

| GPU | UUID | PCI | PCIe | MIG | Uncorrected ECC |
|---|---|---|---|---|---|
| 0 | `GPU-c8377945-df2c-5761-8798-66385611808b` | `01:00.0` | Gen5 x16 | Disabled | 0 |
| 1 | `GPU-992f38cb-b919-5d6c-ddba-b8c1b2f771a9` | `71:00.0` | Gen5 x16 | Disabled | 0 |
| 2 | `GPU-cb103ce8-4672-873d-1f87-6d7c5ad771b2` | `81:00.0` | Gen5 x16 | Disabled | 0 |
| 3 | `GPU-873d76ca-5a84-5177-7ef1-9fdbc98c540f` | `F1:00.0` | Gen5 x16 | Disabled | 0 |

DCGM 4 卡 short diagnostic 全部 Pass。PCIe DevSta/UESta/CESta 没有错误位；无 Xid、GPU reset、降速或掉卡。四卡仅在启动时出现 DOE abort/mailbox timeout，继续作为 P1 观察，不升级固件、BIOS、Kernel 或驱动。

MIG 只完成设计文档 `docs/h100-mig-design.md`，没有启用或创建实例，也没有修改 Slurm GRES。

## 9. 服务、监听和防火墙

- Prometheus ready，Grafana database `ok`，DCGM Exporter/Node Exporter metrics 可读；
- Prometheus、Grafana、DCGM Exporter 均为 Docker 容器；不存在对应 host systemd unit 属于预期，不是 failed unit；
- `systemctl --failed`：0；
- 监控只监听 `127.0.0.1:3000/9090/9100/9400`；
- MariaDB `127.0.0.1:3306`，nv-hostengine `127.0.0.1:5555`；
- 开发容器 SSH `10.82.36.1:22022`；
- host SSH 22 与既有 Slurm 6817–6819 保持原监听；
- 没有新增未批准的公网监控监听；
- UFW inactive；nftables 只有 Docker 自动维护的 bridge/NAT/filter 规则，没有手工防火墙整改。

## 10. 未解决项与后续边界

- P0：Mellanox CRC/symbol 持续增长，管理员接受单机 Pilot 风险并延期到 `Phase Final-Network`；
- Docker Hub：延期，必须使用替代 Registry/本地镜像；
- P1：PCI DOE mailbox timeout observation；
- wait-online P2：已解决；
- RDSEED/smartd：沿用既有兼容性观察，本阶段未修改。

Phase Final-Network 仍需按单变量执行连接器清洁/重插、线缆或 DAC/AOC、更换两侧模块、交换机端口、第二 Mellanox 端口/NIC 排查，并满足正常流量 10 分钟零 CRC/symbol、24 小时无告警及交换机侧计数正常后才能关闭网络 P0。

在物理 P0 关闭前，不允许 NFS、第二节点、跨节点 NCCL、RDMA/RoCE 生产、高速共享存储或全面生产上线。NFS 和第二节点均不建议批准。

## 11. 回滚路径

- collector/Node Exporter/Prometheus：`backups/p0b-20260805-032025/collector-20260805-120700/`；
- netplan：`backups/p0b-20260805-032025/netplan-20260805-135000/etc-netplan.before/`；
- 管理脚本：`backups/p0b-20260805-032025/management-20260805-174800/`；
- 文档：`backups/p0b-20260805-032025/docs-20260805-175000/`；
- Git 配置回退基点：`016b21240da39c8613211111d6f353685d5add52`；
- 当前部署配置：`d7a1dd41ab82ff2ae938936023dd1b7e258d21bc`。

任何回滚都必须先记录当前状态并评估与后续变更的冲突；不得用 destructive reset 覆盖未知修改。

## 12. Pilot 建议

在本报告约束下，可以建议：

```text
READY FOR CONTROLLED SINGLE-NODE PILOT
```

但必须等待管理员明确回复：

```text
允许 Slurm RESUME 并开始受控单机 Pilot
```

收到批准前节点继续 DRAIN，且不得自行创建用户或员工容器。

## 13. 最终状态

```text
MELLANOX MONITORING PASSED
SYSTEMD WAIT ONLINE PASSED
DOCKER HUB CONNECTIVITY DEFERRED
NETWORK PHYSICAL REMEDIATION DEFERRED

PCI DOE STATUS:
P1 OBSERVATION

SLURM NODE REMAINS DRAINED
MIG NOT MODIFIED
FIREWALL NOT MODIFIED
OTHER USERS NOT CREATED

READY FOR CONTROLLED SINGLE-NODE PILOT
```
