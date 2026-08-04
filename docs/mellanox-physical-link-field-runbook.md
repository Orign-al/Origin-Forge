# Mellanox 物理链路现场排障单

适用主机：`sagsh100server`
管理地址：`10.82.36.1`
管理/故障接口：`ens1f0np0`
服务器端口：Mellanox ConnectX-6 Lx `0000:61:00.0`
已发现的交换机邻居：`HKL-center-S6510-2VSU`，端口 `TFGigabitEthernet 1/0/5`

## 安全边界

- 管理流量依赖当前故障链路。现场操作前必须准备带外控制台或确认可接受 SSH 中断。
- 每次只改变一个变量，记录时间、操作者、部件厂商/型号/序列号和操作前后计数。
- 不清零 NIC 或交换机计数。不得在服务器远程执行 link down/up、`ethtool -s`、速率、双工、自动协商或 FEC 修改。
- 不升级 NIC firmware，不修改 netplan、MTU、防火墙、MIG 或 Slurm 状态。
- 每一步结束后，使用同一采样脚本观察 5 分钟；最终候选修复需在正常业务流量下复测至少 10 分钟。

统一 5 分钟采样命令（由 `codexops` 在服务器执行）：

```bash
cd /srv/gpu-platform/platform
RUN_ID=<本阶段运行编号> \
SAMPLE_COUNT=6 \
INTERVAL_SECONDS=60 \
scripts/47-sample-mellanox-link.sh
```

正式 10 分钟复测使用 `SAMPLE_COUNT=11`。该脚本只读统计，不清零计数，不改变链路参数。

## 现场操作顺序

### Step 1：清洁和重新插拔连接器

- 清洁并重新插拔服务器侧和交换机侧连接器。
- 不改变模块、跳纤、服务器端口或交换机端口中的其他变量。
- 恢复链路后重新采样 5 分钟。

### Step 2：更换链路介质

- 更换已知正常且与 10GBASE-SR/850nm/LC 链路相容的光纤、DAC 或 AOC；本机当前模块为 10GBASE-SR，因此不得混用不相容介质。
- 保持两端端口和模块不变。
- 重新采样 5 分钟。

### Step 3：更换服务器侧光模块

- 更换为已知正常、与 NIC、介质、速率和远端模块相容的服务器侧光模块。
- 保持交换机侧模块、端口和链路介质不变。
- 重新采样 5 分钟。

### Step 4：更换交换机侧光模块

- 更换为已知正常且相容的交换机侧光模块。
- 保持服务器侧模块、服务器端口和链路介质不变。
- 重新采样 5 分钟。

### Step 5：切换交换机端口

- 保持服务器端口、服务器侧模块和链路介质不变，切换到已知正常且配置等价的交换机端口。
- 此操作必须由网络管理员执行，并需管理员明确批准。
- 重新采样 5 分钟。

### Step 6：核对交换机侧数据

网络管理员对照每次服务器采样时间，导出以下只读信息：

- CRC/FCS、symbol error、input error、discard；
- link flap/link-down event；
- speed、duplex、autoneg、FEC；
- optic Rx/Tx power、温度、电压及 alarm/warning；
- 端口累计计数和每个采样区间的增量。

单侧 `FEC Active: Off` 或单侧光功率不能独立证明配置错误。必须结合两端能力、模块规格和交换机侧数据判断。

### Step 7：评估另一端口或 NIC 硬件

- 只有 Step 1–6 均未消除错误，才评估服务器另一 Mellanox 端口或 NIC 硬件故障。
- 更换服务器网卡端口属于审批操作；不得由 Codex 远程执行。
- 切换后仍需按相同口径采样，不得因累计值不同而跳过增量比较。

## 验收与记录

候选修复在正常业务流量下连续 10 分钟应满足：

- `crc_delta = 0`；
- `symbol_delta = 0`；
- `link_down_delta = 0`；
- 无新增 carrier change、uncorrected error；
- Registry/Auth/CDN 的轻量连接测试稳定。

若仍有持续增量，状态保持 `NETWORK PHYSICAL P0 OPEN`。若 10 分钟无新增错误，可记录 `NETWORK PHYSICAL RETEST PASSED`，但继续监控至少 24 小时。

每一步填写：

| 项目 | 记录 |
|---|---|
| 操作步骤及时间 | 待现场填写 |
| 操作者 | 待现场填写 |
| 更换部件及序列号 | 待现场填写；未更换写“无” |
| 交换机端口 | 待网络团队填写 |
| 操作前 CRC/symbol | 待填写 |
| 操作后 5/10 分钟增量 | 待填写 |
| 结论/是否进入下一步 | 待填写 |
