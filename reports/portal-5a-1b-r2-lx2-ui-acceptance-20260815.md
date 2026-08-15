# 《Portal-5A-1B-R2-LX2-UI Admin Lease Recovery UI Binding 验收报告》

## 1. Gate 结论

```text
PORTAL-5A-1B-R2-LX2-UI: PASSED

ADMIN USER DETAIL: UPDATED
LEASE / LIFECYCLE UI: AVAILABLE
USER LIST LEASE DETAILS LINK: CLICKABLE
ORIGIN-PILOT COMPUTE DISPLAY: CONSISTENT WITH REAL PRODUCTION STATE

REAL ORIGIN-PILOT RECOVERY: NOT EXECUTED
RETRY RECYCLE BUTTON: NOT CLICKED
ORIGIN-PILOT2 ATTEMPT #2: NOT CREATED
```

本阶段只部署 LX1 后端 Recovery contract 与 LX2-UI 管理界面绑定。没有调用真实
`recycle-retry` POST，没有停止或删除真实 Container，没有修改 Lease/数据库，也没有启动
origin-pilot2 Provision Attempt #2。

## 2. Management transport

| 项目 | 已验证值 |
|---|---|
| Source | `ubuntuserver` |
| Endpoint | `codexops@10.82.36.1:22` |
| Remote host | `sagsh100server` |
| Client public fingerprint | `SHA256:vRb0Vno7tl/hcmiFeDiekEKTx1zsA/oPHvpsSoG3Vwc` |
| H100 ED25519 host fingerprint | `SHA256:9TMlLHNTv1g+PxhdP5v7VcakCn7MofahpNpgJGt/vYs` |
| SSH constraints | `IdentitiesOnly=yes`; password auth disabled; strict host-key checking |
| Private key | 未读取、未打印、未复制、未暴露 |

Portal lifecycle transport 仍是本地 Unix socket / Root Worker；Browser 不接收 service、Worker
或数据库 credential。

## 3. 原 UI 不一致的真实原因

用户列表以 `resource_onboarding_state` 显示 legacy Managed Compute Environment，因此
origin-pilot 为 `ACTIVE`。用户详情“计算资源”原先只识别新式
`compute_onboarding` / Provision Request；origin-pilot 是既存 Managed Compute Identity，
没有该新式 request 表示，因而错误进入“计算身份尚未配置”空状态。

修复没有创建虚假 Managed Compute Identity，也没有迁移或修改生产数据。详情页现在优先
显示真实 `PortalManagedUser` / Container / Lease 摘要；无 Managed Identity、无 Lease 的
origin-pilot2 仍显示未配置和 `—`。

## 4. UI 与只读数据模型

新增或修复：

- User Detail 的“租约 / 生命周期”Tab；
- 时间资格与数据库 state 分离显示；
- expired-by-time + `ACTIVE` 显示严重异常，而非普通绿色 ACTIVE；
- Recycle state、safe error、Operation ID/state、retry/manual-review、Container runtime、
  Portal connection authorization 与 Container SSH key state；
- User list 的 Lease“查看详情”为键盘可访问真实 link；无 Lease 时显示 `—`；
- 页面刷新重新从 API 读取，提交前强制 refetch，状态变化时
  `LEASE_RECOVERY_STATE_CHANGED` fail closed；
- Recovery Operation ID 与 typed state 显示。

生产 Web bundle 已确认包含：

```text
租约 / 生命周期
重试回收
输入完整 Lease ID 确认
LEASE_RECOVERY_STATE_CHANGED
```

## 5. 真实 production incident（部署后只读）

数据库时区为 `Asia/Shanghai`。部署后只读快照对应数据库时间
`2026-08-15T06:37:13.721422+00:00`（本地 `14:37:13+08:00`）：

```text
INCIDENT USER: origin-pilot
LEASE ID: 54314628-7b46-4986-bfbd-895f97e0e70f
STARTS AT: 2026-08-10T05:24:55.083442+00:00
EXPIRES AT: 2026-08-14T05:24:55.083442+00:00
TIME EXPIRED: YES
LEASE DB STATE: ACTIVE
expired_at: NULL
recycled_at: NULL
COMPUTE ENVIRONMENT STATE: ACTIVE

CONTAINER: gpu-dev-origin-pilot
PORTAL DESIRED STATE: RUNNING
PORTAL OBSERVED STATE: RUNNING
DOCKER RUNTIME: RUNNING
PRIVILEGED: FALSE
DEVICE REQUESTS: NONE

RECYCLE OPERATION: 90964d2b-c1e0-4c12-a036-2b49c11d8558
OPERATION STATUS: FAILED
ERROR CODE: CONTAINER_STOP_FAILED
RECYCLE ITEM COUNT: 0
ACTIVE KEY INSTALL STATE: INSTALLED
RECOVERY ELIGIBLE: YES
```

