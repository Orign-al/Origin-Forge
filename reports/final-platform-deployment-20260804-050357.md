# H100 新系统平台部署报告

## 结论与证据边界

- Run ID：`20260804-050357`
- 账号引导完成：`2026-08-04T05:02:33Z`
- 平台运行起点：`2026-08-04T05:03:57Z`（`2026-08-04T13:03:57+08:00`）
- 最终验收：`2026-08-05T01:01:30+08:00` 至 `2026-08-05T01:11:33+08:00`
- 最终只读验收：68 个命令章节，失败数 `0`
- 最终验收原始记录：`/srv/gpu-platform/platform/reports/final-acceptance-raw-20260804-050357-20260805-010130.txt`
- 原始记录 SHA-256：`bb5e1dab30f38771bdf9f0173e3bb3bfed20ccfa6f163968cede7f1edd25eeb7`
- 最终 Mellanox 记录：`/srv/gpu-platform/platform/reports/network-physical-final-20260804-050357-20260805-010158.txt`
- Mellanox 记录 SHA-256：`6b20748990abbffa1c91b33247de7ef802bd54b28df4c94e49f38913f23f90eb`
- 原始 `.txt`、构建树、日志、备份和 secrets 保留在服务器，但均被 Git 忽略。

## 状态总表

| 阶段 | 真实状态 |
|---|---|
| Account bootstrap | `ACCOUNT BOOTSTRAP PASSED` |
| Baseline audit | `BASELINE AUDIT PASSED` |
| Storage baseline | `STORAGE BASELINE PASSED` |
| Project quota | `PROJECT QUOTA PASSED` |
| NVIDIA driver | `NVIDIA DRIVER PASSED` |
| Docker Engine | `DOCKER PASSED` |
| Docker GPU | `DOCKER GPU PASSED` |
| DCGM base | `DCGM BASE PASSED` |
| Slurm base | `SLURM BASE PASSED` |
| Pyxis + Enroot | `PYXIS ENROOT PASSED` |
| Monitoring | `MONITORING PASSED` |
| Long-lived Docker template | `LONG-LIVED DOCKER TEMPLATE PASSED` |
| Network physical check | `NETWORK PHYSICAL CHECK OPEN` |

## 55 项验收记录

1. **部署起止时间**：账号引导从 `2026-08-04T05:02:33Z` 开始；平台 Run ID 起点为 `2026-08-04T05:03:57Z`；最终验收于 `2026-08-05T01:11:33+08:00` 收口。

2. **主机名**：`sagsh100server`；硬件 DMI 为 Lenovo ThinkSystem SR675 V3。

3. **管理 IP**：`10.82.36.1/24` 位于 Mellanox `ens1f0np0`；默认路由为 `10.82.36.254`。地址与路由在最终验收时存在。

4. **OS**：Ubuntu 26.04 LTS（Resolute Raccoon，`amd64`）。所有版本均由新系统重新探测，不使用旧系统结论。

5. **Kernel**：`7.0.0-28-generic`，启动 ID `3b8b3af2-d2bd-4040-886e-e9129ac6c005`。

6. **codexops 创建结果**：`uid=1001(codexops)`，组为 `codexops,sudo,users,gpu-platform-admin`；不属于 `docker` 组。系统中 UID 大于等于 1000 的交互用户仅 `origin-al` 和 `codexops`。

7. **SSH 公钥指纹**：codexops Ed25519 公钥为 `SHA256:vRb0Vno7tl/hcmiFeDiekEKTx1zsA/oPHvpsSoG3Vwc`；已批准的服务器 Ed25519 主机指纹为 `SHA256:9TMlLHNTv1g+PxhdP5v7VcakCn7MofahpNpgJGt/vYs`。私钥从未输出或复制到服务器/容器。

8. **密钥登录验证**：`codexops@10.82.36.1` 使用专用密钥登录通过；只针对 codexops 禁用了密码与 keyboard-interactive，公钥认证启用；`sshd -t` 最终通过。

9. **sudo 验证**：`sudo -n true` 通过；`sudo -n id` 返回 `uid=0(root)`。sudoers 全量语法验证通过。

10. **origin-al 保留状态**：账号、密码登录能力和原有组均保留，未删除、未改密码、未降权；最终 `id origin-al` 正常。

