# Portal 部署手册

## 当前部署形态

- 源码：`/srv/gpu-platform/platform/portal`
- 只读运行代码：`/opt/h100-portal`
- Python venv：`/opt/h100-portal/venv`
- 配置：`/etc/h100-portal`
- 数据和日志：`/var/lib/h100-portal`、`/var/log/h100-portal`
- Runtime socket：`/run/h100-portal/worker.sock`
- Web/API：`127.0.0.1:18080`、`127.0.0.1:18081`
- PostgreSQL：本机 Unix Socket，不监听 Portal TCP 端口

Portal-0/1 只允许 SSH Tunnel 访问，不修改防火墙或公网监听。Slurm 必须在整个部署过程
保持 `IDLE+DRAIN` 且队列为空。

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

## 验收

```bash
curl -fsS http://127.0.0.1:18081/health/live
curl -fsS http://127.0.0.1:18081/health/ready
curl -fsS http://127.0.0.1:18080/login >/dev/null
ss -lntup
systemctl --failed
```

ready 必须同时报告数据库和 Worker 可用。确认不存在 `0.0.0.0:18080`、
`0.0.0.0:18081`、管理网地址或全局 IPv6 Portal 监听。以 API UID 运行
`/opt/h100-portal/tests/worker_socket_smoke.py` 验证固定读取、dry-run 和拒绝路径。

管理员本地访问：

```bash
ssh -L 18080:127.0.0.1:18080 h100-codex
```

浏览器打开 `http://127.0.0.1:18080`。

## 回滚

失败时先停止 Web/API/Worker，保留 PostgreSQL 数据、日志和审计；恢复本轮备份的 unit、
配置和前一版 `/opt/h100-portal`，执行 `daemon-reload` 后只启动已验证版本。回滚不得
RESUME Slurm、删除用户数据、修改 GPU 隔离、MIG、驱动、Kernel 或防火墙。
