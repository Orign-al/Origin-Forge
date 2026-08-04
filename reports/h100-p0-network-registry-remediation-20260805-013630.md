# H100 P0 网络与镜像仓库整改报告

## 1. 执行信息与结论

| 项目 | 结果 |
|---|---|
| Run ID | `20260805-013630` |
| 执行开始 | `2026-08-05T01:36:30+08:00` |
| 最终只读审计 | `2026-08-05T02:32:46+08:00` |
| 主机 | `sagsh100server` / `10.82.36.1` |
| Kernel | `7.0.0-28-generic`，未升级 |
| Git 基线 | `dff0dcc63e38e691027a6dd3e699f895161ecf03` |
| 本阶段源文件提交 | `dec232c7bbd65e7e004d452be7777ff2d820ab15` |
| 原始证据 | `/srv/gpu-platform/platform/reports/network-20260805-013630` |
| 备份 | `/srv/gpu-platform/platform/backups/network-20260805-013630` |

结论：两个 P0 均未达到关闭条件。Mellanox 在 5 分钟正式采样和随后快照中继续产生 CRC/symbol error；现场尚未处理物理部件。Docker Hub 的三个必需域名获得互相矛盾且明显异常的 DNS 答案，随后 IPv4 TCP/TLS 超时；可信 DNS 路径和正式小镜像拉取尚未恢复。两类问题应分别整改：链路 CRC 无法产生语义完整但错误的 DNS 响应，因此不能用物理误码单独解释 DNS 污染证据。

最终状态：

| 检查 | 状态 |
|---|---|
| Mellanox 物理链路 | `NETWORK PHYSICAL P0 OPEN` |
| Docker Hub | `DOCKER HUB CONNECTIVITY P0 OPEN` |
| PCI DOE | `P1 OBSERVATION` |
| systemd wait-online | `P2 OPEN` |
| Controlled user onboarding | **不建议/不允许进入** |
| Slurm RESUME | **不建议/未执行** |
| NFS、第二节点、跨节点 NCCL | **不建议/未执行** |

## 2. 基线保护结果

- 开始时 Git HEAD 与指定基线一致；既有工作区无未知修改。本次 raw evidence、脚本和文档按新运行编号创建，没有覆盖旧报告。
- `codexops` 执行；最终可交互普通用户仍只有 `origin-al`（UID 1000）和 `codexops`（UID 1001）。
- Slurm 两个分区都显示节点 `drained`，原因保持 `platform bootstrap complete`，队列为空。
- 四张 H100 的 MIG Mode 均为 `Disabled`；未执行任何 MIG 命令修改。
- UFW 仍为 inactive；未修改 nftables/iptables。
- 未修改 netplan、systemd-networkd、DNS、IPv6、MTU、代理、Docker daemon、NIC 参数、NIC firmware、Slurm GRES、用户或容器。
- 未重启 Docker、Node Exporter、网络服务或服务器。
- 最终 Docker、slurmdbd、slurmctld、slurmd、node-exporter、nvidia-dcgm 均 active；Prometheus、Grafana、Node Exporter、DCGM Exporter 本机健康检查均成功。

## 3. 管理链路与 NIC 盘点

| 属性 | 实测值 |
|---|---|
| 管理接口 | `ens1f0np0`，持有 `10.82.36.1/24` |
| 默认路由接口 | `ens1f0np0`，网关 `10.82.36.254` |
| Mellanox 故障接口 | `ens1f0np0` |
| 风险标记 | `MANAGEMENT TRAFFIC DEPENDS ON FAULTY LINK` |
| PCI 地址 | `0000:61:00.0` |
| 型号 | Mellanox MT2894 / ConnectX-6 Lx `[15b3:101f]` |
| driver | `mlx5_core` |
| firmware | `26.43.1014 (LNV0000000036)` |
| speed | `10,000 Mb/s` |
| duplex | Full |
| autoneg | On |
| port | Fibre |
| FEC | Configured Auto；Active Off |
| MTU | 1500 |
| RDMA | `rocep97s0f0/1` ACTIVE、LINK_UP，映射 `ens1f0np0` |
| LLDP 邻居 | Ruijie `HKL-center-S6510-2VSU`，`TFGigabitEthernet 1/0/5` |

