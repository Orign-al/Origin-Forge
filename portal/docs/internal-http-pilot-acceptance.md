# Pilot 内部 HTTP 管理员接受记录

## 正式状态

```text
PORTAL ACCESS MODE:
INTERNAL HTTP ACCEPTED BY ADMINISTRATOR — VIRTUAL NETWORK ONLY

TRANSPORT TLS:
NOT ENABLED — NOT REQUIRED FOR CURRENT PILOT SCOPE
```

Portal 当前通过 `http://10.10.10.2:18080/` 访问，地址及全部访问流量限定在受控虚拟
网络中。当前 Pilot 阶段接受内部明文 HTTP，不要求改回 SSH Tunnel，也不要求部署 HTTPS。
该项不再阻断后续 Portal 真实写操作，但仍须获得完整 Portal-3 执行批准。

## 持续边界

1. Web 只能监听批准的虚拟网络地址，不得监听公网地址或 `0.0.0.0`。
2. API 继续只监听 `127.0.0.1:18081`，PostgreSQL 继续使用 Unix Socket。
3. 不修改防火墙，不增加公网路由或端口映射。
4. 不在界面、文档或报告中声称已经启用 HTTPS。
5. Cookie 保持当前内部 HTTP 的 `Secure=false`，继续使用 HttpOnly、SameSite=Strict、
   精确 Origin/Referer、CSRF token、空闲/绝对超时和 session rotation。
6. 保留登录限速、账号锁定、RBAC、最近重新认证和审计。
7. 不修改 Linux/SSH 密码，不生成新的 Origin-al 密码设置链接，不撤销当前有效网页密码。
8. 访问扩展到其他网络、VPN 用户、办公网或公网时，必须重新评估并优先启用 HTTPS。

## Portal-3 Gate

在收到完整 Portal-3 执行批准前：

- Slurm 节点保持 DRAIN；
- 不创建 Pilot Linux 用户；
- 不执行 Origin-al 计算 onboarding；
- 不启用 GPU bypass Guard timer；
- 不执行真实 Worker 写操作；
- 不修改 MIG、防火墙、NFS 或节点拓扑。
