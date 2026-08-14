# 《Portal-5A-1B-R2-LX1 origin-pilot 过期 Lease / CONTAINER_STOP_FAILED 根因与恢复计划验收报告》

## 1. Gate 结论

```text
PORTAL-5A-1B-R2-LX1: PASSED

ROOT CAUSE: KNOWN
SOURCE REMEDIATION: IMPLEMENTED AND COMMITTED
PRODUCTION DEPLOYMENT: DEFERRED TO LX2
REAL ORIGIN-PILOT RECOVERY APPLY: NOT EXECUTED
HUMAN APPROVAL: REQUIRED
```

LX1 仅完成生产只读调查、隔离代码修复、回归测试和正式 Recovery Plan。没有停止真实
Container，没有修改真实 Lease/数据库，没有执行 recycle retry，也没有创建
`origin-pilot2` Attempt #2。

## 2. 管理通道与边界

| 项目 | 已验证值 |
|---|---|
| Management source | `ubuntuserver` |
| SSH endpoint | `codexops@10.82.36.1:22` |
| Remote host | `sagsh100server` |
| Client fingerprint | `SHA256:vRb0Vno7tl/hcmiFeDiekEKTx1zsA/oPHvpsSoG3Vwc` |
| H100 ED25519 host fingerprint | `SHA256:9TMlLHNTv1g+PxhdP5v7VcakCn7MofahpNpgJGt/vYs` |
| SSH policy | `IdentitiesOnly=yes`, password auth disabled, strict host-key checking |
| Private key | 未读取、未打印、未复制、未暴露 |

Codex management transport 是 SSH/codexops；Portal lifecycle transport 仍是：

```text
Portal API
  -> /run/h100-portal/worker.sock
  -> h100-portal-worker.service (root)
  -> fixed resource.recycle handler
  -> /usr/local/sbin/h100-container-stop
```

没有把 Portal lifecycle 改成 SSH transport。

## 3. 时间、Lease 与 Invariant

数据库最终只读快照：

```text
DATABASE TIME: 2026-08-15 03:39:31.175856+08:00
DATABASE TIMEZONE: Asia/Shanghai

LEASE ID: 54314628-7b46-4986-bfbd-895f97e0e70f
STARTS AT: 2026-08-10 13:24:55.083442+08:00
RENEWAL AVAILABLE AT: 2026-08-13 13:24:55.083442+08:00
EXPIRES AT: 2026-08-14 13:24:55.083442+08:00
TIME EXPIRED: YES
DATABASE STATE: ACTIVE
expired_at: NULL
recycled_at: NULL
COMPUTE ENVIRONMENT STATE: ACTIVE
```

因此：

```text
current_time > expires_at AND state == ACTIVE
LEASE STATE INVARIANT: VIOLATED
```

## 4. Recycle Operation 链与真实根因

```text
OPERATION ID: 90964d2b-c1e0-4c12-a036-2b49c11d8558
OPERATION TYPE: lease.expire
STATUS: FAILED
ERROR CODE: CONTAINER_STOP_FAILED
CREATED AT: 2026-08-14 13:25:18.576332+08:00
LAST OBSERVED START: 2026-08-15 03:39:11.271199+08:00
LAST OBSERVED FINISH: 2026-08-15 03:39:11.330160+08:00
STRUCTURED ATTEMPT COUNT: ABSENT (legacy implementation)
STRUCTURED NEXT RETRY: ABSENT (legacy implementation)
OPERATION EVENTS: ABSENT (legacy implementation)
```

到 `2026-08-15 03:26:50+08:00` 的宿主快照：

```text
expiry processed=1/recycled=0 runs since expiry: 803
container-stop FAILED audit records: 803
Docker stop events since expiry: 0
```

失败调用链：

```text
lease.expire
  -> resource.recycle
  -> /usr/local/sbin/h100-container-stop origin-pilot
  -> h100_require_slurm_drained
  -> Slurm node is IDLE, not DRAINED
  -> script exits before docker stop
```

`h100-container-stop` 的 repository source 与 deployed artifact 在调查时均为：

