# Portal-0/1 分阶段实施记录

本文件记录可提交的脱敏阶段结论。运行时证据保存在本轮 `reports/portal-<RUN_ID>`，
秘密仅保存在受限运行时配置中，不进入此文件。

## 阶段 A：调查和工程设计

- 开始：2026-08-06 00:39:17 +08:00
- 结束：2026-08-06 00:47:23 +08:00
- 起始 Git HEAD：`30752823e1fef0c4f20b5afabda7bf4a85ef7c21`
- 修改文件：本目录的架构、安全模型、依赖决策和阶段记录。
- 配置备份：未修改系统配置，因此无配置备份；已创建独立本轮 backup 目录。
- 静态检查：Markdown 路径检查通过；未发现密码、token、私钥或运行时 secret。
- 调查结果：Ubuntu 26.04 LTS；Python 3.14.4；Node/pnpm/PostgreSQL 初始均未安装；
  `origin-al` Linux 用户存在；18080/18081 空闲；Slurm 为 IDLE+DRAIN 且队列为空。
- 未解决问题：Docker Hub 受限、Mellanox 物理网络 P0、PCI DOE P1；均不在本阶段改变。

## 阶段 B：Monorepo、数据库和基础服务

- 开始：2026-08-06 00:47:24 +08:00
- 结束：2026-08-06 02:08:00 +08:00
- 修改文件：workspace/lockfile、Python package、Alembic migration、配置模板和基础 unit。
- 配置备份：保存 PostgreSQL `postgresql.conf` 与 `pg_hba.conf` 修改前副本；未备份或输出
  数据库随机密码。
- 结果：Node 24.19.0、pnpm 11.20.0、Python 3.14.4 venv、PostgreSQL 18.4；独立
  `h100_portal` 数据库/角色，18 张表，revision `dd26070f46bf`，仅 Unix Socket。
- 静态检查：锁文件、Ruff/MyPy、Alembic current/check 通过。
- 未解决问题：Ubuntu 报告 CPU microcode 更新待重启；本阶段禁止自动重启。

## 阶段 C：只读网页和平台数据接入

- 开始：2026-08-06 02:08:01 +08:00
- 结束：2026-08-06 02:29:59 +08:00
- 修改文件：Next.js 中文页面、UI package、API client、只读 routes 与 Worker adapters。
- 配置备份：未修改 Slurm/GRES/GPU/容器配置。
- 结果：12 个导航模块、真实 Slurm/GPU/Docker/存储/监控数据、Loading/Empty/Error/
  Unauthorized/Stale/Partial 状态；NVML index 与 minor 按 PCI/UUID 物理身份合并。
- 静态检查：ESLint、TypeScript、Vitest、生产 build 通过。
- 未解决问题：Docker Hub 继续 DEFERRED；不伪造为 AVAILABLE。

## 阶段 D：认证、RBAC、审批和审计

- 开始：2026-08-06 02:30:00 +08:00
- 结束：2026-08-06 02:39:59 +08:00
- 修改文件：认证 route、Argon2id、setup token、session/CSRF、RBAC、Operation 和 audit。
- 配置备份：`portal.env` 为新建 root-owned secret，不进入 Git 或报告。
- 结果：大小写规范化、五角色后端授权、单次 token、会话旋转、双层 CSRF、限速/锁定、
  高风险重新认证和递归审计脱敏完成。
- 静态检查：认证、token、session、CSRF、RBAC、审批状态机和审计 pytest 通过。
- 未解决问题：限速器为单 API worker 内存状态；当前 localhost 单实例可用，未来横向扩展
  前需改为共享存储。

## 阶段 E：受控写操作框架

- 开始：2026-08-06 02:40:00 +08:00
- 结束：2026-08-06 03:28:59 +08:00
- 修改文件：Root Worker 协议/schema/handlers、脚本 hash allowlist、socket smoke 和 unit。
- 配置备份：安装 unit 前保留本轮 backup 目录；未修改既有 `h100-*` 管理脚本。
- 结果：SO_PEERCRED、64KiB frame、固定 operation、绝对路径命令、超时/输出上限、
  root owner/mode/hash fail-closed；14 类写操作只允许 dry-run，真实执行全局禁用。
- 静态检查：11 项 Worker 单测和生产 socket smoke 通过；非 API UID 连接返回 EACCES。
- 修正：部署 exclude 锚定 `/build/`，避免删除 Next runtime；运行时代码移除 group/other
  写权限；Slurm JSON 规范化；GPU minor 改用 NVIDIA procfs 物理映射。
- 未解决问题：真实写 handler 必须等待后续单独审批，当前不可启用。

## 阶段 F：Origin-al 网页账号和激活链接

