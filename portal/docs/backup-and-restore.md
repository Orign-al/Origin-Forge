# Portal 备份与恢复

## 备份范围

备份对象分为：

- Git 中的 Portal 源码、迁移和部署模板；
- PostgreSQL `h100_portal` 逻辑备份；
- `/etc/h100-portal` 配置和 Worker 脚本 hash allowlist；
- systemd unit；
- 必要的 `/var/lib/h100-portal` 和审计日志。

`portal.env`、数据库 dump 和审计日志均视为敏感数据，不提交 Git。备份目录必须 root-only
并使用批准的离线加密与保留策略。不得备份明文 setup token、session Cookie、SSH 私钥、
MUNGE key 或 SlurmDBD 密码。

## 一致性备份顺序

1. 记录 Git HEAD、Alembic revision、PostgreSQL 版本和服务状态。
2. 如需严格静止点，先停止 Web/API，保留 Worker socket和 Slurm DRAIN。
3. 使用 `pg_dump --format=custom` 对单一 `h100_portal` 数据库做逻辑备份。
4. 复制 root-owned 配置、unit 和脱敏审计证据；计算 SHA-256 清单。
5. 恢复 Web/API 并执行 live/ready 检查。

不得为备份停止 Slurm、修改 GPU 策略或删除容器/用户数据。数据库备份包含密码 hash、
token hash 和 session hash，仍需按 credential 数据保护。

## 恢复顺序

1. 保持 Portal Web/API 停止，Slurm 保持 DRAIN。
2. 验证备份来源、权限、hash、PostgreSQL major version 与目标 Git commit。
3. 恢复独立数据库和配置；不覆盖 SlurmDBD MariaDB 或 Grafana 数据库。
4. 运行 `alembic upgrade head`，再运行 `alembic current` 和 `alembic check`。
5. 安装匹配 commit 的只读运行代码和 unit，验证 systemd sandbox。
6. 撤销恢复出的所有活动 session 和未使用 setup token；轮换 Portal secret 与数据库密码。
7. 启动 Worker socket、API、Web，执行健康和 Worker smoke。
8. 核对审计时间线并由管理员重新签发必要的密码设置链接。

恢复不自动 RESUME Slurm，也不自动启用 Guard timer、创建 Linux 用户或启动用户容器。

## 恢复演练

至少验证：dump 可读、迁移可完成、账号大小写唯一约束仍在、旧 token/session 无法使用、
RBAC 和 CSRF 仍生效、Worker socket 非 API UID 被拒绝、Web 精确监听
`10.10.10.2:18080`、API 仍仅监听 localhost。演练结果只记录 hash、版本和结论，不记录
credential 内容。