安装的 SFP 模块是 10GBASE-SR，因此当前 10 Gb/s 与模块能力一致；网络团队仍需确认这是否为设计速率。10GBASE-SR 上 Active FEC Off 本身不能证明配置错误，必须与交换机端能力和配置对照。

## 4. 光模块只读诊断

| 属性 | 实测值 |
|---|---|
| Vendor / PN | `LENV-Finisar` / `46C3448-L80181M` |
| Serial | `Y050RVF7E0P2` |
| 类型 | SFP、LC、10GBASE-SR、850 nm |
| 温度 | 26.95 °C |
| 电压 | 3.3173 V |
| Tx bias | 5.900 mA |
| Tx power | 0.5561 mW / -2.55 dBm |
| Rx power | 0.1064 mW / -9.73 dBm |
| Rx low warning | -18.01 dBm |
| alarm / warning | 全部 Off |

Rx power 高于模块低告警阈值，温度、电压和本机模块告警正常，但这不能排除连接器污染、间歇性光路、远端发射模块、交换机端口、本机接收模块或 NIC 端口问题。本机 Tx 与本机 Rx 不是同一方向的链路损耗测量，不能直接用二者差值给光纤定损。

## 5. 初始五分钟正式采样

采样窗口：`2026-08-05T01:42:55+08:00` 至 `01:47:55+08:00`。6 个快照形成 5 个 60 秒区间；没有清零统计。

| 区间 | RX bytes Δ | TX bytes Δ | CRC Δ | Symbol Δ | CRC/s | Symbol/s | CRC/GB RX |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0–1 | 4,228 | 206 | 12 | 12 | 0.200000 | 0.200000 | 2,838,221.381268 |
| 1–2 | 10,305 | 8,022 | 0 | 0 | 0 | 0 | 0 |
| 2–3 | 4,288 | 266 | 0 | 0 | 0 | 0 | 0 |
| 3–4 | 4,288 | 266 | 0 | 0 | 0 | 0 | 0 |
| 4–5 | 10,009 | 2,942 | 7 | 7 | 0.116667 | 0.116667 | 699,370.566490 |
| **合计/平均** | **33,118** | **11,702** | **19** | **19** | **0.063333** | **0.063333** | **573,706.141675** |

所有区间的 rx/tx discard、link-down、corrected bits、module-unplug 增量均为 0。驱动未暴露名为 `rx_uncorrected_phy` 和标准 sysfs `carrier_changes` 的计数器，因此不能把这两个缺失字段表述为硬件确认值 0；IP 层采样没有出现新的 carrier change 迹象。

本窗口业务流量极低，`CRC/GB` 分母只有约 33 KB，归一化值会非常大，不能代替正常流量验收。但任何正 CRC/symbol 增量已足以确认活跃的物理层问题。当前系统基线之前还记录过 60 秒内各增加 15,510、即约 258.5 errors/s 的高流量窗口；两次结果不矛盾。

后续只读快照继续确认增长：

- `01:47:55`：CRC `4,501,576`，symbol `4,501,569`；
- `02:18:53`：CRC `4,503,227`，symbol `4,503,220`，各增加 1,651；
- `02:32:46`：CRC `4,503,250`，symbol `4,503,243`；相对正式窗口末尾各增加 1,674，约 2,691 秒平均 0.622074/s。

这些后续区间没有持续记录总吞吐，故只用于确认错误仍在增长，不计算 CRC/GB。

结论：`PHYSICAL LINK ERROR CONFIRMED`。

## 6. 故障域判断与现场处理

