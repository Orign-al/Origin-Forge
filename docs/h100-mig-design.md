# H100 单机 MIG 设计草案

状态：仅设计。四张 H100 当前 MIG Mode 均为 Disabled；本阶段不启用 MIG、不创建 GPU Instance 或 Compute Instance，也不修改 Slurm GRES。

## 已确认能力

只读能力查询已看到 H100 支持的 profile 包括 `1g.10gb`、`1g.10gb+me`、`1g.20gb`、`2g.20gb`、`3g.40gb`、`4g.40gb`、`7g.80gb`。最终可组合布局必须在维护窗口再次用本机 `nvidia-smi mig -lgip/-lcip` 验证，不能仅按 profile 名称推断可同时创建的组合。

## 推荐演进

Pilot 初期继续使用 4 张整卡 H100。只有在整卡队列利用率、任务显存需求和隔离需求形成稳定数据后，才评估混合布局。候选方向是保留部分 GPU 为整卡、只在指定 GPU 上启用 MIG，并为整卡与 MIG 资源建立清晰的 Slurm 分区/QOS 边界。

同一物理 GPU 不能同时作为完整 GPU 和 MIG 资源登记给 Slurm；固定 GPU Docker 也不能与 Slurm 重复登记同一资源。

## 变更 Gate

实施前必须：

1. 获得管理员单独批准“允许启用 MIG”以及随后独立的实例布局批准；
2. 停止相关长期 GPU 服务，确认 `squeue` 为空并将节点 DRAIN；
3. 记录 GPU UUID、PCI 地址、驱动、VBIOS、MIG mode 和支持 profile；
4. 备份 `slurm.conf`、`gres.conf`、cgroup 配置和监控配置；
5. 明确是否需要 GPU reset、驱动重载或 reboot，并分别取得相应批准；
6. 为每个候选布局制定回滚到四张整卡的步骤。

## Slurm 与隔离验证

- 由 NVML/Slurm 重新发现 MIG 资源，不手工猜测设备节点；
- GRES 类型和数量必须与实际 GI/CI 一致；
- cgroup v2 与 `ConstrainDevices=yes` 必须限制到分配的 MIG 设备；
- Pyxis/Enroot 单实例、并发实例和超额请求都要测试；
- 验证作业内只看到获分配的 MIG UUID，不能看到其他 MIG 或整卡；
- `sacct` 必须正确记录请求与分配的 TRES；
- DCGM/Prometheus 必须能按 GPU/MIG 实例观察健康与利用率；
- 回归整卡单 GPU、双 GPU 作业，确保未参与 MIG 的卡不退化。

## 暂不实施原因

当前受控 Pilot 的主要阻塞不是 GPU 切分，而是已延期的 Mellanox 物理 P0 和 Docker Hub 受限路径。启用 MIG 会增加调度、设备隔离和运维复杂度，不能与本阶段网络风险收尾混合实施。