11. **分区**：RAID 逻辑盘 `/dev/sdb` 约 7 TiB（Lenovo RAID 540-8i）；`/boot/efi` 约 1.1 GiB vfat、`/boot` 2 GiB ext4、`/` 196 GiB ext4。`/dev/sda` 是 14.5 GiB 可移动 USB `OnlyDisk`，始终未操作。

12. **LVM**：单 PV `/dev/sdb4`、VG `vg_h100`；LV 为 `lv_swap=64GiB`、`lv_docker=700GiB`、`lv_platform=5.20TiB`。未重新分区、格式化、缩容、删除 LVM 或修改 RAID。

13. **XFS**：`/var/lib/docker` 与 `/srv/gpu-platform` 是独立 XFS，均为 `ftype=1`、`projid32bit=1`，实时挂载含 `noatime,prjquota`；`findmnt --verify --verbose` 无错误或警告。

14. **project quota**：两个 XFS 的 project accounting/enforcement 均为 `ON`。codexops project 为 `h100_codexops` / ID `10001`，目录映射已登记。

15. **VG 剩余空间**：`vg_h100` 最终空闲 `860.70 GiB`。

16. **CPU**：2 × AMD EPYC 9555，2 sockets × 64 cores × 2 threads，共 128 物理核、256 逻辑 CPU；2 个 NUMA 节点。

17. **RAM**：系统总内存约 `498 GiB`；最终验收时约 `489 GiB` available。Swap `64 GiB`，使用量为 0。Slurm 配置 `RealMemory=486377 MiB`，为宿主保留 `24576 MiB`。

18. **4 张 H100**：四张 NVIDIA H100 PCIe 均由 PCIe、NVIDIA 驱动、NVML、Docker、DCGM 和 Slurm NVML 自动发现；每卡 `81559 MiB`。最终空闲温度均 31°C，功耗约 49–52 W。

19. **GPU UUID**：四个 UUID 与 PCI Bus ID 见下方 GPU 表；驱动、DCGM、Docker 和 Slurm 测试记录一致。

20. **NVIDIA 驱动**：NVIDIA 官方 Ubuntu 26.04 仓库的 Open Kernel Module `595.91.07`；Secure Boot disabled；NVML/内核模块版本一致；`nvidia-persistenced` enabled/active。当前启动 Xid、致命/不可纠正 AER、driver/library mismatch 均为 0，四卡 volatile/aggregate DRAM ECC 均为 0。

21. **MIG 当前状态**：四卡均为 `Disabled`，未启用、未创建 GPU Instance 或 Compute Instance。

22. **MIG 支持 profile**：只读查询确认 `1g.10gb`、`1g.10gb+me`、`1g.20gb`、`2g.20gb`、`3g.40gb`、`4g.40gb`、`7g.80gb`；因 MIG disabled，compute-instance 查询正确返回无 MIG-enabled devices。

23. **Docker**：Docker Engine/CLI `29.7.1`、containerd `2.2.6`、runc `1.3.6`、Buildx `0.36.0`、Compose `5.4.0`，来源为 Docker 官方 Ubuntu APT 仓库。Root Dir 为 `/var/lib/docker`；local 日志轮转和 live-restore 生效；默认 runtime 为 runc；未启用 userns-remap、Docker TCP API 或普通用户 daemon 权限。

24. **NVIDIA Container Toolkit**：`1.19.1`，NVIDIA runtime 和 CDI 设备正常，来源为 NVIDIA 官方 libnvidia-container 仓库。

25. **Docker GPU 测试**：固定镜像 `nvcr.io/nvidia/cuda@sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a`；`--gpus all` 精确看到 4 卡，`--gpus device=0` 精确看到 GPU 0；无 privileged、host network 或额外 capabilities。

26. **DCGM**：Host DCGM `4.6.1`、驱动 `595.91.07`；发现 4 个 Active H100；短诊断 `dcgmi diag -r 1` 的 Deployment 和 GPU 0–3 均 Pass，未运行长压力测试。

27. **Slurm 版本**：SchedMD 官方源码 `25.11.7` 构建的同版本 Debian 包；所有组件和开发 headers 一致，未混装其他 Slurm 来源。

28. **Slurm 节点配置**：Cluster `h100`，单节点 `sagsh100server`；分区 `notebook`（默认、8h）和 `train`（7d）；节点最终 `IDLE+DRAIN`，原因 `platform bootstrap complete`，队列为空。

