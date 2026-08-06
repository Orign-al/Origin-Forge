# Portal 安全事件响应

## 适用事件

包括网页登录异常、token/session 泄露、数据库或配置泄露、Root Worker 拒绝异常、脚本
hash 变化、未知监听、审计缺口、RBAC/CSRF 绕过、生产页面伪造健康及未授权宿主变更。

## 立即处置

1. 记录 UTC 和服务器本地时间、值班人、现有 Slurm state/reason 与运行作业。
2. 不覆盖重要 DRAIN reason；Portal 不自动 RESUME。
3. 停止 `h100-portal-web` 和 `h100-portal-api`；怀疑 Worker 时同时停止 Worker socket/service。
4. 不修改防火墙、MIG、NVIDIA Driver、Kernel、Mellanox 或现有 GPU 隔离来“快速修复”。
5. 保存 journal、审计、unit、二进制/脚本 hash、Git status 和数据库一致性备份。

若发现实际宿主写入或 Worker 完整性失败，节点应保持/进入 DRAIN；禁止自动删除策略、结束
用户进程或清理用户数据。当前节点本已 DRAIN，优先保留原安全 reason。

## 凭据事件

- setup token：撤销所有未使用 token，重新签发时只在当前终端显示一次。
- session：撤销全部 Portal session，轮换 Portal secret 后要求重新登录。
- 网页密码：创建一次性重置流程，不读取或修改 Linux/SSH 密码。
- 数据库凭据：轮换独立 `h100_portal` 角色密码，更新 `portal.env`，不触碰 SlurmDBD。

不要把可疑密码、token、Cookie、私钥或完整请求正文复制进工单或报告。

## Worker 事件

核对 socket owner/group/mode、父目录遍历权限、API UID peer credential、unit sandbox、固定
operation schema和 `/etc/h100-portal/worker-scripts.json`。任何脚本 owner/hash/mode 不匹配
都按 fail-closed 处理；不得临时关闭 hash 检查或放宽 socket。

确认 API 未加入 docker/sudo/video/render/gpu-platform-admin 组，未获得 Docker Socket、
MUNGE key、shadow 或任意 shell/argv/path 能力。

## 恢复上线门槛

- 根因、影响范围和时间线已确认；
- 恶意/错误会话与 token 已撤销，必要凭据已轮换；
- Ruff/MyPy/Pytest、前端 lint/typecheck/test/build、Playwright、systemd verify 均通过；
- Worker smoke、Web 私有地址/API localhost 精确监听、PostgreSQL Unix Socket 和审计链路通过；
- GPU 4/4、DCGM、Docker、Prometheus/Grafana 与 systemd 回归通过；
- Slurm 仍 DRAIN、队列为空，且没有自动创建 Pilot 用户或容器。

恢复 Portal 服务与恢复 Slurm 调度是两项独立审批。Portal 事件关闭绝不隐含 Slurm RESUME。

## 已知但未由 Portal 修复的风险

- NETWORK PHYSICAL P0：管理员接受单节点 Pilot 风险；
- Docker Hub connectivity：DEFERRED，必须使用替代 Registry；
- PCI DOE：P1 observation；
- 无 NFS、无第二节点、无跨节点 NCCL/RDMA 生产能力。
