# H100 Portal 架构

## 部署边界

Portal 是单节点、受控虚拟网络内的管理控制面。经管理员批准，Web 精确监听服务器
`tun0` 地址 `10.10.10.2:18080`；API 仍只监听 `127.0.0.1:18081`，PostgreSQL 仍只
使用本机 Unix Socket。浏览器从批准的 `10.10.10.0/24` 虚拟网络直接访问 Web，SSH
Tunnel 仅为可选回退。管理员已接受当前 Pilot 使用内部明文 HTTP；TLS 未启用且当前范围
不要求启用。Portal 不修改防火墙、不监听 `0.0.0.0`、不提供公网入口，也不把当前入口
描述为 HTTPS。

```text
浏览器（批准的 10.10.10.0/24 虚拟网络；内部 HTTP；SSH Tunnel 可选）
        |
        v
Next.js Web 10.10.10.2:18080 -- /api/* --> FastAPI 127.0.0.1:18081
                                      |
                   +------------------+------------------+
                   |                                     |
                   v                                     v
       PostgreSQL Unix Socket                 worker.sock (0660)
                                                         |
                                                         v
                                               Root Worker (root)
                                                         |
                         +-------------------------------+----------------+
                         |                               |                |
                   固定只读适配器                 固定 dry-run handler   既有受控脚本
```

## 组件职责

- `apps/web`：中文控制台、表格、筛选、表单和任务状态轮询。它不执行宿主命令。
  SSH key pair 生成只发生在浏览器；Web bundle 不把 private key 发送给任何服务。
- `apps/api`：认证、会话、CSRF、RBAC、审批状态机、审计、数据库事务以及 Worker
  客户端。API 以非 root 系统账号运行，不能访问 Docker Socket、MUNGE key、
  Linux shadow 或 SlurmDBD 密码。
- `apps/worker`：唯一 root 组件。它通过 systemd 管理的 Unix Domain Socket 接收
  限长、定版 JSON；使用对端凭据校验 API UID；重新验证固定 schema；只分派固定
  operation type。
- PostgreSQL：独立的 `h100_portal` 数据库和 `h100_portal` 数据库角色，与 SlurmDBD
  MariaDB 和 Grafana 完全分离。

## 运行身份

`h100-portal-web`、`h100-portal-api`、`h100-portal-worker` 均为无密码、无交互 shell
的系统账号，不加入 `docker`、`sudo`、`video`、`render` 或
`gpu-platform-admin`。Worker unit 因宿主查询需要以 `root:root` 运行；
`h100-portal-worker` 账号仅用于明确保留组件身份，不授予权限。

## 数据读取

Worker 优先调用结构化接口：Slurm JSON/parsable2、`nvidia-smi --query-gpu` CSV、
Docker JSON inspect 的字段白名单、LVM JSON 及 Prometheus localhost API。每次调用
均有固定绝对路径、固定 argv、固定 PATH、超时、最大输出和结构化错误码。异常返回
`UNKNOWN` 或 partial failure，不以 `0` 冒充健康状态。

GPU 数据同时保留 NVML index、UUID、PCI Bus ID、Linux minor 与 device path。minor
来自 NVIDIA 驱动 `/proc/driver/nvidia/gpus/*/information`，按 PCI Bus 合并并复核 UUID；
任何代码都不得推断 NVML index 等于 Linux minor。

## 写操作

HTTP 请求只创建 Operation，不等待宿主操作完成。危险操作需要对象名二次确认、
最近十分钟重新认证和审批。API 通过 Worker Socket 提交已审批任务；Worker 再检查
对象、参数、当前状态、脚本 owner/mode/hash 与幂等键。

Portal-3C 已只对精确 `origin-pilot` 参数开放并完成一次真实 `user.stage`；该路径保持
hash、actor、approval、payload 和幂等键绑定。Portal-3D-R 增加自助 public-key record：
API 保存 canonical public key/metadata，Worker 在 root-only 目录准备 UUID `.pub` 和绑定
sidecar。Portal-3E-FINAL 为唯一重新批准的 `origin-pilot` Operation 开放真实
`user.activate`：固定绑定 dry-run、managed-user/key UUID、fingerprint、approval、actor
和幂等键；Worker 重跑 STAGED/SSH policy/资源 Gate 后，才通过受控脚本安装两处公钥、
切换 shell、启动无 GPU 容器并重新读取完整后置条件。失败走固定回滚并恢复 STAGED。

连接页还定义了 Activate 后的专用“启动我的受管容器”路径：只有资源所有者的计算身份为
ACTIVE、适用 CONTAINER Key 为 INSTALLED、容器为安全的 STOPPED/GPU NONE 时才显示。
API 从数据库生成闭合 Worker payload；Worker 再核对 root-owned 生命周期状态、持久
authorized_keys fingerprint、精确资源/挂载和脚本 hash。失败或后置条件不满足时固定停止
容器。该路径不接受通用 Docker 参数，也不使当前 STAGED 用户可启动容器。

其他用户 Activate、`quota.update`、`slurm.resume` 等仍不会执行。Slurm 保持 DRAIN，
客户端 SSH 验证必须由持有对应私钥的用户完成。

## 现阶段基础设施约束

- 单节点；无 NFS、无第二节点、无跨节点 NCCL。
- MIG Disabled，Portal 不提供 MIG 或 GPU reset 操作。
- Docker Hub 为 DEFERRED/RESTRICTED；不依赖其运行时可用性。
- Mellanox 物理网络 P0 由管理员接受为单节点 Pilot 风险。
- PCI DOE 为 P1 观察项。
