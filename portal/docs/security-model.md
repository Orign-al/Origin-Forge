# H100 Portal 安全模型

## 信任边界

浏览器、Web、API、数据库与 Root Worker 是独立信任域。浏览器校验只改善交互，API
必须重新校验；API 校验不能替代 Worker 校验。Web 与 API 均为非 root，API 不持有
任意宿主命令能力。Root Worker 仅信任通过 Unix Socket 对端凭据验证、协议版本验证、
严格 schema 验证和 allowlist 分派后的请求。

计算用户采用两阶段 SSH 公钥契约：`user.stage` 永不读取或安装公钥；`user.activate`
只接收受控 key record ID，并在 root-owned staging 文件上使用 `O_NOFOLLOW`、owner/mode/
inode/大小检查和 `ssh-keygen` fingerprint 校验。公钥验证失败不会部分激活用户。

Worker 禁止接收任意 command、argv、shell、SQL、路径、systemd unit、Docker mount、
capability 或 `scontrol` 参数。子进程一律 `shell=False`，使用固定环境、超时与输出
上限。现有管理脚本在调用前检查 root owner、普通用户不可写和 SHA-256 allowlist。

## 身份分离

网页身份与 Linux 资源身份分别建模。网页显示账号 `Origin-al` 的唯一规范化登录名为
`origin-al`，登录大小写不敏感；它映射现有 Linux 用户 `origin-al`。独立计算身份
`origin-pilot` 通过 `PortalManagedUser` 关联到该网页账号，不替换管理映射。Portal 网页
密码不读取或修改 Linux shadow，不修改 SSH 密码、管理账号 shell、sudo 或
`authorized_keys`。

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
`10.10.10.2:18080` 两个精确 Origin，不使用通配。会话空闲超时 30 分钟、绝对超时
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

## 审计与脱敏

审计记录 actor、role、来源 IP、User-Agent 摘要、对象、operation、结果、时间及严格
白名单 metadata。递归脱敏器拒绝或替换 password、token、secret、cookie、session、
private key、authorized key body、数据库密码和 MUNGE key。SSH 公钥只记录类型、
fingerprint 与截断注释。

## Portal-3C 精确写边界

唯一真实写允许固定 `origin-pilot`、UID/GID 20001、project 30001、端口 22023 和固定
容器/Slurm 参数。Worker systemd sandbox 仅为 hash-pinned 生命周期脚本增加 `/etc`、
`/home`、`/srv/gpu-platform` 写白名单；固定 schema、actor/approval/idempotency 绑定和
`shell=False` 不变。Activate、其他用户、任意命令、Slurm RESUME、MIG、GRES、Kernel、
Driver、Mellanox、防火墙、NFS 和第二节点仍不在授权范围。

内部 HTTP 的管理员接受仅解除 transport 对未来 Portal-3 的阻断，不构成 Portal-3 执行
批准。收到完整 Portal-3 批准前，真实写 handler 继续禁用。