当前能确定的是服务器正在接收带 CRC/symbol 错误的帧。故障域仍包含：

1. 本机模块接收端、连接器或 NIC 端口；
2. 光纤/DAC/AOC 或其两端清洁度与弯折；
3. 交换机侧模块发射端或端口硬件；
4. 两端速率、介质能力或 FEC 配置不一致。

本机 driver/firmware 可读、链路持续 UP、无 link-down/discard，模块 DDM 无 alarm，只能降低部分本机软件或完全失链假设的优先级，不能定位到单一部件。交换机侧 CRC/FCS、symbol、光功率和端口增量尚未提供，因此不能在服务器单侧判定线缆、模块或交换机中的哪一个损坏。

可直接交给现场/网络团队的逐步操作单已生成：`docs/mellanox-physical-link-field-runbook.md`。顺序严格为：

1. 清洁并重插两端连接器；
2. 更换已知正常介质；
3. 更换服务器侧模块；
4. 更换交换机侧模块；
5. 经批准切换到配置等价的已知正常交换机端口；
6. 对照交换机侧 CRC/FCS、symbol、discard、flap、speed、duplex、autoneg、FEC、optic Rx/Tx；
7. 均无效时才评估服务器另一 Mellanox 端口或 NIC 硬件。

本阶段现场人员**没有更换或重新插拔任何部件**，交换机端口也未切换。因此处理后 10 分钟采样未执行，不能打印 `NETWORK PHYSICAL RETEST PASSED`。正式候选修复需在正常业务流量下连续 10 分钟满足 CRC、symbol、link-down、carrier change 和 uncorrected 均无新增，并继续监控 24 小时。

## 7. DNS 诊断

当前 systemd-resolved 上游为 `114.114.114.114` 和 `223.5.5.5`，本机应用通过 `127.0.0.53` stub 查询。每个 Docker 必需域名的 A/AAAA 各连续查询 10 次，共 60/60 次返回 NOERROR，超时率 0%，平均耗时约 0.1–0.2 ms、范围 0–1 ms，无 SERVFAIL；但答案完整性异常：

| 域名 | 本机 stub 的稳定示例答案 |
|---|---|
| `registry-1.docker.io` A | `75.126.115.192` |
| `auth.docker.io` A | `108.160.172.232` |
| `production.cloudflare.docker.com` A | `69.63.184.142` |
| `registry-1.docker.io` AAAA | `2001::4131:1a62` |
| `auth.docker.io` AAAA | `2001::9f8a:1414` |
| `production.cloudflare.docker.com` AAAA | `2a03:2880:f10f:83:face:b00c:0:25de` |

交叉检查进一步显示同一域名随 DNS 目的地址和 UDP/TCP 传输方式得到互相矛盾的答案。例如：

- UDP 直查 `1.1.1.1` 时，Registry 得到 `162.125.2.5`；
- TCP 查询配置的两个 DNS 时，Registry 分别得到 `199.59.149.239` 和 `75.126.124.162`；
- TCP DNS 到 `1.1.1.1`、`8.8.8.8` 被 reset；
- Cloudflare DoH 被 reset，Google DoH 超时。

这些响应不是普通 CDN 轮询可以合理解释的同一答案集合，强烈指向上游 DNS 污染/透明干预。它不是本机 DNS cache 或 Docker daemon 独有问题。未经批准不得在服务器上改 DNS；网络团队应先提供经过批准、答案完整性可验证的企业解析路径，并说明 UDP/TCP 53 与 DoH 的出口策略。

## 8. IPv4、IPv6、TCP、TLS 与 HTTP