```text
d39d1033b0d14c75779ad60ffefdc8731fb429bae694aa0abb1e733a2e7d484c
```

真实根因不是 Docker daemon 故障，也不是 Container ID drift；它是错误的全节点
`DRAINED` 前置条件。单个经 Worker 再验证为 GPU-none、非 privileged 的 dev Container
停止，不应要求整个 H100 Slurm node DRAIN。

## 5. Portal / Docker 身份与 Container 安全

| 项目 | Portal / Docker 实际值 |
|---|---|
| Portal container ID | `42d06e4e-f0c3-4b57-b867-12828bcea37f` |
| Docker ID | `25bd54fc858bd54248105a66dc36e87f4d61bf41aeac70c044df9bb7cbcc0643` |
| Name | `gpu-dev-origin-pilot` |
| Owner label | `origin-pilot` |
| Portal image digest | `sha256:8a57d2f72a6b956aa132102a3ec36b4a1341ecf3b296133a8ecccb9244bea417` |
| Docker manifest digest | 同上 |
| Portal/Docker identity | `MATCH` |
| Runtime state | `RUNNING` |
| PID | `1376921` |
| StartedAt | `2026-08-09T08:21:42.985444489Z` |
| Restart policy | `unless-stopped` |
| Privileged | `false` |
| DeviceRequests | `null` |
| Devices | `null` |
| Direct GPU | `NONE` |
| Docker socket mount | `ABSENT` |
| MUNGE mount | `ABSENT` |
| SSH listener | `10.82.36.1:22023` |

挂载仅包含保留的用户 `home`、`workspace`、`shared` 与 Container SSH host keys；未读取
用户文件内容或 Container 环境变量。

## 6. 活跃会话与 Slurm Gate

```text
ACTIVE PORTAL SESSION COUNT: 0
ESTABLISHED CONTAINER SSH COUNT: 0
RUNNING SLURM JOB COUNT: 0
PENDING SLURM JOB COUNT: 0
COMPLETING SLURM JOB COUNT: 0
GLOBAL QUEUE: EMPTY
SLURM NODE: IDLE
```

没有读取用户终端内容、命令、stdout 或 stderr。

## 7. 到期访问控制结论

现有 production Portal 代码按可信服务器时间动态拒绝：

```text
new Web Terminal: DENIED
new Portal Job: DENIED
Container start/restart: DENIED
Lease renewal: DENIED
Portal connection response: available=false
```

但真实状态同时为：

```text
10.82.36.1:22023: LISTENING
Portal SSH key record: INSTALLED / active
Container authorized key: INSTALLED
```

没有用用户身份尝试认证，但端口和授权 key 同时保留，不能证明新的直接 Container SSH
被拒绝。因此分类为：

```text
LEASE EXPIRY AUTHORIZATION: FAIL
SEVERITY: P0
GAP: EXPIRED DIRECT CONTAINER SSH REVOCATION
```

## 8. Expiry side-effect matrix

| Side effect | 实际状态 |
|---|---|
| Pending jobs cancelled | `NOT REQUIRED`（0） |
| Running jobs handled | `NOT REQUIRED`（0） |
| Container stop | `FAILED BEFORE docker stop` |
| Portal new access | `DENIED BY expires_at` |
| Direct Container SSH authorization | `STILL INSTALLED` |
| Lease state | `ACTIVE`（违反时间 invariant） |
| Compute environment state | `ACTIVE` |
| Recycle item | `NOT CREATED` |
| Data | `PRESERVED` |
| Quota / Project ID | `PRESERVED` |
| Linux UID/GID | `PRESERVED` |
| Slurm history | `PRESERVED` |
| Public key record | `PRESERVED` |
| Permanent delete | `DISABLED / NOT EXECUTED` |

这是 `expired + cleanup failed + container/key still active` 的 half-recycle 状态。

## 9. 数据、Quota 与 Linux Identity

