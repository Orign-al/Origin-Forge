# Mellanox PHY Prometheus textfile collector 设计

状态：**设计、shellcheck、手动采集、promtool 和 unit 解析均已通过；未安装。**

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

脚本设置受控 `PATH=/usr/sbin:/usr/bin:/sbin:/bin`。实测 `nobody` 可以读取该接口的 `ethtool -S`，因此候选 service 以 root 执行但将 capability bounding/ambient sets 设为空，不保留 `CAP_NET_ADMIN`。service 不访问 Docker Socket。

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

Phase P0-B 已经管理员授权，从 Ubuntu 26.04 `resolute/universe` 官方仓库安装 `shellcheck 0.11.0-2`。以下预部署检查已通过：

- `bash -n`；
- `shellcheck 0.11.0`；
- 临时目录手动采集得到 5 HELP、5 TYPE、5 sample；
- 现有 Prometheus 容器内 `promtool check metrics`；
- Node Exporter 1.12.1 支持 `--collector.textfile.directory`；
- 候选 service/timer 的 `systemd-analyze verify`。

剩余未满足条件只有管理员明确批准生产部署。当前 Node Exporter ExecStart 未改变，生产 `/metrics` 中没有 `h100_mellanox_*`，所有系统安装目标均不存在。

## 建议告警（确认指标存在后再部署）

```promql
increase(h100_mellanox_rx_crc_errors_phy_total[5m]) > 0
increase(h100_mellanox_rx_symbol_err_phy_total[5m]) > 0
increase(h100_mellanox_link_down_events_phy_total[5m]) > 0
```

不应在指标首次出现前部署这些规则；重启或 NIC 计数复位也需结合 `resets()` 区分。
