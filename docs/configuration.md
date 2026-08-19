# Origin Forge 配置指南

本文说明 Portal 的开发与生产配置。生产服务的完整安装、迁移、验收和回滚仍以
[`portal/docs/deployment.md`](../portal/docs/deployment.md) 为准。

## 配置文件

仓库只提供不含凭据的模板：

```text
portal/deploy/portal.env.example
```

生产配置固定为：

```text
/etc/h100-portal/portal.env
```

目录应为 `root:root 0750`，文件应为 `root:h100-portal-api 0640`。不得把生产环境文件、
数据库密码、Session secret、一次性设置链接或用户私钥复制回仓库。

在首次部署的新主机上创建目录和配置：

```bash
sudo install -d -o root -g root -m 0750 /etc/h100-portal
sudo install -o root -g h100-portal-api -m 0640 \
  /srv/gpu-platform/platform/portal/deploy/portal.env.example \
  /etc/h100-portal/portal.env
sudoedit /etc/h100-portal/portal.env
```

已有生产配置时不要覆盖模板；使用 `sudoedit` 做精确变更，并先保存 root-only 备份。

## Portal 环境变量

| 变量                                        | 用途                            | 生产要求                                           |
| ------------------------------------------- | ------------------------------- | -------------------------------------------------- |
| `PORTAL_DATABASE_URL`                       | SQLAlchemy/PostgreSQL 连接地址  | 使用本机 Unix Socket；不得复用 SlurmDBD MariaDB    |
| `PORTAL_SECRET_KEY`                         | Session、CSRF 等 HMAC 密钥      | 至少 32 个随机字节；不得提交或输出到日志           |
| `PORTAL_WORKER_SOCKET`                      | Root Worker Unix Socket         | 固定 `/run/h100-portal/worker.sock`                |
| `PORTAL_PUBLIC_ACCESS_HOST`                 | UI 和 SSH 信息显示的主地址      | 具体 IPv4；当前为 `20.10.10.3`，禁止 `0.0.0.0`     |
| `PORTAL_ALLOWED_ORIGINS`                    | CORS/CSRF 精确 Origin allowlist | 逗号分隔；禁止 `*`                                 |
| `PORTAL_COOKIE_SECURE`                      | 是否只通过 HTTPS 发送 Cookie    | 当前内网 HTTP 为 `false`；启用 HTTPS 后改为 `true` |
| `PORTAL_ENVIRONMENT`                        | 运行环境标签                    | 生产为 `production`                                |
| `PORTAL_INITIAL_PASSWORD_SETUP_TOKEN_HOURS` | 首次设置链接有效期              | 当前为 `24`                                        |
| `PORTAL_PASSWORD_RESET_TOKEN_MINUTES`       | 密码重置链接有效期              | 当前为 `30`                                        |
| `PORTAL_PASSWORD_ACTION_CHALLENGE_MINUTES`  | 密码操作 challenge 有效期       | 当前为 `10`                                        |

当前双 EasyTier 入口对应的非敏感配置为：

```dotenv
PORTAL_PUBLIC_ACCESS_HOST=20.10.10.3
PORTAL_ALLOWED_ORIGINS=http://127.0.0.1:18080,http://10.10.10.220:18080,http://20.10.10.3:18080,http://20.10.10.3,https://20.10.10.3
```

`PORTAL_PUBLIC_ACCESS_HOST` 只控制用户可见的主地址，不控制 listener。Web listener 由
`h100-portal-web.service` 的 `HOSTNAME=0.0.0.0` 配置；systemd `IPAddressAllow` 继续限制
到批准的 EasyTier/VPN/管理网段。

## Portal 界面语言

Portal 支持 `zh-CN` 和 `en-US`。登录页及登录后的顶栏均提供语言选择器，默认语言为中文。
浏览器选择后写入以下非敏感偏好 Cookie：

```text
origin_forge_locale=zh-CN | en-US
```

Cookie 固定为 `Path=/; Max-Age=31536000; SameSite=Lax`。当前内网 HTTP 部署不能设置
`Secure`；该 Cookie 必须保持非 `HttpOnly`，因为客户端切换器需要更新它。它不包含账号、
Session、token 或 CSRF 数据，服务端只接受上述两个固定值，其他值一律回退到 `zh-CN`。

语言 Cookie 按访问主机隔离，因此 `10.10.10.220` 与 `20.10.10.3` 会分别记忆偏好。这不会
影响两个入口的登录、Session 安全边界或同源 `/api/v1/*` 代理。

