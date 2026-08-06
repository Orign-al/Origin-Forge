# Portal-2 真实数据与前端验收

Portal-2 延续私有网络控制面边界：Web 监听管理员批准的 10.10.10.2:18080，API 只监听
127.0.0.1:18081，PostgreSQL 使用 Unix Socket，Root Worker 使用受权限控制的 Unix
Socket。Slurm 保持 DRAIN，所有宿主写操作只生成草稿、审批记录和 Worker dry-run。

## 真实数据来源

| 页面       | 主要来源                                                    | 失败语义                               |
| ---------- | ----------------------------------------------------------- | -------------------------------------- |
| 总览       | Slurm、NVML、Docker、df/vgs、Prometheus、Portal PostgreSQL  | PARTIAL / UNKNOWN                      |
| GPU        | nvidia-smi query、driver procfs、DCGM、内核日志、Slurm      | 不推断 minor，不把查询失败显示为 CLEAR |
| Slurm/作业 | scontrol JSON、squeue JSON、sacct -P、sacctmgr -P           | 当前队列与七天历史分开                 |
| 容器       | 固定 docker ps/inspect 白名单                               | 不暴露任意参数或原始环境               |
| 存储       | 固定 df/vgs/du、docker system df、XFS project quota         | 不接受任意路径                         |
| 镜像       | 本地 Docker digest 库存、固定 Registry endpoint 探测        | tag 不是唯一身份                       |
| 监控       | localhost Prometheus/Grafana、Guard、systemd、Docker、Slurm | 缺失数据不显示为 0                     |

GPU 物理身份由 UUID 与 PCI Bus ID 联结 NVIDIA driver procfs 的 minor；NVML index 仅作为
展示字段。页面同时保留 UUID、Bus ID、minor 和 /dev/nvidiaN。

## 安全与操作语义

- Origin-al 网页身份为 ACTIVE，计算资源身份保持 NOT_ENROLLED。
- user.plan(origin-al) 只允许创建 DRAFT；user.stage、user.activate、GPU 隔离和
  容器创建仍被 API 和 Worker 双层拒绝。
- DRAFT 需显式提交为 PENDING_APPROVAL，审批后也只调用 dry_run=true。
- Slurm RESUME 按钮保持禁用，Worker 的非 dry-run 写请求固定返回拒绝。
- 页面访问、token 消费、session 创建/撤销、草稿、审批模拟和 Worker 结果进入脱敏审计。

## 双分辨率截图

tests/capture_portal2_snapshot.py 由 root 在服务器上调用生产只读 adapters，并从数据库
只读取安全字段；它不输出数据库凭据、密码 hash、setup token、session、Cookie 或 SSH
key。Playwright 的 portal2-visual.spec.ts 使用同轮快照在隔离的 Next 测试服务中生成：

- 登录、总览、GPU、Slurm、用户详情、容器、存储、监控、审批、审计；
- 1366×768 和 1920×1080 各一组，共 20 张；
- 检查页面无横向布局溢出、无渐变、无 backdrop filter、卡片圆角不超过 8px，且不包含
  “AI 助手”“智能助手”或“AI 洞察”。

生产 API 另行通过 Worker Socket smoke 和 HTTP 健康检查验收；截图 fixture 不能代替生产
数据接入检查。