UI 将同时显示 expired、DB `ACTIVE`、Recycle `FAILED` 与
`CONTAINER_STOP_FAILED`，不会把时间已过期的 entitlement 误呈现为正常 ACTIVE。

## 6. Recovery UI security contract

```text
PLATFORM_OWNER ONLY: PASS
ORDINARY USER RECOVERY ACCESS: DENIED
CSRF: REQUIRED / PRESERVED
RECENT REAUTHENTICATION: REQUIRED
TYPED LEASE ID: FULL UUID REQUIRED
PRE-SUBMIT REFRESH: REQUIRED
AUDIT / OPERATION MODEL: PRESERVED
PERMANENT DELETE: NOT PART OF RECOVERY
```

`lease.recovery` 只赋予 `platform_owner`；`platform_admin`、operator、auditor 与 ordinary
user 均没有该 permission。无认证访问 production incident endpoint 返回 HTTP `401`。
Recovery POST runtime 继续调用 `require_session_csrf` 与
`require_recent_reauthentication`，并验证 body confirmation 与 URL Lease UUID 完全一致。

Recent re-auth 密码只发往正式 `/auth/reauthenticate`。Recovery payload 只含 typed Lease ID、
safe reason 与 idempotency key；没有 password、session、CSRF token、traceback 或内部 argv。

确认 Dialog 明确说明：停止过期开发 Container、暂停 Container SSH、进入
`RECYCLE_BIN`；保留 data、Quota、UID/GID、SSH public-key record 与 Slurm history；不会
永久删除、Restore、Renew、Reactivate 或启动新 Lease。

## 7. Regression 与质量门

隔离 clone：`/tmp/portal-5a-1b-r2-lx1-test`。所有 API、Vitest 与 Playwright Recovery
行为均使用 fixture/mock/test database；没有向真实 origin-pilot 发送 Recovery。

```text
ESLINT: PASS
TYPESCRIPT: PASS
VITEST: 28/28 PASS
PRETTIER: PASS (FULL REPOSITORY)
NEXT BUILD: PASS, 25 PAGES
PLAYWRIGHT CORE: 48 PASS, 22 CONFIGURED SKIP, 0 FAIL
LX2-UI PLAYWRIGHT: 2/2 PASS
RUFF: PASS
RUFF FORMAT: 78 FILES FORMATTED
MYPY STRICT: SUCCESS, 37 SOURCE FILES
PYTEST: 238 PASS, 1 UPSTREAM STARLETTE/HTTPX DEPRECATION WARNING
AFFECTED BACKEND TARGETED: 19/19 PASS
```

覆盖包括：

- platform_owner 看到 expired failed Lease 与 Recovery action；
- ordinary user 不收到管理员 Recovery action；
- expired + DB ACTIVE 异常警告；
- normal ACTIVE 与 RECYCLE_BIN 不显示 Retry；
- recent re-auth、CSRF 与完整 Lease UUID confirmation；
- submit 前 race refetch；
- Operation result 显示；
- User list Lease link；
- legacy compute state 正确显示；
- Recovery payload 不含 password/session/CSRF；
- stale R1 SSH 文案断言与当前正式 Container SSH activation 语义一致。

## 8. Source 与 deployment

```text
START PRODUCTION GIT: 7d1fbeb0c3146c0bcad944c0fea0dce7c2468bfd
LX1 BACKEND COMMIT: a3f6024b15b7ab53e71853d273e69aad04fab04d
LX1 REPORT COMMIT: fdb6f4ef4fe9d4da97ef6273ffd80f6bbe38bc4e
LX2-UI CODE COMMIT: 53af5e2bfffd223a4274f6a435fe071cd0968b22
PRODUCTION CODE DEPLOYMENT GIT: 53af5e2bfffd223a4274f6a435fe071cd0968b22
TRACKED WORKTREE: CLEAN
```

正式部署步骤：

1. Git bundle prerequisite 验证后，production source 从 `7d1fbeb` fast-forward 到
   `53af5e2`；
2. 固定 Node 24.19.0 / pnpm 11.20.0 执行 production `pnpm build`；
3. 执行仓库正式 `portal/deploy/scripts/install-runtime.sh`；
4. `systemd-analyze verify` 通过（只有系统既存 XFS CPUAccounting removed warning）；
5. 仅重启 `h100-portal-worker.service`、`h100-portal-api.service`、
   `h100-portal-web.service`；
6. 没有重启 Docker、Slurm、DCGM、sshd、network，也没有暂停 expiry timer。

部署后：

