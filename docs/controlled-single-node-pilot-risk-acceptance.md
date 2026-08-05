# H100 受控单机 Pilot 风险确认

## 适用范围

本文件只适用于第一批最多 2–3 名公司内部 Pilot 用户。只允许单机作业、整卡 H100 调度和本机 XFS 工作目录；不代表平台已全面生产就绪。

## 管理员接受但未关闭的风险

```text
NETWORK PHYSICAL P0:
DEFERRED BY ADMINISTRATOR — RISK ACCEPTED FOR SINGLE-NODE PILOT
```

Mellanox `ens1f0np0` 的 CRC/symbol error 仍持续增长，且管理 SSH 与外部访问依赖该接口。可能出现丢包、重传、超时或断连。collector 与告警只能观测风险，不能修复物理链路。

因此 Pilot 期间禁止：

- NFS、SMB 共享工作目录和高速共享存储；
- 第二 GPU 节点、多节点 Slurm和跨节点 NCCL；
- RDMA/RoCE 生产验收；
- 大规模外部数据迁移或同步；
- 依赖网络的高可用控制面；
- 宣布平台全面生产就绪。

Docker Hub 状态为：

```text
DOCKER HUB CONNECTIVITY:
DEFERRED — ALTERNATIVE REGISTRIES REQUIRED
```

Pilot 只使用已缓存的本地镜像、NVIDIA NGC、GHCR、Quay，或管理员离线导入并记录 digest 的 archive；不得依赖 Docker Hub 实时拉取。

## Pilot 准入条件

- 四张 H100 均可见，无 Xid、AER error 或掉卡，DCGM short diagnostic 通过；
- Slurm、Pyxis、Enroot 已有整卡单 GPU 与双 GPU 管理员验收证据；
- XFS project quota 有效，每个普通用户默认 300GB；
- 长期开发容器默认无 GPU、无 Docker Socket、非 privileged、非 host network/PID/IPC；
- 容器具有 CPU、内存和 PIDs 限制，用户不属于宿主 docker 组；
- Prometheus、Grafana、DCGM Exporter、Node Exporter 正常；
- Mellanox collector timer 正常，CRC、symbol 和 link-down 规则已加载；
- 没有新增未批准的公网监听；
- Pilot 用户已阅读本文件，并确认关键数据的异机备份责任。

## Pilot 运行约束

- 首批最多 2–3 人，不创建超出审批范围的账号或容器；
- 只运行单节点任务，不启用 MIG；
- 不进行大规模外部下载；
- 重要训练结果及时归档到管理员批准的异机位置；当前本机平台不承诺正式异机备份；
- 每日检查 CRC、symbol、link-down 规则以及 `h100_mellanox_*` 指标；
- 出现 link-down、持续 SSH 不稳定、GPU Xid/AER/掉卡、监控退化或数据完整性异常时，暂停 Pilot 并升级处理。

## 审批 Gate

在管理员明确回复以下原文前，Slurm 节点必须继续 DRAIN：

```text
允许 Slurm RESUME 并开始受控单机 Pilot
```

该批准只允许单节点 Pilot，不解除网络、MIG、NFS、第二节点或防火墙的独立审批边界。