| 目标 | IPv4 HTTP | IPv4 TCP 443 | 说明 |
|---|---|---|---|
| `registry-1.docker.io/v2/` | 10 秒 connect timeout，HTTP 000 | timeout | 未到正常的 401 阶段 |
| `auth.docker.io/token` | 10 秒 connect timeout，HTTP 000 | timeout | 无 token 响应 |
| `production.cloudflare.docker.com` | 10 秒 connect timeout，HTTP 000 | timeout | CDN 路径不可用 |
| `download.docker.com` | HTTP 200 | 成功 | 一般 HTTPS 出口可用 |
| `nvcr.io/v2/` | HTTP 401 | 成功 | 401 是 Registry 正常未认证响应 |
| `ghcr.io/v2/` | HTTP 401 | 成功 | 正常 |
| `quay.io/v2/` | HTTP 401 | 成功 | 正常 |

IPv6 只有 link-local 地址，没有全局 IPv6 地址或默认路由。Docker 三域的 `curl -6` 立即失败。这应标为“IPv6 未配置可用出口”，不是本次 IPv4 connect timeout 的根因；未禁用 IPv6。

修正计时后的 `openssl s_client` v2 测试中，Registry、Auth、CDN 三域均在 30 秒后退出 124，未完成 TLS 握手，也没有收到证书；TLS version、cipher、subject、issuer 和 verify result 因此全部不可得。因未到证书阶段，不能判断这些目标是否存在 TLS interception；没有关闭证书验证或安装未知 CA。其他官方 Registry 能通过系统 CA 完成 HTTPS，进一步说明并非所有 TLS 流量都失败。

## 9. Registry API、Auth、CDN 和 pull

- Registry `/v2/`：curl exit 28，10 秒 TCP connect timeout；没有 HTTP header，无法解析 `WWW-Authenticate`。
- 匿名 Auth：curl exit 28，没有获得 JSON，也没有 token/access_token 字段。
- token 从未打印、写入报告或落盘。
- 因 Auth 不通，没有调用 manifest 和 blob 重定向流程；CDN 另行轻量连接同样超时。
- 原始 `registry-api.txt` 中旧版 `elapsed_ms` 受纳秒格式兼容问题影响，不采用该数值；exit 28 和 curl 自身的 10 秒 timeout 仍有效。脚本已把 TLS/Auth 计时修正为 `elapsed_seconds`。
- 物理链路仍在产生错误，且管理员没有提供/批准 Alpine 固定 digest，因此未执行 `docker pull`、`docker run` 或大型镜像下载。没有消耗 rate limit，也没有制造残缺层。

正式复测必须在物理链路稳定且可信 DNS 恢复后按 DNS → IPv4 TCP → TLS → Registry 401 → Auth JSON → CDN → 固定 digest 小镜像 pull/run 的顺序执行。

## 10. 代理、Docker daemon、MTU 与本机过滤

代理审计：

- 当前 shell：无 HTTP_PROXY/HTTPS_PROXY/NO_PROXY；
- sudo 环境：无代理；
- Docker service Environment/EnvironmentFiles：均无代理；
- 已检查的 systemd、environment、profile 和 apt 文件中没有发现 proxy 变量配置文件；
- 没有代理凭据被读取或输出。

Docker daemon：

- active，自 `2026-08-04T15:05:35+08:00` 起未因本阶段重启；
- `/etc/docker/daemon.json` 只有既有 data-root、local logging、live-restore 和 NVIDIA runtime；
- 无 registry-mirrors、insecure-registries、自定义 DNS、MTU 或 proxy；
- 没有配置 insecure registry、关闭 TLS 或第三方 mirror；
- curl 在 daemon 之外已失败，所以重启 Docker 没有诊断价值，本阶段未重启。

MTU：

- 本机接口和路由 MTU 均为 1500；tracepath 报 PMTU 1500；
- 对当时异常解析地址的 1472/1400/1200 字节不分片 ping 均无响应，但公网主机可禁 ICMP，不能据此证明 MTU 故障；
- 其他 Registry 的完整 TLS/HTTP 正常，没有证据支持把 MTU 改成 1400 或其他值。

过滤/conntrack：

