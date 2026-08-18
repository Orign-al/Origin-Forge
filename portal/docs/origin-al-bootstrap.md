# Origin-al 网页账号 Bootstrap

## 身份定义

- 显示登录名：`Origin-al`
- 唯一规范化登录名：`origin-al`（登录大小写不敏感）
- Linux 映射：现有 `origin-al`
- 角色：`platform_owner`
- 初始网页状态：`INVITED` / `SETUP_REQUIRED`
- 初始计算 onboarding：`NOT_ENROLLED`

Bootstrap 不创建 Linux 用户，不修改 Linux 密码、SSH 密码认证、`authorized_keys`、shell、
sudo 或 GPU slice。不得创建大小写变体 Linux 账号。

## 前置检查

```bash
getent passwd origin-al
curl -fsS http://127.0.0.1:18081/health/ready
curl -fsS http://10.10.10.220:18080/login >/dev/null
sudo bash -c \
  'set -a; . /etc/h100-portal/portal.env; set +a; exec /usr/bin/setpriv --reuid=h100-portal-api --regid=h100-portal-api --init-groups /opt/h100-portal/venv/bin/h100-portal-admin status-origin-al'
```

若 Linux 用户不存在立即停止。若 Portal 账号已存在，先检查角色、状态和映射；不得覆盖
密码或默认生成新 token。只有管理员明确执行 reset 才可撤销旧 token 并生成新链接。

## 账号预创建（不生成 token）

为了让账号状态、角色、Linux 映射、报告和 Git 在明文 URL 出现前完成验收，先运行：

```bash
sudo bash -c \
  'set -a; . /etc/h100-portal/portal.env; set +a; exec /usr/bin/setpriv --reuid=h100-portal-api --regid=h100-portal-api --init-groups /opt/h100-portal/venv/bin/h100-portal-admin prepare-origin-al'
```

该命令幂等，只创建或校验网页记录，并明确输出 `no setup token generated`。随后可运行
`status-origin-al` 并完成报告；它不改变 Linux/SSH/计算身份。每次 prepare 都写入不含
credential 的 `portal_owner.prepare` 审计事件。

## 首次邀请

只能在不会进入 journal 或普通日志的当前管理员 SSH 终端中直接运行：

```bash
sudo bash -c \
  'set -a; . /etc/h100-portal/portal.env; set +a; exec /usr/bin/setpriv --reuid=h100-portal-api --regid=h100-portal-api --init-groups /opt/h100-portal/venv/bin/h100-portal-admin bootstrap-origin-al --reset-setup-token --base-url http://10.10.10.220:18080'
```

`--reset-setup-token` 在此处是显式授权：预创建后账号已存在，而最终步骤必须签发首次
token。禁止使用 `systemd-run`、`tee`、shell tracing 或重定向保存输出。CLI 是唯一允许打印一次
明文 token 的位置；数据库只保存 digest。最终报告只能写
`Origin-al password setup token generated`，不得包含 URL。

签发同时记录 `password.setup_link.issue`，metadata 只说明是否替换旧链接，不保存链接、
token 或请求正文。

配置目录保持 `root:root 0750`。上面的固定命令由 root 读取 EnvironmentFile，随后立即用
`setpriv` 降为 API UID；不要让 API 账号直接遍历 `/etc/h100-portal`，也不要把环境打印出来。

批准的私有隧道客户端可直接打开 CLI 显示的 `10.10.10.220:18080` 一次性 URL。如需
SSH Tunnel 回退，管理员本地先建立：

```bash
ssh -L 18080:10.10.10.220:18080 h100-codex
```

然后把 CLI 链接的主机部分替换为 `127.0.0.1:18080`，在 30 分钟内打开。token 与主机名
不绑定，但 API 只接受上述两个精确 Origin。管理员不得替 Origin-al 设置或获知最终密码。

## 设置完成后的状态

成功设置密码后 token 立即失效，账号变为 `ACTIVE` / `SET`，写入 `activated_at`，撤销
旧会话并建立新会话，记录不含密码/token 的审计事件。资源 onboarding 仍为
`NOT_ENROLLED`，GPU 隔离为 `NOT_APPLIED`；不得自动创建容器或 Slurm 计算身份。

若 URL 过期，必须由 platform owner/受控管理员明确执行 `--reset-setup-token`。重新签发
会撤销旧的未使用 token；任何报告与工单仍不得保存 URL。