29. **CPU 拓扑**：配置完全来自 `slurmd -C`：`CPUs=256 Boards=1 SocketsPerBoard=2 CoresPerSocket=64 ThreadsPerCore=2`，未手工猜测。

30. **GRES**：`Gres=gpu:h100:4`，`AutoDetect=nvml`；实际 NVML 名称 `nvidia_h100_pcie`，经类型子串匹配验证；`scontrol` 最终显示 4 个 GPU TRES。

31. **cgroup v2**：`CgroupPlugin=cgroup/v2`，`ProctrackType=proctrack/cgroup`，`TaskPlugin=task/cgroup,task/affinity`，`JobAcctGatherType=jobacct_gather/cgroup`。

32. **ConstrainDevices**：`ConstrainCores=yes`、`ConstrainRAMSpace=yes`、`ConstrainSwapSpace=yes`、`ConstrainDevices=yes`；管理员作业中观察到 cgroup v2 作业路径与正确 GPU 可见性。

33. **SlurmDBD**：MariaDB、MUNGE、slurmdbd、slurmctld、slurmd 均 enabled/active；稳定启动后无 error/fatal/plugin mismatch。数据库密码只在 `/etc/slurm/slurmdbd.conf`，元数据为 `slurm:slurm 0600`，从未输出。MUNGE key 为 `munge:munge 0400`，从未输出。

34. **Accounting**：Cluster `h100`、Account `company` 与 `platform-admin`、codexops association 和 Administrator AdminLevel 均存在；作业 1–3 均记录为 `COMPLETED 0:0`。

35. **QOS**：`general` 每用户最多 1 GPU、`core` 最多 2 GPU、`admin` 最多 4 GPU；codexops 默认 account/QOS 为 `platform-admin/admin`。

36. **Enroot**：官方 tag `v4.2.1`（commit `ead3a25ed974948235d28f99fc387ac82435ce62`）；按 UID 隔离 cache/data/runtime 到 `/srv/gpu-platform/enroot/*/${UID}`，每用户目录 0700；系统 AppArmor 限制未弱化。

37. **Pyxis**：NVIDIA/pyxis 官方 tag `v0.24.0`，精确针对 `slurm-smd-dev 25.11.7-1h100.1` 构建；插件 `/usr/local/lib/slurm/spank_pyxis.so` SHA-256 为 `72071897de9c4f34fa92f9414728954be6027f9d3a5ea0427b39ca074747edd9`；无 incompatible plugin version。

38. **单 GPU 作业测试**：Job 2 使用固定 CUDA digest，通过 Pyxis/Enroot 只看到 1 卡，UUID 为 `GPU-c8377945-df2c-5761-8798-66385611808b`；作业、squeue、sacct、cgroup 和释放检查均通过。

39. **双 GPU 作业测试**：Job 3 只看到 GPU 0 和 1 两个 UUID；`COMPLETED 0:0`，结束后无 GPU 进程。CPU Job 1 同样完成。临时 RESUME 得到明确批准，测试后立即重新 DRAIN。

40. **Prometheus**：`3.13.2`，固定 digest `sha256:1147c92841726a6fef55fe6124491d6f85480f8de204f7d420304ca5bbd0a8f7`；ready 通过，Prometheus/Node/DCGM 三个 target 全部 UP，只监听 `127.0.0.1:9090`。

41. **Grafana**：OSS `13.1.1`，health 显示 database `ok`；只监听 `127.0.0.1:3000`。随机管理员密码仅在 `/srv/gpu-platform/platform/secrets/monitoring.env`（`root:root 0600`）。管理员需要时可自行运行 `sudo sed -n 's/^GF_SECURITY_ADMIN_PASSWORD=//p' /srv/gpu-platform/platform/secrets/monitoring.env`；本次部署未读取或输出密码。

42. **DCGM Exporter**：`4.6.0-4.8.3`，固定 NVIDIA digest `sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a`；只监听 `127.0.0.1:9400`，导出 4 个 UUID 以及利用率、显存、温度、功耗、ECC、PCIe 和 MIG 状态指标。

43. **Node Exporter**：`1.12.1`，`node-exporter.service` enabled/active，只监听 `127.0.0.1:9100`，metrics probe 通过。

