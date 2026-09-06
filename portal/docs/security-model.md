# Origin Forge Portal 安全模型

## 信任边界

浏览器、Web、API、数据库与 Root Worker 是独立信任域。浏览器校验只改善交互，API
必须重新校验；API 校验不能替代 Worker 校验。Web 与 API 均为非 root，API 不持有
任意宿主命令能力。Root Worker 仅信任通过 Unix Socket 对端凭据验证、协议版本验证、
严格 schema 验证和 allowlist 分派后的请求。

计算用户采用两阶段 SSH 公钥契约：`user.stage` 永不读取或安装公钥；`user.activate`
只接收受控 key record ID，并在 root-owned staging 文件上使用 `O_NOFOLLOW`、owner/mode/
inode/大小检查和 `ssh-keygen` fingerprint 校验。公钥验证失败不会部分激活用户。

当前多用户产品路径使用 `POST /api/v1/self/compute/activate` 和 Worker 的
`compute.activate.self`，旧 `user.activate` 仅保留历史兼容。浏览器只发送幂等键；API 从
认证会话解析 owner，并绑定对应 Request、成功 Stage、Container、Storage 和该 owner 的
有效 `CONTAINER` key。Worker 先写 root-controlled `ACTIVATING / Lease NOT_STARTED` 状态，
安装容器公钥、启动并验证 GPU NONE 的容器，再写 `ACTIVE` 和精确 96 小时 Lease。任何失败
都会停止容器、移除本次安装的容器公钥并恢复 STAGED；宿主 shell 始终 nologin，Host
authorized_keys 始终 ABSENT。

新 Key 可以在浏览器生成或由用户导入已有 `.pub`。浏览器生成使用固定依赖和 Web Crypto
随机源；私钥只保留在页面内存及用户下载，不提交 API，也不使用 localStorage、
sessionStorage、IndexedDB、Service Worker cache、analytics 或错误遥测。API 不提供
server-generated private key endpoint，Worker 不生成私钥。当前内部 HTTP 是管理员已接受
的虚拟网络模式，不得声称有 HTTPS；该 transport 决策不放宽“私钥永不离开浏览器”的
数据边界。

API 将请求先作为未记录正文检查私钥字段与装甲，再进入 `extra=forbid` schema 和结构化
public-key parser；错误消息与审计不回显正文。数据库保存 canonical public key 是有意的，
但常规响应和审计只返回类型、SHA-256 fingerprint、注释、Scope 和状态。全局 fingerprint
唯一，撤销记录保留以阻止复用。

Root Worker staging 以 UUID 文件和 sidecar 绑定 enrollment Operation/managed identity，
使用 root-only 目录、无链接跟随读取、inode/owner/mode/link/size/hash 复核和原子写入。
HOST 与 CONTAINER 的 authorized_keys 永远是独立目标；不得挂载宿主 `.ssh` 或任何私钥到
容器。

Worker 禁止接收任意 command、argv、shell、SQL、路径、systemd unit、Docker mount、
capability 或 `scontrol` 参数。子进程一律 `shell=False`，使用固定环境、超时与输出
上限。现有管理脚本在调用前检查 root owner、普通用户不可写和 SHA-256 allowlist。

## 身份分离

网页身份与 Linux 资源身份分别建模。网页显示账号 `Origin-al` 的唯一规范化登录名为
`origin-al`，登录大小写不敏感；它映射现有 Linux 用户 `origin-al`。独立计算身份
`origin-pilot` 通过 `PortalManagedUser` 关联到该网页账号，不替换管理映射。Portal 网页
密码不读取或修改 Linux shadow，不修改 SSH 密码、管理账号 shell、sudo 或
`authorized_keys`。

## Development Container sudo 边界

Development Container 内的受管普通用户可以获得该容器 namespace 内的 root，但这不等于
宿主 root。策略文件按用户生成于 root-only container-data 目录，必须是 `root:root 0440`
的普通文件、只有精确一行 `<user> ALL=(ALL:ALL) NOPASSWD: ALL`，并通过 `visudo -cf`；容器
只读挂载到 `/etc/sudoers.d/90-h100-dev-user`。不得挂载宿主 `/etc/sudoers`，也不得让容器
修改该策略文件。

允许容器 sudo 的前提是宿主同名账号继续使用 `/usr/sbin/nologin`、密码锁定、没有 Host
`authorized_keys`，且不属于 `sudo`、`docker`、`video`、`render`、`gpu-platform-admin`、
`adm` 或 `systemd-journal`。容器必须保持非 privileged、非 host network/PID/IPC/cgroup
namespace、默认受限 AppArmor/seccomp、无额外 capability、无 GPU DeviceRequest，并且不挂载
Docker socket、Worker socket、MUNGE 或非白名单宿主路径。容器 root 可以管理个人 Home、
Workspace 与 Shared 内容；这些数据原本就归该用户所有，不能借此读取其他用户或管理宿主。

