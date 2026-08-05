# Mellanox PHY Prometheus textfile collector 设计

状态：**已部署并通过运行态验收。**

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

## 已部署状态

管理员已明确批准生产部署。已完成：

1. 创建 `/var/lib/node_exporter/textfile_collector`；
2. 安装 `/usr/local/sbin/h100-mellanox-metrics`；
3. 安装 `h100-mellanox-metrics.service` 和每分钟 timer；
4. 为 Node Exporter 增加 textfile collector 参数并完成重启；
5. 把三条规则加载到 Prometheus。

运行态验证结果：

- `bash -n`、ShellCheck、`systemd-analyze verify` 和 `promtool` 均通过；
- timer 为 enabled/active，最近一次 oneshot `Result=success`、退出码 0；
- Node Exporter 仍只监听 `127.0.0.1:9100`；
- `/metrics` 暴露全部五个 `h100_mellanox_*` 指标；
- Prometheus 的 node-exporter、dcgm-exporter、prometheus targets 均为 up；
- CRC 与 symbol 规则因当前已知物理故障进入 firing；link-down 规则为 inactive/healthy；
- 回滚备份位于 `backups/p0b-20260805-032025/collector-20260805-120700/`。

Phase P0-B 已经管理员授权，从 Ubuntu 26.04 `resolute/universe` 官方仓库安装 `shellcheck 0.11.0-2`。以下预部署检查已通过：

- `bash -n`；
- `shellcheck 0.11.0`；
- 临时目录手动采集得到 5 HELP、5 TYPE、5 sample；
- 现有 Prometheus 容器内 `promtool check metrics`；
- Node Exporter 1.12.1 支持 `--collector.textfile.directory`；
- 候选 service/timer 的 `systemd-analyze verify`。

## 已部署告警

```promql
increase(h100_mellanox_rx_crc_errors_phy_total[5m]) > 0
increase(h100_mellanox_rx_symbol_err_phy_total[5m]) > 0
increase(h100_mellanox_link_down_events_phy_total[5m]) > 0
```

规则文件为 `monitoring/rules/h100-mellanox-alerts.yml`。重启或 NIC 计数复位仍需结合 `resets()` 和现场记录解释，不能把计数器复位误判为物理修复。