- UFW inactive；未发现本机 OUTPUT deny 规则；nftables/iptables 主要为 Docker 标准链；
- IPv4 forwarding=1 是 Docker 既有行为，IPv6 forwarding=0；
- conntrack 为 55/262144，无耗尽；
- 未 flush 规则或 conntrack，未修改防火墙。

综合判断：一般 IPv4、TCP 443、TLS 和国外官方 Registry 并未全部失败。已确认的首要问题是 Docker 三域 DNS 答案完整性及上游对 DNS/相关出口的透明干预；在可信解析恢复前，无法独立证明是否还叠加了 Docker Hub 443 ACL。网络团队应查看源地址 `10.82.36.1` 的 DNS/NAT/出口设备日志，提供可信答案，然后检查 Registry/Auth/CDN 官方目标的 TCP 443 允许策略。不得用第三方镜像加速器规避。

## 11. PCI DOE mailbox timeout 复核

| 项目 | 结果 |
|---|---|
| GPU | 4 × NVIDIA H100 PCIe，持续可见 |
| PCIe | 四卡均 32 GT/s、x16（Gen5 x16） |
| Driver | `595.91.07` |
| VBIOS | 四卡均 `96.00.74.00.1C` |
| DOE 日志 | 每卡 3 条：ABORT timeout、reset mailbox 失败、create mailbox 失败；合计 12 条，均在启动早期 |
| Xid | 0 |
| 实际 AER error 记录 | 0；但 ACPI `_OSC` 同时报告平台不支持 AER 控制，需谨慎解释“无 AER” |
| GPU reset/掉卡 | 0 |
| DCGM r1 | 四卡全部 Pass |
| BIOS/Firmware inventory | BIOS Revision 7.20；Firmware Revision 11.10；XCC 版本因未安装 ipmitool 未采集 |

没有升级 BIOS、BMC/XCC、GPU/NIC firmware、NVIDIA driver 或 Kernel。当前满足“四卡正常、PCIe 未降速、无 Xid/掉卡、DCGM Pass”，故保持：`PCI DOE TIMEOUT — P1 OBSERVATION`。如后续出现 Xid、实际 AER、掉卡、降速或 DCGM 失败，应升级为 P0 并交由 Lenovo/NVIDIA/kernel 团队调查，不自行升级 firmware。

## 12. P2 只读复核

### systemd-networkd-wait-online

唯一 failed unit 是 `systemd-networkd-wait-online.service`。netplan generator 生成的第一个 ExecStart 要求 `ens27f0-3`、`ens1f0np0`、`ens1f1np1` 和 `enx8e3b4a390387` 全部至少达到 degraded；其中 `ens27f0-3` 和 `ens1f1np1` 均 no-carrier/configuring 且 `Required For Online: yes`，120 秒后超时。管理接口与 USB 管理链路已经 routable/configured。

最小建议是在确认这些端口确实预留/未使用后，只对对应 netplan 接口设置 `optional: true`，或经设计评审后把 wait-online 限定为实际必需接口。由于涉及 netplan/networkd 且管理流量依赖故障 Mellanox，本阶段不修改；实施前需时间戳备份、带外控制台和管理员明确批准。

### RDSEED

内核记录 `RDSEED32 is broken. Disabling the corresponding CPUID bit.`。系统已经禁用对应能力，未见运行时故障；保持 BIOS/微码 P2 观察，不在本阶段升级。

### smartd

smartmontools active。`smartctl --scan-open` 能识别 RAID 逻辑盘及 4 个 `sat+megaraid` 后端盘。后端盘不支持 ATA CHECK POWER STATUS，smartd 忽略 `-n`，部分健康属性也受控制器透传能力限制，但仍监控 4 个设备。未猜测或修改 smartd 设备参数。

## 13. 持续监控设计（未安装）

已生成并提交：