```text
DATA ROOT: /srv/gpu-platform/users/origin-pilot
DATA OWNER: origin-pilot:origin-pilot (20001:20001)
DATA: PRESENT
PROJECT ID: 30001 / h100_origin-pilot
XFS PROJECT QUOTA: ENFORCED
HARD LIMIT: 314572800 KiB (300 GiB)
DATABASE QUOTA: 322122547200 bytes
LINUX SHELL: /usr/sbin/nologin
PASSWORD: LOCKED
HOST SSH: DISABLED_BY_PLATFORM_POLICY
SSH KEY RECORD: PRESERVED
SLURM HISTORY: PRESERVED
```

未执行删除、移动、truncate、chown、quota release、Project ID release、UID/GID 变更或
public-key record 删除。

## 10. 自动恢复为何失败

现有 timer 每分钟扫描 due Lease。legacy 实现复用同一 Operation，把它重新设为
`RUNNING`，调用相同失败 handler 后再设为 `FAILED`，并把 Lease 恢复为原 `ACTIVE`。

缺失项：

- 无持久化 attempt count；
- 无 backoff；
- 无 max retry；
- 无 next retry timestamp；
- 无 manual-review/dead-letter 状态；
- 无 OperationEvent 链；
- cleanup 失败错误地恢复合法 `ACTIVE` entitlement。

所以 803 次调用均在同一 Slurm DRAIN gate 失败，且没有自动收敛。

## 11. Source remediation

正式 source commit：

```text
a3f6024b15b7ab53e71853d273e69aad04fab04d
Fail closed expired lease recycle recovery
```

实现内容：

1. Lease 到期后 cleanup 失败不再回滚成 `ACTIVE`；持久化为 `EXPIRED`，compute
   environment 为 `SUSPENDED`，Container desired state 为 `STOPPED`。
2. Worker 在 Slurm/Container cleanup 前原子移动 Container `authorized_keys` 到保留的
   recycle 路径，先拒绝新 SSH，再执行后续步骤。
3. Operation JSON 记录 versioned、machine-readable cleanup evidence；不保存 argv、
   stderr、key 内容或其他 secret。
4. 自动 retry 最多 3 次，backoff 为 60 秒、300 秒；超过上限进入 manual review。
5. Legacy `FAILED` Operation 无可信 retry evidence 时不再无限自动重放。
6. Pending Job 直接取消；Running Job 先 TERM、再受控 cancel；Slurm history 不删除。
7. Recycle handler 保持幂等，不重复创建 recycle item，不删除 data、quota、UID、Project ID。
8. 移除 GPU-none dev Container stop 对全节点 `DRAINED` 的错误依赖；Worker 仍先验证
   owner、GPU-none、non-privileged、无 Docker socket、无 MUNGE。
9. 新增正式 Portal Recovery API：

```text
GET  /api/v1/admin/lease-recovery-incidents
POST /api/v1/admin/compute-leases/{lease_id}/recycle-retry
```

POST 仅允许 `platform_owner`，要求 session CSRF、recent reauthentication、准确 Lease ID
typed confirmation、idempotency key，并通过 fixed local Root Worker 执行和审计。

10. 将 `h100-container-stop` candidate hash 纳入 `worker-scripts.json`，并由正式
    `install-runtime.sh` 安装 root-owned artifact；中断部署时 integrity mismatch 会
    fail closed。

## 12. Candidate artifact identities

| Artifact | Candidate SHA256 |
|---|---|
| `scripts/h100-container-stop` | `ff798161b632c8bf61c1fac30bfb9e455cc31bee17c34cf7eb353247a082e752` |
| `expiry_service.py` | `6e0998da5852698316603ba86b6b02c8a4135348725d698a4f643e5db321bbcb` |
| Worker `handlers.py` | `9c600b228eb4e88e7457c2cd43bdd0fe025e4b5ee639ed08a9330cb49d1857df` |
| `worker-scripts.json` | `750fbec391129a23ce0bcd62a4d4b87353f827cec8241139c7bc22ea5d209eac` |
| `install-runtime.sh` | `92ff0fdbfa0120f138ea662bbb72aedaaab3fcdd46eae6f632fbcc3e2cf2114f` |

## 13. Regression 与质量门

