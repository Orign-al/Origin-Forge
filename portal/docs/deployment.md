# Portal 部署手册

## 当前部署形态

- 源码：`/srv/gpu-platform/platform/portal`
- 只读运行代码：`/opt/h100-portal`
- Python venv：`/opt/h100-portal/venv`
- 配置：`/etc/h100-portal`
- 数据和日志：`/var/lib/h100-portal`、`/var/log/h100-portal`
- Runtime socket：`/run/h100-portal/worker.sock`
- Web：`10.10.10.220:18080`（精确绑定 `tun0`）
- API：`127.0.0.1:18081`（继续仅限 loopback）
- PostgreSQL：本机 Unix Socket，不监听 Portal TCP 端口

管理员已接受当前 Pilot 经 `10.10.10.0/24` 受控虚拟网络使用内部明文 HTTP 直接访问
Web；Transport TLS 未启用且当前范围不要求启用。SSH Tunnel 仅保留为可选回退。Web 不
监听 `0.0.0.0`，API 不监听管理网地址。本变更不修改防火墙或公网监听。Slurm 必须在整个
部署过程保持 `IDLE+DRAIN` 且队列为空。若访问范围扩大，必须重新评估并优先启用 HTTPS。

## 前置检查

```bash
git -C /srv/gpu-platform/platform status --short
git -C /srv/gpu-platform/platform rev-parse HEAD
sinfo -Nel
squeue -a
getent passwd origin-al
ss -lntup
```

工作区不干净时不得 reset 或删除既有文件。若有作业，停止部署。确认 18080/18081 未被
非 Portal 进程占用。

## 依赖和构建

Node.js 使用官方 tarball并验证同目录 `SHASUMS256.txt`；不得执行 NodeSource
`curl | bash`。Corepack 固定 `pnpm-workspace.yaml`/`packageManager` 指定的 pnpm。
Python 使用独立 venv，PostgreSQL 使用 Ubuntu 官方仓库。

```bash
cd /srv/gpu-platform/platform/portal
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm test
pnpm build

/opt/h100-portal/venv/bin/pip install --requirement requirements.lock
/opt/h100-portal/venv/bin/pip install --no-deps --no-build-isolation .
/opt/h100-portal/venv/bin/ruff check .
/opt/h100-portal/venv/bin/ruff format --check .
/opt/h100-portal/venv/bin/mypy apps/api/src apps/worker/src
/opt/h100-portal/venv/bin/pytest
```

`requirements.lock` 固定全部已解析 Python 版本；当前文件不包含 artifact hash，不能搭配
`--require-hashes`。Node 和 pnpm 的固定绝对路径应加入本次管理员命令的 `PATH`，不要
改变系统默认 Node。

## 数据库

`h100_portal` 数据库和同名角色与 SlurmDBD MariaDB、Grafana 完全分离。数据库凭据只存
于 `/etc/h100-portal/portal.env`（`root:h100-portal-api 0640`），不得输出或提交。

迁移只能由 Alembic 显式执行：

```bash
sudo bash -c \
  'set -a; . /etc/h100-portal/portal.env; set +a; cd /opt/h100-portal; exec /usr/bin/setpriv --reuid=h100-portal-api --regid=h100-portal-api --init-groups /opt/h100-portal/venv/bin/alembic -c /opt/h100-portal/alembic.ini upgrade head'
```

部署前后以同样的 root-load-then-drop-privilege 方式运行 `alembic current` 和
`alembic check`。`/etc/h100-portal` 保持 `root:root 0750`；不得为方便 CLI 而放宽目录。
应用启动不得自动改变 schema。

Portal-3D-R 的目标 revision 是 `f4a91c3e7b20`。迁移把 SSH Key 从单值式字段扩展为多条
record，并增加 Scope、状态、生成方式、创建者和 enrollment Operation 绑定。部署前必须
确认不存在无法迁移的 NULL 活跃 Key；升级后执行 `alembic current`、`alembic check`，并
在临时 PostgreSQL 数据库完成 upgrade → downgrade → upgrade 往返。不得用 SQLite 结果
代替 PostgreSQL DDL 验收。

## 安装与 systemd

`deploy/scripts/install-runtime.sh` 将源码复制到 `/opt/h100-portal`，排除 venv、缓存和开发
依赖，并清除运行时代码的 group/other 写权限。`/build/` 排除规则只能锚定仓库根目录；
不得排除 Next standalone 内的 `next/dist/build/`。

