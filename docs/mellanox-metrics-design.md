# Mellanox PHY Prometheus textfile collector 设计

状态：**仅完成设计和手动测试候选；未安装。**

## 文件与生产默认值

- 采集脚本：`scripts/h100-mellanox-metrics`
- systemd service：`monitoring/h100-mellanox-metrics.service`
- systemd timer：`monitoring/h100-mellanox-metrics.timer`
- Node Exporter drop-in 候选：`monitoring/node-exporter-textfile-collector.conf`
- 实际接口：`ens1f0np0`
- textfile 目录：`/var/lib/node_exporter/textfile_collector`
- 输出：`h100_mellanox.prom`
- 周期：60 秒

脚本只用 `ethtool -S ens1f0np0` 读取 NIC 统计。它使用固定字段白名单、10 秒超时、`flock` 和同目录临时文件加原子 `rename`。任何字段缺失或格式错误都会保留上一份有效指标，不会发布部分结果。

指标：

- `h100_mellanox_rx_crc_errors_phy_total`
- `h100_mellanox_rx_symbol_err_phy_total`
- `h100_mellanox_link_down_events_phy_total`
- `h100_mellanox_rx_discards_phy_total`
- `h100_mellanox_tx_discards_phy_total`

## 安装审批 Gate

安装会产生以下系统变更，因此当前未执行：

1. 创建 `/var/lib/node_exporter/textfile_collector`；
2. 安装 `/usr/local/sbin/h100-mellanox-metrics`；
3. 安装 service/timer；
4. 为现有 Node Exporter 增加 textfile collector 参数并重启 Node Exporter；
5. 启用 timer。

正式安装前必须：

- 时间戳备份现有 Node Exporter unit 和所有同名目标文件；
- `bash -n` 和 `shellcheck` 均通过；
- 在临时目录手动运行并验证 Prometheus text format；
- 确认 `/metrics` 出现全部五个指标；
- 确认现有 Prometheus target、Grafana、DCGM Exporter 未退化；
- 获得管理员明确批准。

当前环境没有 `shellcheck`。本阶段不为这一项扩大包安装范围，因此该检查仍是安装前的未满足条件。

## 建议告警（确认指标存在后再部署）

```promql
increase(h100_mellanox_rx_crc_errors_phy_total[5m]) > 0
increase(h100_mellanox_rx_symbol_err_phy_total[5m]) > 0
increase(h100_mellanox_link_down_events_phy_total[5m]) > 0
```

不应在指标首次出现前部署这些规则；重启或 NIC 计数复位也需结合 `resets()` 区分。
