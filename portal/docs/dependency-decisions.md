# Portal-0/1 依赖来源与版本决策

调查时间：2026-08-06（服务器本地时区 UTC+08:00）。所有版本均在实施时从官方稳定
元数据端点读取，拒绝 alpha、beta、rc 和 `latest` 镜像标签。

## 运行时

| 组件       |                  选择 | 来源与理由                                                                |
| ---------- | --------------------: | ------------------------------------------------------------------------- |
| Node.js    | 24.19.0 LTS (Krypton) | `https://nodejs.org/dist/index.json` 当时最新 LTS；官方 Linux x64 tarball |
| pnpm       |               11.20.0 | npm 官方 registry 的稳定 `latest`；要求 Node >=22.13；由 Corepack 固定    |
| Python     |                3.14.4 | Ubuntu 26.04 系统 Python；使用独立 venv，不覆盖系统包                     |
| PostgreSQL |                    18 | Ubuntu 26.04 官方仓库候选版本；不用 Docker 镜像                           |

Node.js 文件 `node-v24.19.0-linux-x64.tar.xz` 的官方 SHA-256 和本地计算值均为：

```text
14b342e71204f811bde6153be8e04b62aef63c236fef92b55f9c83154b409647
```

校验清单来自同一版本目录的 `SHASUMS256.txt`。未使用 NodeSource 或任何
`curl | bash` 安装方式。

## 前端直接依赖基线

| 包                                 | 稳定版本 |
| ---------------------------------- | -------: |
| next                               |   16.3.0 |
| react / react-dom                  |   19.2.8 |
| typescript                         |    6.0.3 |
| tailwindcss / @tailwindcss/postcss |    4.3.3 |
| @tanstack/react-query              |  5.101.4 |
| @tanstack/react-table              |    9.0.0 |
| react-hook-form                    |   7.84.0 |
| zod                                |    4.4.3 |
| vitest                             |   4.1.10 |
| eslint                             |   9.39.5 |
| prettier                           |    3.9.6 |
| @playwright/test                   |   1.62.1 |

安装时 `package.json` 使用精确版本，`packageManager` 固定 pnpm，提交
`pnpm-lock.yaml`。构建不加载第三方 CDN、远程字体、分析脚本或遥测。

调查时 TypeScript 7.0.2 和 ESLint 10.8.0 已有稳定版本，但 Next.js 16.3.0 的当前插件和
类型链尚不能同时通过工程检查，因此固定为最新兼容稳定版 TypeScript 6.0.3 与 ESLint
9.39.5。未使用 alpha、beta 或 rc 规避兼容性。

## 后端直接依赖基线

| 包          | 稳定版本 |
| ----------- | -------: |
| FastAPI     |  0.141.1 |
| Pydantic    |   2.13.4 |
| SQLAlchemy  |   2.0.51 |
| Alembic     |   1.19.0 |
| psycopg     |    3.3.4 |
| argon2-cffi |   25.1.0 |
| Uvicorn     |   0.52.1 |
| HTTPX       |   0.28.1 |
| pytest      |    9.1.1 |
| Ruff        |   0.16.1 |
| MyPy        |    2.3.0 |

直接依赖在 `pyproject.toml` 精确固定；安装后的全部传递依赖写入
`requirements.lock`。Python 环境位于 `/opt/h100-portal/venv`，不写系统 Python。