隔离 clone：`/tmp/portal-5a-1b-r2-lx1-test`。未使用真实 `origin-pilot` 作为写测试对象。

覆盖：

- Container stop 首次失败后 entitlement 仍立即过期；
- key 在 Container stop 前挂起，新 SSH authorization 被撤销；
- backoff 后 retry 成功进入 `RECYCLE_BIN`；
- legacy failure 不自动无限重放；
- repeated recycle 幂等；
- running Slurm job 受控取消；
- DB 错误保留 `ACTIVE` 时仍按 `expires_at` 拒绝 Portal 操作；
- ordinary user 拒绝、platform_owner + CSRF + reauth + typed confirmation gate；
- sensitive stderr marker 不进入 Worker result；
- Container stop 不再要求 global Slurm DRAIN；
- source/manifest/runtime installer hash binding。

真实结果：

```text
Bash syntax: PASS
ShellCheck: PASS
Ruff: PASS
Ruff format check: 78 files already formatted
MyPy strict: Success, 37 source files
pytest: 237 passed, 1 upstream Starlette deprecation warning
Frontend: NOT RUN (no frontend source changed)
Playwright: NOT RUN (no frontend source changed)
```

## 14. Migration

没有 model/table/column 变更；新增 evidence 使用既有 Operation JSON 字段。

```text
ALEMBIC CURRENT: e2a4c6f810b3 (head)
ALEMBIC HEAD: e2a4c6f810b3
ALEMBIC CHECK: No new upgrade operations detected
SCHEMA DRIFT: NONE
PRODUCTION DB WRITE: NONE
```

## 15. Production deployment 状态与延期原因

```text
PRODUCTION GIT HEAD: 7d1fbeb0c3146c0bcad944c0fea0dce7c2468bfd
PRODUCTION TRACKED WORKTREE: CLEAN
API SERVICE: ACTIVE
ROOT WORKER SERVICE: ACTIVE
WORKER SOCKET: root:h100-portal-api 0660
RUNTIME DEPLOYMENT OF a3f6024: NOT EXECUTED
SERVICE RESTART: NOT EXECUTED
```

Production source/runtime 仍一致：

| Artifact | Production source/runtime SHA256 |
|---|---|
| `h100-container-stop` | `d39d1033b0d14c75779ad60ffefdc8731fb429bae694aa0abb1e733a2e7d484c` |
| `expiry_service.py` | `b909de894fd8e4066b27e79f486ae6517c4988a2652f171fac93dd7182dc51e9` |
| Worker `handlers.py` | `524d90c374bb322c4a9a86d3f9856f575ecb374e2aed7c367b6f3e348cdd397a` |

没有在 LX1 部署，因为 expiry timer 正在每分钟运行。只更新 stop artifact、Worker 或 API
中的任一部分都会形成竞态或临时 contract mismatch；完整部署则可能立即对真实
`origin-pilot` 执行 key suspension/Container stop，等同 LX2 Recovery Apply，超出 LX1
授权。故 source 已提交，runtime deployment 与真实 Recovery 必须在同一个已批准 LX2
维护窗口完成。

## 16. 平台健康

```text
SLURM: AVAILABLE
NODE: IDLE
QUEUE: EMPTY
GPU INVENTORY: 4 x NVIDIA H100 PCIe
GPU TEMPERATURES: 29, 30, 29, 29 C
DCGM 4.6.1 LEVEL 1: GPU0 PASS, GPU1 PASS, GPU2 PASS, GPU3 PASS
MIG: DISABLED on GPU0..GPU3
GPU BYPASS GUARD: PASSED (managed_users=1, users_verified=1)
SYSTEMD FAILED UNITS: 0
```

未 DRAIN Slurm、未重启 Docker/Slurm/DCGM/sshd/network，未执行裸 Guard 程序。

## 17. origin-pilot2 隔离证明