44. **长期 Docker 镜像**：Canonical Ubuntu 24.04 固定基础 digest `sha256:019e8eb29a85e74d64925745884f2ec79aa27e3feab36353d24656f4d6b89467`；本地镜像 `h100-local/dev-container:ubuntu24.04-codexops-20260804`，ID `sha256:0003a26a1bfc1f4109440039e91bb9d28603f8ff1bc56946eed3bfd4cda75577`；未安装 Docker daemon/CLI 或宿主 NVIDIA driver。

45. **codexops 测试容器**：`gpu-dev-codexops` healthy；8 CPU、32 GiB RAM、4096 PIDs、8 GiB shm；`Privileged=false`、非 host network/PID/IPC、无 DeviceRequest、无 docker.sock；SSH 仅绑定 `10.82.36.1:22022`。重建后 SSH host key 与持久化文件哈希不变。

46. **XFS 300GB 测试配额**：`h100_codexops` hard limit 为 `314572800 KiB`（300 GiB），覆盖 `/srv/gpu-platform/users/codexops`；home/workspace/shared 独立持久化，容器重建不删除数据。

47. **当前监听端口**：见下表。监控、MariaDB 和 DCGM hostengine 均为 localhost；宿主 SSH 与 Slurm 6817–6819 按当前私有管理网模型监听；Docker TCP API 不存在。UFW 为 inactive，未手工配置 nftables；Docker 安装后存在 Docker 自己管理的标准 iptables-nft NAT/filter chains。

48. **Mellanox 错误统计**：`ens1f0np0`（ConnectX-6 Lx，10 Gb/s）最终 60 秒内 CRC 与 symbol error 均从 `4,014,522/4,014,515` 增至 `4,030,032/4,030,025`，各增 `15,510`，平均 `258.5/s`；discard 与 link-down 增量为 0。状态为 P0 OPEN；未修改速率、FEC、autoneg、固件、接口状态或交换机。

49. **systemd failed units**：只有 `systemd-networkd-wait-online.service` failed；管理 IP、默认路由、SSH 和 chrony 均正常。smartd 自身 active。

50. **所有未解决 P0/P1/P2**：P0—Mellanox CRC/symbol 持续增长，阻断正式 NFS、第二节点、跨节点 NCCL 和高速共享存储；P0—Docker Hub Registry 与 Auth 在最终复测中均 15 秒连接超时，阻断 Docker Hub pull，未修改 DNS/代理/镜像站。P1—四张 H100 在启动早期出现 PCI DOE mailbox timeout，当前无 Xid/AER/ECC/功能故障，需 Lenovo/NVIDIA/kernel 联合跟进。P2—networkd wait-online 启动超时；P2—内核检测 RDSEED32 异常后禁用对应 CPUID bit，建议 BIOS/微码跟进；P2—MegaRAID 后端盘不支持 smartd 的 ATA CHECK POWER STATUS，smartd 忽略 `-n` 但仍 active。部署期 slurmdbd 配置错误和 Docker veth 瞬时日志均已修复/结束，不列为开放故障。

51. **配置备份路径**：统一位于 `/srv/gpu-platform/platform/backups/`，包括 `fstab-20260804-050357`、`daemon.json-before-nvidia-ctk-20260804-050357`、`etc-slurm-before-pyxis-20260804-050357.tar`、`slurm-acct-db-before-associations-20260804-050357.sql`、`monitoring-20260804-214647-616799811/`、`codexops-quota-20260804-222616-939470440/`。SSH 另有 `/etc/ssh/sshd_config.backup-20260804-045806` 和 `/etc/ssh/sshd_config.d.backup-20260804-045806`。备份未提交 Git。

52. **Git 仓库路径**：服务端 `/srv/gpu-platform/platform`；本地镜像 `/home/origin-al/Code/H100RemoteSSH/h100-platform-bootstrap`。Secrets、backups、raw reports、build/artifacts、logs、authorized_keys、MUNGE key、slurmdbd.conf 等均被忽略。

53. **Git commit**：已审计配置基线提交为 `8a52753f213eb79d857112c44cb1ba087a146d64`，提交信息 `Bootstrap H100 platform from fresh installation`。提交前 `git diff --cached --check` 通过，125 个文件中无禁用路径、私钥标记或已知 token 签名。本报告与最终审计脚本将作为收尾提交保存，最终 HEAD 记录在交付消息中。