- `scripts/h100-mellanox-metrics`；
- `monitoring/h100-mellanox-metrics.service`；
- `monitoring/h100-mellanox-metrics.timer`；
- `monitoring/node-exporter-textfile-collector.conf`；
- `docs/mellanox-metrics-design.md`。

脚本固定读取 `ens1f0np0` 的五个白名单 ethtool 计数，使用 10 秒 timeout、flock、同目录临时文件和原子 rename。临时手工测试得到 5 HELP、5 TYPE、5 sample，现有 Prometheus 容器的 `promtool check metrics` 通过。

服务器/本地没有 shellcheck，本阶段没有为此扩大包安装范围，所以正式安装 Gate 未满足。当前没有创建生产 textfile 目录，没有安装 `/usr/local/sbin/h100-mellanox-metrics`，没有修改/重启 Node Exporter，没有安装或启用 service/timer，也没有部署告警。现有 Node Exporter ExecStart 仍不含 textfile collector 参数。

获得明确批准并补齐 shellcheck 后，建议确认指标存在再部署以下告警：5 分钟 CRC 增量 > 0、symbol 增量 > 0、link-down 增量 > 0。

## 14. Git、备份与执行记录

本阶段纳入 Git 的内容为两个只读诊断脚本、Mellanox 采样脚本、现场排障单、监控候选脚本/unit、测试记录和 `.gitignore` 的 raw evidence 规则。源文件提交是 `dec232c7bbd65e7e004d452be7777ff2d820ab15`，父提交即指定基线 `dff0dcc63e38e691027a6dd3e699f895161ecf03`。

提交前执行了：

- 三个 shell 脚本的 `bash -n`；
- 使用现有 `grep` 的私钥、凭据 URL、数据库/Grafana 密码、Bearer/JWT 特征扫描；
- `git diff --cached --check`；
- 显式文件列表暂存，没有使用宽泛的未知文件提交。

主要备份：

- `backups/network-20260805-013630/48-diagnose-registry-connectivity.sh.before-20260805-021059`；
- `backups/network-20260805-013630/48-diagnose-registry-connectivity.sh.before-auth-timer-20260805-022738`；
- `backups/network-20260805-013630/gitignore.before-20260805-022600`；
- `backups/network-20260805-013630/*before-format-20260805-022922*`。

raw evidence 保留在服务器运行目录并被 `reports/network-*/` 忽略，避免把 2.4 MiB ethtool 快照和运行锁纳入配置提交；本报告记录了判定所需数据。未记录密码、私钥、token、MUNGE key、数据库密码或 Grafana 密码。

## 15. 网络团队下一步清单

1. 按现场排障单一次只改一个变量，并同步导出交换机端口计数；任何切换前准备带外访问。
2. 每一步后执行统一 5 分钟采样；候选修复执行正常业务流量下 10 分钟正式采样。
3. 提供经批准、答案完整性可验证的 DNS 路径，说明当前对外 UDP/TCP 53 和 DoH 的透明策略；在修改服务器 DNS 前先给出差异并审批。
4. 使用可信 DNS 结果检查源 `10.82.36.1` 到 Registry、Auth、CDN 的 NAT/ACL/代理日志和 TCP 443 策略。
5. 物理和解析均稳定后，按顺序完成 Registry 401、匿名 Auth、CDN 和一个批准的 Alpine 固定 digest pull/run。
6. 连续观察 Mellanox 24 小时后，再评估 controlled user onboarding；NFS、第二节点和跨节点 NCCL 需要单独验收。

当前不满足 `READY FOR CONTROLLED USER ONBOARDING` 的全部前置条件。

```text
NETWORK PHYSICAL P0 OPEN

DOCKER HUB CONNECTIVITY P0 OPEN

PCI DOE STATUS:
P1 OBSERVATION

SYSTEMD WAIT ONLINE:
P2 OPEN

SLURM NODE REMAINS DRAINED
MIG NOT MODIFIED
FIREWALL NOT MODIFIED
OTHER USERS NOT CREATED
```