```text
WORKER: ACTIVE
API: ACTIVE
WEB: ACTIVE
EXPIRY TIMER: ACTIVE
WORKER SOCKET: /run/h100-portal/worker.sock root:h100-portal-api 0660
API /health/live: OK
API /health/ready: READY, worker=true, database=true
WEB /login: HTTP 200
SYSTEMD FAILED UNITS: 0
```

## 9. Runtime binding

| Artifact | Source/runtime SHA256 |
|---|---|
| `expiry_service.py` | `6e0998da5852698316603ba86b6b02c8a4135348725d698a4f643e5db321bbcb` |
| API `self_service.py` | `9f215576538eb76eba018d9dae3c2b7a4450614d67a00f719711364721c132ac` |
| API `users.py` | `44e0adc0585c5f0778c94aeebae25a8de877fe837275909c2a08d74025bf9333` |
| Worker `handlers.py` | `9c600b228eb4e88e7457c2cd43bdd0fe025e4b5ee639ed08a9330cb49d1857df` |
| Next BUILD_ID | `6feb8611e4d64f1d9b1565003b009d9d0fc17091f7adce537514474fc9aa3158` |
| `h100-container-stop` | `ff798161b632c8bf61c1fac30bfb9e455cc31bee17c34cf7eb353247a082e752` |
| Worker manifest | `750fbec391129a23ce0bcd62a4d4b87353f827cec8241139c7bc22ea5d209eac` |

API/Worker import 路径均为 `/opt/h100-portal/venv/lib/python3.14/site-packages/...`，hash 与
production source 相同。Web ExecStart 为固定 standalone `server.js`，source 与 runtime
BUILD_ID 相同。

## 10. Migration

无 model/table/column 变更；新增只读摘要使用既有数据结构。

```text
ALEMBIC CURRENT: e2a4c6f810b3 (head)
ALEMBIC HEAD: e2a4c6f810b3
ALEMBIC CHECK: No new upgrade operations detected
SCHEMA DRIFT: NONE
```

没有执行 production upgrade、downgrade、直接 ALTER 或手工数据库修改。

## 11. Timer 与真实 resource 零写入证明

部署后的 timer 在 `14:33`、`14:34`、`14:35`、`14:36`、`14:37 +08:00` 均正常结束：

```text
processed=1 recycled=0
```

legacy failed Operation 没有 `lease-recycle-cleanup-v1` bounded evidence，因此 LX1 runtime
将其保留为 manual-review incident，不自动 replay。Operation 最后 started/finished 仍是部署前
`14:31:58 +08:00`；多次新 timer 周期没有调用真实 Recovery。

```text
REAL ORIGIN-PILOT RECOVERY: NOT EXECUTED
CONTAINER MANUALLY STOPPED: NO
LEASE MANUALLY MODIFIED: NO
DATABASE MANUALLY MODIFIED: NO
CONTAINER RUNTIME: RUNNING
LEASE DB STATE: ACTIVE
RECYCLE ITEM COUNT: 0
```

## 12. origin-pilot2 isolation

```text
REQUEST 25aafaf9-b4f8-4cb7-beb0-127ed9923d83: FAILED
ATTEMPT #1 PLAN: 4160b0d8-612e-406a-92cc-00c3af9e8356 / FAILED
ATTEMPT #2 COUNT: 0
MANAGED IDENTITY COUNT: 0
LEASE COUNT: 0
COMPUTE: NOT PROVISIONED
LEASE: NOT STARTED
```

## 13. Platform health

```text
SLURM NODE: IDLE
QUEUE: EMPTY
GPU INVENTORY: 4 x NVIDIA H100 PCIe / ACTIVE
MIG: DISABLED on GPU0..GPU3
GPU BYPASS GUARD: latest systemd Result=success, ExecMainStatus=0
GPU BYPASS GUARD TIMER: ACTIVE
SYSTEMD FAILED UNITS: 0
```

Guard 仅通过 systemd 状态复核，没有裸调用 Guard 主程序。

## 14. Mandatory Human Gate

自动化已证明 production bundle、backend route、权限 gate 与真实只读 incident 数据。由于
Codex 不读取或使用 platform_owner password/session/CSRF，最终 Browser visual check 由管理员
本人完成：

```text
========================================
LX2 RECOVERY UI READY — DO NOT CLICK YET
========================================

origin-pilot
  -> Lease / 生命周期
  -> 54314628-7b46-4986-bfbd-895f97e0e70f

EXPECTED DISPLAY:
EXPIRED
DB ACTIVE
RECYCLE FAILED
CONTAINER_STOP_FAILED

BUTTON:
重试回收

STATE:
NOT CLICKED

NEXT GATE:
PORTAL-5A-1B-R2-LX2
ORIGIN-PILOT LEASE RECOVERY APPLY
========================================
```

管理员不得向 Codex 提供 password、session cookie、CSRF 或 token。只有管理员明确批准并进入
LX2 Apply，才允许真实 Recovery。
