# Mellanox textfile collector 候选测试

- Run ID：`20260805-013630`
- 测试时间：`2026-08-05T02:18:53+08:00`
- 测试位置：服务器临时目录 `/tmp/h100-mlx-metrics-test.IODibS`
- 生产安装：**未执行**
- Node Exporter 配置/进程：**未修改、未重启**

## 检查结果

| 检查 | 结果 |
|---|---|
| `bash -n` | 通过 |
| `shellcheck` | 未执行：服务器和本地均未安装；没有为此扩大软件安装范围 |
| 手动只读 `ethtool -S ens1f0np0` | 通过 |
| 固定白名单字段 | 5/5 存在且为非负整数 |
| HELP/TYPE/sample 行数 | 5/5/5 |
| 现有 Prometheus 容器内 `promtool check metrics` | 通过 |
| systemd unit 解析 | 候选 unit 被解析；因生产脚本尚未安装到 `/usr/local/sbin`，按预期报告目标不存在，退出 1 |
| 现有 Node Exporter 健康 | `active/running`，`127.0.0.1:9100/metrics` 正常 |

测试快照计数：

| 指标 | 值 |
|---|---:|
| `h100_mellanox_rx_crc_errors_phy_total` | 4,503,227 |
| `h100_mellanox_rx_symbol_err_phy_total` | 4,503,220 |
| `h100_mellanox_link_down_events_phy_total` | 0 |
| `h100_mellanox_rx_discards_phy_total` | 0 |
| `h100_mellanox_tx_discards_phy_total` | 0 |

候选文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `scripts/h100-mellanox-metrics` | `85064058bb002327d33ce2ec97077163e5b8f11f0b13cd60cf749a8d9b0e2ff0` |
| `monitoring/h100-mellanox-metrics.service` | `4c90b4fb57918443a4ef4f0ee6dbdd3a1147807e6a8554c75200b3b512128512` |
| `monitoring/h100-mellanox-metrics.timer` | `3567fab348301fd93224cb70c86e7052e20b125c98fc41d4c64c4d52cb4c6358` |
| `monitoring/node-exporter-textfile-collector.conf` | `8e45560332e7ab1c75b7f03cfb31dcc930f4ff15047a11c2206088db0a3a1223` |

## 安装 Gate

安装仍被阻止，直到管理员明确批准，且正式安装前补齐 `shellcheck`。当前没有创建 textfile 目录，没有安装 `/usr/local/sbin/h100-mellanox-metrics`，没有安装/启用 timer，也没有部署告警规则。