```text
PORTAL ACCOUNT: ACTIVE
RESOURCE ONBOARDING: NOT_ENROLLED
REQUEST 25aafaf9-b4f8-4cb7-beb0-127ed9923d83: FAILED
ORIGINAL APPROVAL AUDIT COUNT: 1
PLAN #1: 4160b0d8-612e-406a-92cc-00c3af9e8356 / FAILED
ATTEMPT #2 PLAN COUNT: 0
ATTEMPT #2 RESERVATION COUNT: 0
MANAGED IDENTITY COUNT: 0
LEASE COUNT: 0
LINUX USER: ABSENT
LINUX GROUP: ABSENT
CONTAINER: ABSENT
COMPUTE: NOT PROVISIONED
LEASE: NOT STARTED
```

## 18. LX2 正式 Recovery Plan

### Preconditions

1. 管理员明确批准 `Portal-5A-1B-R2-LX2`。
2. 再次确认 Lease ID、过期时间、Operation ID、Container identity、data/quota。
3. 再次确认 origin-pilot pending/running/completing Slurm jobs 为 0；若出现 running job，
   按批准的 controlled job policy 处理，不能跳过。
4. 确认 Alembic current=head、systemd failed units=0、candidate commit/hash 与本报告一致。

### Control-plane deployment

1. 在正式维护窗口暂停 `h100-portal-lease-expiry.timer`，只阻止竞态，不修改 Lease。
2. 将 commit `a3f6024b15b7ab53e71853d273e69aad04fab04d` 合并/快进到 production source。
3. 使用正式 `portal/deploy/scripts/install-runtime.sh` 一次部署 API、Worker、manifest 和
   root-owned `h100-container-stop`；不使用随机 `cp`。
4. 重启且仅重启实际变更的 Portal API/Worker，验证 service、socket、artifact hash 和
   recovery route；不重启 Docker、Slurm、DCGM、sshd 或 network。
5. 在 human apply 前保持 expiry timer 暂停。

### Human Recovery Apply

1. 管理员本人以真实 `platform_owner` Portal session 登录。
2. 完成 recent reauthentication，使用 session CSRF。
3. 选择 Lease `54314628-7b46-4986-bfbd-895f97e0e70f`，输入准确 Lease ID，提交正式
   `recycle-retry` Operation；Codex 不读取或代用管理员凭据。
4. Root Worker 首先挂起 Container authorized key，然后确认/cancel job，执行固定
   `h100-container-stop`，验证 GPU-none/STOPPED/key suspended/data preserved。
5. API 在同一业务事务中记录新 Operation、OperationEvent、Audit、Lease/Container/
   key/storage/recycle-item 状态。

### Expected result

```text
Lease: RECYCLE_BIN
Compute environment: RECYCLED
Container: STOPPED
New Portal connections: DENIED
New direct Container SSH: DENIED
Data: PRESERVED
Quota / Project ID: PRESERVED
Linux UID/GID: PRESERVED
Slurm history: PRESERVED
Public key record: PRESERVED / SUSPENDED_BY_RECYCLE
Permanent delete: NO
```

### Fail-closed behavior

如果 Container stop 或后置验证仍失败：

```text
Lease entitlement: EXPIRED (not ACTIVE)
Compute environment: SUSPENDED
New key authentication: DENIED when suspension verified
Container desired state: STOPPED
Container observed state: actual runtime state
Cleanup: bounded retry or platform_owner manual review
Data/quota/UID/Project ID/history: PRESERVED
Permanent delete: NO
```

验证完成后恢复 expiry timer，并确认没有重复 recycle item、没有意外 Compute write、
systemd failed units 为 0。

## 19. Mandatory Stop / Human Gate

```text
REAL ORIGIN-PILOT RECOVERY APPLY: NOT EXECUTED
CONTAINER STOP MANUALLY: NO
DATABASE MANUALLY MODIFIED: NO
LEASE MANUALLY MODIFIED: NO
ORIGIN-PILOT2 ATTEMPT #2: NOT CREATED

NEXT GATE:
PORTAL-5A-1B-R2-LX2
ORIGIN-PILOT LEASE RECOVERY APPLY
```

管理员必须明确批准 LX2，才允许部署该候选并对真实 `origin-pilot` 执行正式 Recovery
Operation。
