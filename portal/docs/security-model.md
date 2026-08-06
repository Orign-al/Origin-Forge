# H100 Portal 安全模型

## 信任边界

浏览器、Web、API、数据库与 Root Worker 是独立信任域。浏览器校验只改善交互，API
必须重新校验；API 校验不能替代 Worker 校验。Web 与 API 均为非 root，API 不持有
任意宿主命令能力。Root Worker 仅信任通过 Unix Socket 对端凭据验证、协议版本验证、
严格 schema 验证和 allowlist 分派后的请求。

Worker 禁止接收任意 command、argv、shell、SQL、路径、systemd unit、Docker mount、
capability 或 `scontrol` 参数。子进程一律 `shell=False`，使用固定环境、超时与输出
上限。现有管理脚本在调用前检查 root owner、普通用户不可写和 SHA-256 allowlist。

## 身份分离

网页身份与 Linux 资源身份分别建模。网页显示账号 `Origin-al` 的唯一规范化登录名为
`origin-al`，登录大小写不敏感；它映射现有 Linux 用户 `origin-al`，但初始计算身份为
`NOT_ENROLLED`。Portal 网页密码不读取或修改 Linux shadow，不修改 SSH 密码、shell、
sudo 或 `authorized_keys`。

## 密码和邀请

- 密码长度 14–128 字符，保留用户输入的 Unicode 与前后空格，不执行 trim。
- 密码必须非空，不得等于规范化用户名，并拒绝内置弱密码集合。
- 使用 `argon2-cffi` 的 Argon2id 当前安全默认参数与每密码独立 salt。
- 首次设置 token 使用 48 字节 CSPRNG，URL-safe 编码；数据库只保存 SHA-256 hash。
- token 单次使用、30 分钟过期；重新签发会撤销旧 token。
- 明文 token 只在受控管理员终端显示一次，不进入日志、报告、Git 或环境变量。

## 会话

会话 ID 使用 48 字节 CSPRNG，仅 hash 入库。Cookie 为 `HttpOnly`、`SameSite=Strict`、
`Path=/`；批准的私有隧道网络和 SSH Tunnel HTTP 模式为 `Secure=false`，未来 HTTPS
部署必须切换为 `Secure=true`。CSRF 只接受 `127.0.0.1:18080` 与 `10.10.10.2:18080`
两个精确 Origin，不使用通配。会话空闲超时 30 分钟、绝对超时 12 小时；登录后建立新会话，改密后
撤销其他会话，退出立即撤销当前会话。高风险操作要求十分钟内重新认证。

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

## 明确不变项

Portal-0/1 不创建 Linux/Pilot 用户，不创建长期用户容器，不启用 Guard timer，不修改
现有 GPU 隔离 drop-in，不修改 MIG、GRES、Kernel、Driver、Mellanox 或防火墙，且不
RESUME Slurm。