既有容器只能通过 `h100-container-sudo-enable USER --confirm=USER` 的显式单用户迁移启用。
工具先验证上述边界和 canonical Home/Workspace alias，再保留原镜像并创建回滚点，使用
`--force-recreate --no-build` 受控重建；失败时自动恢复原 Compose。该操作会造成该用户容器
短暂中断，不是热更新，也不能通过 Root Worker HTTP API 触发。

## 密码和邀请

- 密码长度 14–128 字符，保留用户输入的 Unicode 与前后空格，不执行 trim。
- 密码必须非空，不得等于规范化用户名，并拒绝内置弱密码集合。
- 使用 `argon2-cffi` 的 Argon2id 当前安全默认参数与每密码独立 salt。
- 首次设置 token 使用 48 字节 CSPRNG，URL-safe 编码；数据库只保存 SHA-256 hash。
- token 单次使用、30 分钟过期；重新签发会撤销旧 token。
- 明文 token 只在受控管理员终端显示一次，不进入日志、报告、Git 或环境变量。

## 会话

会话 ID 使用 48 字节 CSPRNG，仅 hash 入库。Cookie 为 `HttpOnly`、`SameSite=Strict`、
`Path=/`。管理员已接受受控虚拟网络内的 Pilot HTTP，因此当前保持 `Secure=false`；TLS
未启用且当前范围不要求启用。CSRF 只接受 `127.0.0.1:18080` 与
`10.10.10.220:18080`、`20.10.10.3:18080`、批准代理 `20.10.10.3` 的 HTTP/HTTPS 和 localhost 精确
Origin，不使用通配。会话空闲超时 30 分钟、绝对超时
12 小时；登录后建立新会话，改密后撤销其他会话，退出立即撤销当前会话。高风险操作要求
十分钟内重新认证。若访问扩展到其他网络、VPN 用户、办公网或公网，必须重新评估并优先
启用 HTTPS，同时把 Cookie 切换为 `Secure=true`。

## CSRF 与登录保护

状态修改请求同时验证受信 Origin/Referer 和由服务端 HMAC 签名的 CSRF token；
SameSite Cookie 不是唯一防线。登录使用统一错误文案，按 IP 与规范化账号限速，连续
失败暂时锁定，并记录不含密码的安全审计事件。

## RBAC 与审批

后端逐操作检查 `platform_owner`、`platform_admin`、`operator`、`auditor`、`user`
权限，不能依赖前端隐藏按钮。高风险操作进入 `PENDING_APPROVAL`，需要审批记录、
对象名确认及最近重新认证。`slurm.resume` 在 Portal-0/1 即使获得审批也只能 dry-run。

Lease 续期使用专用权限：`platform_owner` 与 `platform_admin` 可读、可审批，`auditor`
只读，`operator` 和普通用户均无权访问管理员续期列表。审批 POST 同时要求 Session CSRF
与最近重新认证；浏览器中的导航或按钮隐藏不是授权证据。到期服务仅将已经失效的待审请求
收敛为 `CANCELLED`，不得把已经回收或恢复的 Lease 状态倒退回 `EXPIRED`。

## 审计与脱敏

审计记录 actor、role、来源 IP、User-Agent 摘要、对象、operation、结果、时间及严格
白名单 metadata。递归脱敏器拒绝或替换 password、token、secret、cookie、session、
private key、authorized key body、数据库密码和 MUNGE key。SSH 公钥只记录类型、
fingerprint 与截断注释。

## Portal-3C/3D-R 精确写边界

唯一真实写允许固定 `origin-pilot`、UID/GID 20001、project 30001、端口 22023 和固定
容器/Slurm 参数。Worker systemd sandbox 仅为 hash-pinned 生命周期脚本增加 `/etc`、
`/home`、`/srv/gpu-platform` 及固定平台审计日志写白名单。`ProtectHome=no` 仅避免覆盖
`ReadWritePaths=/home` 的精确 Stage 需求；`ProtectSystem=strict` 继续使其余宿主树只读。
固定 schema、actor/approval/idempotency 绑定和 `shell=False` 不变。Activate、其他用户、
任意命令、Slurm RESUME、MIG、GRES、Kernel、Driver、Mellanox、防火墙、NFS 和第二节点
仍不在授权范围。

内部 HTTP 的管理员接受仅解除 transport 阻断，不构成额外执行批准。Portal-3C 只开放
当前精确批准的 `origin-pilot user.stage`；Activate 和所有其他真实写 handler 继续禁用。
Portal-3D-R 只增加 public-key self-service staging 与 `user.activate` dry-run。它不修改
宿主/容器 authorized_keys、shell、密码、容器状态、Slurm、quota 或 GPU policy。