```bash
sudo /srv/gpu-platform/platform/portal/deploy/scripts/install-runtime.sh
sudo systemd-analyze verify \
  /etc/systemd/system/h100-portal-web.service \
  /etc/systemd/system/h100-portal-api.service \
  /etc/systemd/system/h100-portal-worker.socket \
  /etc/systemd/system/h100-portal-worker.service
sudo systemctl enable --now h100-portal-worker.socket
sudo systemctl enable --now h100-portal-api.service
sudo systemctl enable --now h100-portal-web.service
```

Socket unit 以 `DirectoryMode=0755` 创建 `/run/h100-portal`；socket 本身必须为
`root:h100-portal-api 0660`。父目录必须允许 API UID 遍历，但不得放宽 socket。

SSH public-key staging 必须在启用自助登记前存在：

```text
/var/lib/h100-portal/ssh-key-staging  root:root 0700
```

不得把该目录放入 Git、Web static 目录或普通备份报告。Worker 只允许创建 UUID `.pub` 和
配套 `.meta.json`，均为 `root:root 0600`。API 服务账号不得直接写该目录；Web 服务账号
不得读取。部署脚本或 tmpfiles 规则必须保证目录 metadata，不得在运行时放宽权限。

Portal-3E-FINAL 修改 `h100-user-create`，增加固定 Activate 回滚入口。必须先以 root-only
方式备份已安装文件，再以同目录临时文件、`root:root`、批准 mode、fsync 和原子 rename
部署；随后把新 SHA-256 精确写入 `/etc/h100-portal/worker-scripts.json`。仓库
`deploy/worker-scripts.json` 必须保存同一精确值。不得使用通配 hash、跳过校验或先启动
Worker 再补 allowlist；Worker 重启后必须先证明相关脚本的 `integrity_ok=true`。

### Managed compute user SSH policy

受管 policy source 是仓库根目录的
`config/ssh/70-h100-managed-compute-users.conf`，部署/验证入口是
`scripts/h100-managed-ssh-policy`。脚本只管理固定 source、固定 target、固定 SHA-256 和固定
`sshd -T -C` 上下文；`apply` 在完成 `sshd -t`、origin-pilot effective policy 和
origin-al/codexops no-regression 检查后仍不会自行 reload。管理员复核证据后，才对实际
OpenSSH unit 执行 reload，保持原管理连接并建立第二条管理连接。

部署前必须备份主配置与所有实际 drop-in，保留 owner/mode/mtime/hash。若语法、effective
policy、管理账号 diff、reload、服务状态或第二连接任一失败，保持原连接，恢复备份，重新
执行 `sshd -t` 与 reload。禁止 stop/restart sshd，也禁止把 PasswordAuthentication 的
变更扩大到全局用户。策略验收本身不安装 authorized_keys、不修改 shell、不启动容器。

## 验收

```bash
curl -fsS http://127.0.0.1:18081/health/live
curl -fsS http://127.0.0.1:18081/health/ready
curl -fsS http://10.10.10.220:18080/login >/dev/null
ss -lntup
systemctl --failed
```

ready 必须同时报告数据库和 Worker 可用。确认 Web 只有 `10.10.10.220:18080`，API 只有
`127.0.0.1:18081`，且不存在 `0.0.0.0:18080/18081` 或全局 IPv6 Portal 监听。
Web unit 的网络沙箱必须保留 `IPAddressDeny=any`，只额外允许 localhost 和
`10.10.10.0/24`；API unit 仍只允许 localhost。以 API UID 运行
`/opt/h100-portal/tests/worker_socket_smoke.py` 验证固定读取、dry-run 和拒绝路径。

浏览器验收必须覆盖 1366×768 和 1920×1080 的 Key 空状态、生成、导入、fingerprint
确认、一次性私钥下载确认、Key 列表、连接 Gate 与单次 owner-bound Activate。测试 Key 只在测试
内存/临时目录生成，测试结束删除，不安装到真实账号。部署后用源码/日志/数据库扫描确认
没有 private-key 装甲；扫描输出不得反向打印任何疑似 secret 正文。

批准的虚拟网络客户端直接打开 `http://10.10.10.220:18080`。如需可选 SSH Tunnel 回退：

```bash
ssh -L 18080:10.10.10.220:18080 h100-codex
```

浏览器打开 `http://127.0.0.1:18080`。两种入口都在精确 CSRF Origin allowlist 中；不得
增加通配 Origin。

## 回滚

失败时先停止 Web/API/Worker，保留 PostgreSQL 数据、日志和审计；恢复本轮备份的 unit、
配置和前一版 `/opt/h100-portal`，执行 `daemon-reload` 后只启动已验证版本。回滚不得
RESUME Slurm、删除用户数据、修改 GPU 隔离、MIG、驱动、Kernel 或防火墙。