- 开始：2026-08-06 03:33:00 +08:00
- 结束：2026-08-06 03:36:59 +08:00（token 签发保留为最终终端动作）
- 修改文件：bootstrap CLI、无 token 预创建命令、CLI 测试和操作文档。
- 配置备份：数据库事务；未修改 Linux `origin-al`、SSH、shell、sudo 或计算身份。
- 结果：`Origin-al` / `origin-al`、`platform_owner`、`INVITED`、`SETUP_REQUIRED`、
  `NOT_ENROLLED`；credential/token/session/managed-user 计数均为 0；prepare 审计已写入。
- 静态检查：prepare 幂等且不签发 token 的 pytest 通过。
- 未解决问题：一次性 URL 必须在所有报告/Git 完成后显示一次，由管理员本人设置密码。

## 阶段 G：部署、测试和验收

- 开始：2026-08-06 03:37:00 +08:00
- 结束：在最终报告记录。
- 修改文件：部署/安全/备份/事件响应文档、最终 unit 微调和报告。
- 配置备份：沿用本轮 backup；运行时代码可由前一 Git/构建和备份恢复。
- 结果：Web/API localhost、Worker socket、PostgreSQL Unix Socket、健康检查和平台回归通过；
  Web 正常 SIGTERM 码 143 已声明，维护重启不再产生 false failed result。
- 静态检查：27 pytest、严格 MyPy、Ruff、8 Vitest、ESLint、TypeScript、Next build、
  16 Playwright、systemd verify、Alembic check 均通过。
- 不变项：Slurm IDLE+DRAIN、队列为空、Guard timer disabled、MIG Disabled、无 Pilot Linux
  用户、无新容器、无防火墙/NFS/第二节点变更。
- 未解决问题：Mellanox P0、Docker Hub deferred、PCI DOE P1 和待重启 microcode 观察项。

## Portal-0/1 后续批准变更：私有隧道地址监听

- 开始：2026-08-06 14:51:27 +08:00
- 结束：2026-08-06 15:17:08 +08:00。
- 授权：管理员明确确认 `10.10.10.2` 为安全管理入口。
- 设计：只把 Web 从 loopback 改为精确 `10.10.10.2:18080`；API 继续强制
  `127.0.0.1:18081`，PostgreSQL 和 Root Worker 仍为 Unix Socket。
- 网络沙箱：Web 继续 `IPAddressDeny=any`，只允许 localhost 与 `10.10.10.0/24`；不监听
  `0.0.0.0`，不修改防火墙。
- 认证：CSRF/CORS allowlist 只加入精确 `http://10.10.10.2:18080`，保留 localhost
  SSH Tunnel 回退，不使用通配 Origin。
- 不变项：Slurm DRAIN、Guard timer disabled、MIG Disabled、无 Pilot Linux 用户、无计算
  onboarding、无 NFS/第二节点变更。

## Portal-2 后续管理员决策：内部 HTTP 接受

- 决策日期：2026-08-06。
- `PORTAL ACCESS MODE`：`INTERNAL HTTP ACCEPTED BY ADMINISTRATOR — VIRTUAL NETWORK ONLY`。
- `TRANSPORT TLS`：`NOT ENABLED — NOT REQUIRED FOR CURRENT PILOT SCOPE`。
- Web 继续只监听 `10.10.10.2:18080`；API 继续只监听 `127.0.0.1:18081`。
- 不修改防火墙，不增加公网路由或端口映射，不声称已启用 HTTPS。
- Cookie、登录限速、CSRF、会话超时、RBAC 和审计保持当前内部 HTTP 配置。
- 访问扩展到其他网络、VPN 用户、办公网或公网时，必须重新评估并优先启用 HTTPS。
- 该决策解除 transport 对未来真实写操作的阻断，但不等于 Portal-3 执行批准；收到完整
  Portal-3 批准前，真实写操作继续禁用，Slurm 保持 DRAIN。

## Portal-3D-R：SSH Key 自助与连接 Gate

- 决策日期：2026-08-08。
- 浏览器可本地生成标准 OpenSSH ED25519 key pair，或导入已有 `.pub`；服务器只接收和
  保存 public key/metadata，private key 只存在浏览器内存和用户下载。
- Key 数据模型支持最多五条独立 record、HOST/CONTAINER/BOTH Scope、VALIDATED/
  INSTALLED/REVOKED 状态、全局 fingerprint 冲突和 enrollment Operation 绑定。
- 首次登录、宿主连接、容器连接、复制命令和 VS Code 入口共用同一 Key 设置流程；
  VALIDATED 但 STAGED 时不显示连接命令。
- Root Worker 只新增 root-owned public-key staging prepare/discard；真实 Activate handler
  仍禁用。`user.activate` 仅允许 dry-run，并重新验证 STAGED 资源及两个安装目标计划。
- 数据库 revision：`f4a91c3e7b20`。最终测试、部署、真实用户 Key 登记和验收结果记录在
  本阶段运行报告；在真实 Key 登记前不得声称 Activate dry-run READY。