54. **回滚说明**：回滚前先保持节点 DRAIN 并确认无作业；按组件从上述时间戳备份恢复，分别用 `sshd -t`、`findmnt --verify`、`jq empty`/`dockerd --validate`、Slurm bounded validation 验证后再 reload/restart。fstab、驱动/kernel 的回滚若需重启必须重新审批；数据库备份为 root-only secret-bearing 文件，不得显示或提交；监控可先停止 Compose 再恢复配置；长期容器删除默认保留 `/srv/gpu-platform/users/codexops` 数据。始终保留 origin-al 会话作为恢复路径，禁止操作 `/dev/sda` USB。

55. **后续管理员需要确认的事项**：先处理 Mellanox 物理链路和 Docker Hub egress；向 Lenovo/NVIDIA/kernel 团队提交 PCI DOE、RDSEED 和 smartd 兼容性记录；审批前不得启用 NFS/第二节点/跨节点 NCCL/高速共享存储；Slurm 继续 DRAIN，正式 RESUME 需单独批准；MIG 启用/实例创建、UFW/nftables、监控对外开放、新用户及其 Docker、JumpServer/Open OnDemand 均需后续明确批准。长期 Docker 到 Slurm 当前推荐用户直接 SSH 登录宿主提交，详见 `docs/long-lived-docker-to-slurm-security.md`。

## GPU 最终表

| GPU | UUID | PCI Bus ID | 型号 | 显存 | 温度 | 功耗 | ECC | Persistence | Compute Mode | MIG Mode |
|---:|---|---|---|---:|---:|---:|---|---|---|---|
| 0 | `GPU-c8377945-df2c-5761-8798-66385611808b` | `00000000:01:00.0` | NVIDIA H100 PCIe | 81559 MiB | 31°C | 48.77 W | enabled；volatile/aggregate corrected/uncorrected DRAM 均 0 | Enabled | Default | Disabled |
| 1 | `GPU-992f38cb-b919-5d6c-ddba-b8c1b2f771a9` | `00000000:71:00.0` | NVIDIA H100 PCIe | 81559 MiB | 31°C | 51.56 W | enabled；volatile/aggregate corrected/uncorrected DRAM 均 0 | Enabled | Default | Disabled |
| 2 | `GPU-cb103ce8-4672-873d-1f87-6d7c5ad771b2` | `00000000:81:00.0` | NVIDIA H100 PCIe | 81559 MiB | 31°C | 49.39 W | enabled；volatile/aggregate corrected/uncorrected DRAM 均 0 | Enabled | Default | Disabled |
| 3 | `GPU-873d76ca-5a84-5177-7ef1-9fdbc98c540f` | `00000000:F1:00.0` | NVIDIA H100 PCIe | 81559 MiB | 31°C | 50.19 W | enabled；volatile/aggregate corrected/uncorrected DRAM 均 0 | Enabled | Default | Disabled |

## 当前入站监听摘要

| 地址 | 端口 | 服务 | 可见性/用途 |
|---|---:|---|---|
| `0.0.0.0`, `[::]` | 22 | OpenSSH | 当前主机接口；仅应由公司管理网/未来跳板访问 |
| `10.82.36.1` | 22022 | gpu-dev-codexops SSH | 私有管理网测试容器 |
| `127.0.0.1` | 3000 | Grafana | localhost；使用 SSH tunnel |
| `127.0.0.1` | 9090 | Prometheus | localhost |
| `127.0.0.1` | 9100 | Node Exporter | localhost |
| `127.0.0.1` | 9400 | DCGM Exporter | localhost |
| `127.0.0.1` | 5555 | DCGM hostengine | localhost |
| `127.0.0.1` | 3306 | MariaDB | localhost |
| `0.0.0.0` | 6817–6819 | Slurm | 当前单机/私有管理网；第二节点前需审批访问策略 |

## 强制终态

```text
PLATFORM BASELINE DEPLOYED
SLURM NODE REMAINS DRAINED
MIG NOT MODIFIED
FIREWALL NOT MODIFIED
JUMPSERVER NOT DEPLOYED
OPEN ONDEMAND NOT DEPLOYED
OTHER USERS NOT CREATED
```
