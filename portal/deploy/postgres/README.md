# PostgreSQL deployment note

Portal 使用 Ubuntu 官方 PostgreSQL 18，独立数据库和角色均为 `h100_portal`。当前仅通过
本机 Unix Socket 连接，不使用 Docker 镜像，不复用 SlurmDBD MariaDB 或 Grafana 数据库。

运行时数据库 URL 和随机密码只保存在 `/etc/h100-portal/portal.env`
（`root:h100-portal-api 0640`）。本目录只保存非敏感说明；schema 变更位于
`apps/api/alembic/versions`，必须显式执行 Alembic。
