# 密码、邀请与会话安全

## 身份分离

Portal 密码只认证网页账号，不读取或修改 Linux shadow。`Origin-al` 的规范化网页登录名
为 `origin-al`，映射已有 Linux 用户 `origin-al`，但网页密码、Linux 密码和 SSH 凭据
彼此独立。网页登录大小写不敏感，数据库对 `normalized_login` 设置唯一约束。

## 密码规则与哈希

- 14–128 个 Unicode 字符；不 trim，允许长口令和中文。
- 禁止空白密码、与规范化用户名相同的密码及内置常见弱密码。
- 不强制特定字符类别，避免降低长口令可用性。
- 使用 `argon2-cffi` 的 Argon2id 安全默认参数、独立随机 salt 和完整 encoded hash。
- 不记录明文、请求正文或哈希导出；不使用 MD5、SHA-1、单轮 SHA-256 或可逆加密。

修改网页密码会撤销其他会话并旋转当前会话，不影响 Linux/SSH 密码。

## 一次性设置 token

设置 token 由 48 字节 CSPRNG 生成并进行 URL-safe 编码。数据库只保存 SHA-256 digest；
明文仅由管理员 bootstrap CLI 在当前终端显示一次。token 默认 30 分钟过期、单次使用；
使用后写入 `used_at`，新 token 会撤销旧的未使用 token。

禁止把 URL 或 token 写入 journal、普通日志、报告、Git、环境变量、shell history 文件、
工单或聊天。不要通过 `systemd-run`、`tee` 或带命令回显的调试 shell 运行 bootstrap。

## 会话

- session id 使用 48 字节 CSPRNG，数据库只保存 digest。
- Cookie：`HttpOnly`、`SameSite=Strict`、`Path=/`。
- 当前受控虚拟网络内部 HTTP 由管理员接受，Cookie 保持 `Secure=false`；TLS 未启用且当前
  Pilot 范围不要求启用。
- 空闲超时 30 分钟，绝对超时 12 小时。
- 每次登录建立新 session 并撤销同浏览器提交的旧 session。
- 退出立即撤销当前 session；用户可查看并撤销其他活动 session。
- 高风险操作要求 10 分钟内重新认证。

浏览器关闭不是唯一安全边界。数据库恢复、密钥疑似泄露或管理员账号事件后，应撤销所有
活动 session 和未使用设置 token。

## CSRF 和来源校验

每个状态修改请求必须同时通过：

1. `Origin` 或 `Referer` 属于精确 allowlist；
2. CSRF Cookie 与 `X-CSRF-Token` 一致；
3. 登录前 token 的 HMAC 或登录后 session 中保存的 CSRF digest 有效。

SameSite Cookie 不能替代上述校验。API 不信任代理头，当前服务启动使用
`--no-proxy-headers`。生产 allowlist 只包含 `http://10.10.10.2:18080` 和作为回退的
`http://127.0.0.1:18080`，不得添加通配 Origin。

若访问范围扩展到其他网络、VPN 用户、办公网或公网，必须重新进行 transport 威胁评估，
优先启用 HTTPS，并将 Cookie `Secure` 属性切换为 `true`。不得在 TLS 启用前声称当前入口
为 HTTPS。

## 登录防护

登录按来源 IP 和规范化账号分别进行滑动窗口限速；连续失败达到阈值后临时锁定账号。
不存在账号和错误密码返回同一文案，并执行固定 Argon2 验证路径以降低枚举信号。审计仅
记录 actor、安全摘要、结果和原因，不记录密码、Cookie、token 或完整 User-Agent。