## 生成生产随机值

在受控管理员终端生成随机值，并直接写入 root-owned 配置。以下命令只用于生成候选值，
不要把输出复制到聊天、工单、Git 或 shell history：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

数据库角色密码和 `PORTAL_SECRET_KEY` 必须分别生成，不得复用。部署报告只能记录已设置、
owner/mode 和校验结论，不能记录明文。

## 开发配置

完整 Alembic migration 以 PostgreSQL 18 为准。SQLite 只用于 pytest 的隔离单元测试，不能
作为生产 DDL 验收结果。

准备一个专用开发数据库和角色后，在单独终端设置开发环境：

```bash
cd portal
export PORTAL_DATABASE_URL='postgresql+psycopg://origin_forge_dev:CHANGE_ME@127.0.0.1/origin_forge_dev'
export PORTAL_SECRET_KEY='CHANGE_ME_TO_AT_LEAST_32_RANDOM_BYTES'
export PORTAL_WORKER_SOCKET='/tmp/origin-forge-dev/worker.sock'
# 该字段要求具体的非 loopback IPv4；本地开发保留默认展示地址即可。
export PORTAL_PUBLIC_ACCESS_HOST='20.10.10.3'
export PORTAL_ALLOWED_ORIGINS='http://127.0.0.1:18080'
export PORTAL_COOKIE_SECURE='false'
export PORTAL_ENVIRONMENT='development'
export PYTHONPATH="$PWD/apps/api/src:$PWD/apps/worker/src"
```

上面的 `CHANGE_ME_*` 只能用于说明字段，启动前必须替换。若希望减少 shell history 暴露，
使用权限为 `0600`、位于仓库外的临时环境文件加载变量。

显式迁移并启动 API：

```bash
.venv/bin/alembic -c alembic.ini upgrade head
.venv/bin/uvicorn h100_portal_api.main:app \
  --host 127.0.0.1 --port 18081 --workers 1 --no-proxy-headers
```

另一个终端启动 Web：

```bash
cd portal
pnpm dev
```

开发入口为 `http://127.0.0.1:18080/`。健康检查：

```bash
curl -fsS http://127.0.0.1:18081/health/live
curl -I http://127.0.0.1:18080/
```

没有真实 Root Worker socket 时 `/health/ready` 返回 503 是 fail-closed 行为。不要为本地开发
把 Worker 改成 TCP、放宽为任意 shell，或挂载 Docker Socket。涉及用户、Container、Slurm
和 GPU 的端到端测试必须在受控 H100 测试环境完成。

## 生产启动顺序

安装 runtime 与显式 migration 完成后，按依赖顺序启动：

```bash
sudo systemctl enable --now h100-portal-worker.socket
sudo systemctl enable --now h100-portal-api.service
sudo systemctl enable h100-portal-web.service
sudo systemctl restart h100-portal-web.service
```

Root Worker 由 socket activation 启动。不要把 Worker 改成 TCP 服务。只有代码或配置实际
影响 API 时才重启 API；不要因 Portal 部署重启 Docker、Slurm、DCGM、sshd 或 EasyTier。

## EasyTier 与浏览器代理

服务端地址：

```text
tun0  10.10.10.220/24
tun1  20.10.10.3/24
```

客户端必须获得同一 EasyTier 网络中的虚拟地址与路由。Chrome 使用系统代理时，502 通常
由代理软件而非 Portal 返回；应把以下网段设为 DIRECT：

```text
10.10.10.0/24
20.10.10.0/24
```

客户端可使用以下命令区分路由与浏览器代理问题：

```powershell
Test-NetConnection 20.10.10.3 -Port 18080
curl.exe --noproxy "*" -I http://20.10.10.3:18080/
```

## 部署后不变量

每次配置或部署后至少确认：

```bash
curl -fsS http://127.0.0.1:18081/health/live
curl -fsS http://127.0.0.1:18081/health/ready
curl -fsS http://10.10.10.220:18080/login >/dev/null
curl -fsS http://20.10.10.3:18080/login >/dev/null
ss -lntup
systemctl --failed
```

预期边界：

- Web：`0.0.0.0:18080`
- API：仅 `127.0.0.1:18081`
- Worker：仅 `/run/h100-portal/worker.sock`
- Container SSH：`0.0.0.0:<allocated 220xx>`，用户显示具体 EasyTier 地址
- Docker API：无 TCP listener
- PostgreSQL/MariaDB/SlurmDBD/MUNGE：不作为 EasyTier 用户入口
