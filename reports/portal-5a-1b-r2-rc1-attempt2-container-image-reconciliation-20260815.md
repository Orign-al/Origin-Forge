# 《Portal-5A-1B-R2-RC1 Attempt #2 Failed Stage Reconciliation 与 CONTAINER_IMAGE Root Cause Report》

## 结论与边界

本报告对应 `PORTAL-5A-1B-R2-RC1`。调查使用 Portal 数据库只读事务、既有 Root Worker/
宿主只读探针、宿主审计/Journal；本地新增 `retry_verify` 仅在隔离 fixture 中测试，没有对生产
调用。没有执行真实 Reconcile、Rollback、Reservation release、Stage、Docker lifecycle、
Attempt #3 或任何手工数据库修改。

```text
PORTAL-5A-1B-R2-RC1: PASSED

ROOT CAUSE KNOWN
SIDE EFFECTS RECONCILED OR GAPS IDENTIFIED
FAILED_HOLD PRESERVED
NO ATTEMPT #3
```

生产状态的最后一次可用只读采样约为 `2026-08-15 18:04:28 +08:00`；数据库时区为
`Asia/Shanghai`。本报告中的时间均保留生产记录的时区和精度；没有把调查时间当作
历史 Operation 时间。

## 1. Request / Attempt lineage

| 对象 | 实际值 | 状态 |
|---|---|---|
| Request | `25aafaf9-b4f8-4cb7-beb0-127ed9923d83` | `FAILED` |
| User | `origin-pilot2` | Portal account 保留 |
| Original approval count | `1` | 未重复审批 |
| Retry authorization | `9f848d24-ebfc-4db4-8239-6e1a868a75ab` | `SUCCEEDED`，actor `origin-al` / `platform_owner` |
| Attempt #1 / Plan #1 | `4160b0d8-612e-406a-92cc-00c3af9e8356` | `FAILED / ROLLED_BACK / PRESERVED` |
| Attempt #2 / Plan #2 | `70c75dac-71ba-47b6-9e4d-fc10dc1dddfb` | `FAILED` |
| Dry-run #2 | `964e1de1-240a-4a26-a3d6-105d0d87a07a` | `SUCCEEDED / READY_FOR_PROVISION` |
| Stage #2 | `981a7fa1-f246-4e6c-bf70-291537291fb6` | `FAILED`，actor `origin-al` / `platform_owner` |

Stage #2 的正式结果没有被覆盖：

```text
ERROR: COMPUTE_STAGE_FAILED
FIRST FAILED STEP: CONTAINER_IMAGE
LAST SUCCESSFUL STEP: SLURM_ASSOCIATION
ROLLBACK: REQUIRES_MANUAL_REVIEW
SIDE-EFFECT CLASSIFICATION: PARTIAL_UNKNOWN
```

没有 Attempt #3、Plan #3、Reservations #3、Lease 或 SSH enrollment。

## 2. Stage #2 forward step matrix

以下矩阵由 Stage Operation、Worker journal、平台 audit、Slurm transaction history 和宿主
零残留探针交叉核对；`NOT_REACHED` 表示没有证据表明该 forward step 执行。

| Forward step | 结果 | 证据/说明 |
|---|---|---|
| Explicit confirmation gate | `SUCCEEDED` | Dry-run #2 的 argument 13/14 均绑定有效；不是本次失败点 |
| Linux identity | `CREATED`，随后补偿清理 | `origin-pilot2` user/group 在 `15:27:48` journal 中出现；最终 absent |
| GPU policy apply/self-test | `SUCCEEDED`，随后 remove | `15:27:48` audit/journal；最终 drop-in/registry absent |
| Private storage / container-data | `CREATED`，随后清理 | Stage 顺序和 audit 均显示已进入；最终两处路径 absent |
| Cross-user filesystem probe | `SUCCEEDED` | Stage 继续到 quota，未报告隔离失败 |
| XFS project mapping/quota | `CREATED`，随后恢复 | Project `30002` 的 before backup 与当前文件 `cmp=0`；最终 mapping/quota absent |
| Slurm association | `CREATED/MODIFIED`，随后删除 | 最后成功 step；transaction IDs 21–23 为 add/modify，24–26 为 remove |
| Container image | `FAILED` | 进入 validator 后在 build 前失败 |
| Derived image build | `NOT_REACHED` | derived image absent |
| Container definition/create | `NOT_REACHED` | Docker container absent，端口 `22024` 未监听 |
| Portal Managed Compute Identity | `NOT_REACHED` | Portal row absent |
| Lifecycle state / Lease | `NOT_REACHED` | Lease count `0`，`starts_at`/`expires_at` 为 `NULL` |
| SSH enrollment / authorized_keys | `NOT_REACHED` | key row 和 container authorized_keys absent |

Stage 开始于 `2026-08-15T15:27:47.505418+08:00`，结束于
`2026-08-15T15:27:51.075727+08:00`（`+08:00`）。

## 3. `CONTAINER_IMAGE` 的真实语义与根因

旧生产 handler 的 `CONTAINER_IMAGE` 不是泛化的“镜像拉取”；它是从已存在的
`gpu-dev-origin-pilot` 取得 base image reference/ID，检查本地 image inspect、身份 labels、
`Config.User`，然后才允许构建 derived image。失败发生在 derived image build/container create
之前。

生产 base artifact 的脱敏身份如下：

```text
REFERENCE:
h100-local/dev-container:ubuntu24.04-origin-pilot-20260804

IMAGE ID:
sha256:db7ee2c41b7b0e0b6c67ab2302d936e6df87abbef4ef7c718b98f1763ec4efec

REPO DIGEST:
h100-local/dev-container@sha256:db7ee2c41b7b0e0b6c67ab2302d936e6df87abbef4ef7c718b98f1763ec4efec

CREATED:
2026-08-08T02:26:06.27784275+08:00

CONFIG.USER:
NULL (Docker effective default: root)

IDENTITY LABELS:
h100.dev.user=origin-pilot
h100.dev.uid=20001
h100.dev.gid=20001
```

旧 predicate 是：

```jq
.Config.User == "" or .Config.User == "root"
```

Docker 对省略的 `Config.User` 返回 JSON `null`，所以该 predicate 对这份合法 root-default
镜像返回 false，实际安全错误为：

```text
FIRST LOW-LEVEL FAILURE:
origin-pilot image does not build derived users as root
```

因此：

```text
CONTAINER_IMAGE FAILURE TYPE: IMAGE_BUILD_USER_INVALID (null/default-root handling bug)
EXPECTED IMAGE: h100-local/dev-container:ubuntu24.04-origin-pilot-20260804
EXPECTED DIGEST: sha256:db7ee2c41b7b0e0b6c67ab2302d936e6df87abbef4ef7c718b98f1763ec4efec
ACTUAL IMAGE STATE: present; labels and artifact ID valid; Config.User omitted/null
ROOT CAUSE: old validator treated Docker null Config.User as non-root
```

### Dry-run 与 Stage 同时成立的解释

旧 Dry-run 的 `_standard_dev_image_available()` 只检查 source container、`.Image` 和
image inspect 是否成功，不保存 tag、RepoDigest、labels、Created 或 `Config.User`。Stage
则执行了更强的 validator。因此：

```text
DRY-RUN IMAGE CHECK: PASS (source container/image ID inspect only)
STAGE IMAGE CHECK: FAIL (strong Config.User predicate)
BASE ARTIFACT ID: SAME
DRY-RUN FULL IMAGE CONTRACT: NOT RECORDED BY OLD RUNTIME
STAGE FULL IMAGE CONTRACT: REFERENCE/DIGEST/LABELS/USER READ
IMAGE IDENTITY MATCH: ARTIFACT ID=YES; FULL CONTRACT=NO/UNBOUND
```

没有发现 Dry-run 后 artifact 被替换、RepoDigest/Created 改变或 reference 指向另一个
image 的证据：

```text
TOCTOU: NO EVIDENCE
DRY-RUN EXECUTOR: local Root Worker
STAGE EXECUTOR: local Root Worker
DOCKER CONTEXT: default
DOCKER SOCKET: /var/run/docker.sock
LOOKUP: old Dry-run source ID inspect; Stage tag inspect + ID check + jq validator
```

这不是 argument 13/14 回归：

```text
R1 POSITIONAL PARAMETER ROOT CAUSE: NOT RECURRED
ARGUMENT 13: EXPLICIT_STAGE_CONFIRMATION_FLAG / VALID
ARGUMENT 14: CONFIRMED_TARGET_USERNAME / VALID (origin-pilot2)
```

## 4. Forward side effects 与 rollback

Stage forward 写入确实越过了 Linux、storage/quota、GPU policy 和 Slurm association；这不能
只凭当前宿主为空推断“从未写入”。补偿链中已确认：

1. Slurm transaction `21–23` 创建/修改 association，`24–26` 删除 association、account
   coordinates 和 user；当前 association absent。
2. GPU policy remove 成功；当前 per-UID drop-in 和 registry absent。
3. `userdel --remove origin-pilot2` 成功，并自动删除同名 private group。
4. 旧脚本随后仍执行 `groupdel origin-pilot2`；目标 group 已不存在，命令返回非零。
5. 脚本因此设置 `manual_review=1` 并输出 `PARTIAL_ROLLBACK_FAILED`。Worker 在 retained
   resources 为空但 rollback declaration 不可一致证明时，降级为
   `PARTIAL_UNKNOWN / REQUIRES_MANUAL_REVIEW`。

这就是 `PARTIAL_UNKNOWN` 的具体来源，不是 Docker stop/image API 的未知响应，也不是保留
资源本身仍存在的证明。

XFS rollback backup：

```text
/srv/gpu-platform/platform/backups/compute-stage-origin-pilot2-20260815-152748-178685417
projects.before SHA256: d7fb11343713f1fc1a62b3431914f9030ecc9b943d9761528331776f6bf9567c
projid.before SHA256:   a29ce8cc02c867cccd1acaa5bb7e9da9f1b9d4a1de10648f109294dda393b1d2
projects.before == /etc/projects: YES (cmp=0)
projid.before == /etc/projid:     YES (cmp=0)
```

### Rollback evidence matrix

| Compensation step | 真实观察 | 当前状态 |
|---|---|---|
| Container / compose / derived image | 未到达或未创建 | absent |
| Slurm association | delete transactions 24–26 | absent |
| XFS mapping/quota | before files restored | absent，且 backup `cmp=0` |
| GPU policy | remove audit | absent |
| Container/private storage | cleanup evidence | absent |
| Linux user | `userdel --remove` success | absent |
| Linux private group | implicitly removed by `userdel`; explicit `groupdel` returned non-zero | absent, evidence marker historically inconsistent |
| Lifecycle state | no active target state | absent |

## 5. 只读 reconciliation evidence

```text
HOST RESIDUE OBSERVED: NONE
```

逐项结果：

| 资源 | 只读结果 |
|---|---|
| Linux user/group and UID/GID 20002 | absent |
| `/home/origin-pilot2` | absent |
| managed storage / container-data | absent |
| XFS ProjectID `30002` mapping/quota | absent |
| per-UID GPU policy / registry | absent |
| Slurm association and jobs | absent；node `IDLE` |
| Docker `gpu-dev-origin-pilot2` | absent |
| derived image | absent |
| SSH port `22024` | not listening |
| Portal Managed Compute Identity | absent |
| Portal storage rows | `0` |
| Portal Lease rows | `0`; `NOT_STARTED` |
| Portal SSH key rows | `0` |
| container authorized_keys | absent |
| Other object using same numeric/name values | none observed |

这些是 formal reconcile 的输入证据；本阶段没有把它们写回 Operation/Plan/Request，也没有
将 `PARTIAL_UNKNOWN` 改成 `ROLLED_BACK`。

## 6. Reservations #2

五条 Reservation 均属于 Plan #2，`active_key` 保留且状态仍为 `FAILED_HOLD`：

| Kind / value | Reservation ID | State |
|---|---|---|
| UID / `20002` | `3fac6fc0-19d7-4fcd-9988-8d11deaceea5` | `FAILED_HOLD` |
| GID / `20002` | `86f28884-c6cc-4101-b810-2cbbd8b6065b` | `FAILED_HOLD` |
| PROJECT_ID / `30002` | `b0d06339-fcc4-4f42-99e4-6f6d60f05b02` | `FAILED_HOLD` |
| SSH_PORT / `22024` | `4331901f-066a-4b3d-ac36-cc6a8a32ea52` | `FAILED_HOLD` |
| CONTAINER_NAME / `gpu-dev-origin-pilot2` | `a67a3cdd-405d-48d3-98df-615b78f33ae6` | `FAILED_HOLD` |

Attempt #1 的五条 Reservation ID 完全不同，均为 `RELEASED / PRESERVED`，没有 row reuse。
`FAILED_HOLD SAFETY FUNCTION: WORKING`：在 rollback 证据未被正式验证前，allocator 不会把
这些坐标发给其他对象。RC1 没有释放或编辑任何一行。

## 7. Origin-pilot 与平台隔离复核

`origin-pilot` 未被本阶段触碰：Lease 仍为 `RECYCLE_BIN`，container 仍为 stopped/exited，
数据、quota、UID/GID、SSH public-key record 和 Slurm history 保留；没有 Restore、Renew 或
Activate。`origin-pilot2` 没有因此获得任何 compute identity、container、Lease 或 key。

生产健康只读结果：

```text
CONTROL-PLANE HEAD: 23258b3ae38bd4610afcf6d148b0e9638270aaab
WORKER/API/WEB: ACTIVE
EXPIRY TIMER: ACTIVE / ENABLED
GPU BYPASS GUARD: PASSING
SYSTEMD FAILED UNITS: 0
SLURM: AVAILABLE; NODE=IDLE; QUEUE=EMPTY; NO DRAIN/DRAINING/DOWN
GPU: 4 x H100 PRESENT
DCGM: 4/4 PASS
MIG: DISABLED
```

## 8. 控制面修复（仅本地实现，未部署）

### Image contract / Stage fix

- `_standard_dev_image_identity()` 现在以安全摘要记录 reference、image ID、RepoDigest、
  Created、labels 和 `Config.User`；`null`/空值按 Docker effective default root 处理，并要求
  RepoDigest 的 digest 与 image ID 绑定。
- Dry-run 保存完整 image contract；Stage 在调用脚本前比较 contract，任何 identity drift
  返回 `DRY_RUN_STAGE_CONTRACT_MISMATCH`，副作用为 `NO_SIDE_EFFECT`。
- `h100-provision-stage` 在第一笔 Linux/storage/quota/Slurm 写入前执行 image pre-write gate，
  并用与 image ID 对应的 pinned RepoDigest 构建（`--pull=false --network=none`）；没有临时联网
  pull，也不会把裸 `sha256:<id>` 当作 registry reference。
- rollback 对 `userdel` 自动删除 private group 采用 idempotent postcondition，并输出结构化
  `COMPUTE STAGE ROLLBACK STEP: NAME=STATE` markers。

本地新脚本 SHA256：

```text
c65c6577af89c8ae26ee215e9b8646493eae17c2bd79fd98d885c4b2adc4546c
```

本地控制面实现 commit：`b3b7b7dbebe6651d73cbbbf2857ab57671ccb9c1`；该 commit 尚未部署到
生产，因此生产 runtime 仍按下方旧 hash 运行。

生产当前仍是旧 handler SHA256：

```text
ad03b8fbf310e755ba06055e3cc6dfaa5430be6bc450d3d058a75d1327ad02e4
```

### Formal Failed-Provision reconciliation workflow

本地新增 source-only recovery contract：

```text
POST /api/v1/admin/compute-resource-requests/{request_id}/reconcile-failed-provision
```

它要求 `platform_owner`、CSRF、recent re-auth、typed Request/Plan/failed Stage IDs、review
note 和 idempotency key；锁定精确五条 `FAILED_HOLD` rows，调用 Root Worker 只读零残留
验证，再在同一事务中把 Stage rollback evidence 标为 `ROLLED_BACK`、释放仅绑定的五条
reservation，并保持 Request/Plan/历史 Stage/Original approval 不变。该 workflow 不创建
Attempt #3，且重复请求只返回同一成功 Operation。

该 API 仅在本地源码和隔离测试中存在，**未部署到生产，未对真实 Attempt #2 调用**。

## 9. Tests / quality gates

| Gate | 实际结果 |
|---|---|
| Worker image/reconciliation targeted tests | `10 passed` |
| Worker full `test_portal5a1a_worker.py` | `33 passed` |
| Affected backend direct tests (isolated DB) | `41 passed` |
| Full worker suite | `130 passed`; 3 unrelated sandbox `socketpair/sendall PermissionError` failures，不是代码断言失败 |
| mypy（隔离 Python 3.14-syntax tree，6 个受影响源码文件） | `Success: no issues found` |
| Ruff check | passed |
| Ruff format check | passed |
| `bash -n scripts/h100-provision-stage` | passed |
| `worker-scripts.json` JSON + stage hash | passed，manifest 与 `c65c65…` 一致 |
| Full API TestClient suite | 未宣称通过；本地 Python 3.12 与项目 Python 3.14/FastAPI test-client 组合在 sandbox hang，未改变生产 |

没有运行会触发真实 Stage/Reconcile 的 Playwright 或生产 API 调用。

## 10. 当前状态与下一 Human Gate

```text
ATTEMPT #2: FAILED / PRESERVED
PLAN #2: FAILED
STAGE #2: FAILED / REQUIRES_MANUAL_REVIEW
ROLLBACK: PARTIAL_UNKNOWN (evidence gap identified)
HOST RESIDUE: NONE OBSERVED
OPERATION RECONCILIATION: INCOMPLETE (not applied)
SAFE TO FORMALLY MARK ROLLBACK VERIFIED: NO — requires approved formal operation
SAFE TO RELEASE FAILED_HOLD AFTER FORMAL AUTHORIZATION: YES, conditional on fresh zero-residue verification
FAILED_HOLD RELEASED: NO
REAL RECONCILIATION EXECUTED: NO
ATTEMPT #3: NOT CREATED
```

建议的正式操作顺序（必须另行获批，且先部署已测试控制面）：

1. `platform_owner` 在 Portal 打开失败 Request，确认 Request/Plan/Stage/五条 Reservation
   的 typed binding、recent re-auth 和当前宿主零残留。
2. 通过正式 `Reconcile Failed Provision / Verify Rollback` Operation（上列 endpoint 或其
   产品化 UI）执行 Root Worker `retry_verify`；任何 residue/unknown/绑定漂移都 fail closed，
   保持 `PARTIAL_UNKNOWN + FAILED_HOLD`。
3. 只有验证成功，才在同一正式 Operation 中将 Attempt #2 标为 `FAILED / RECONCILED`、
   rollback 标为 `VERIFIED`，并释放这五条 `FAILED_HOLD`；不改变原审批，不创建 Attempt #3。
4. 另行的人类 Gate 才能决定是否重新授权 Provision；本报告不执行 Retry Authorization、
   Plan、Dry-run 或 Stage。

```text
========================================
ATTEMPT #2 RECONCILIATION AUTHORIZATION REQUIRED
========================================

PROPOSED ACTION:
Reconcile Failed Provision / Verify Rollback / Release Failed Hold

CURRENT SAFETY STATE:
PARTIAL_UNKNOWN + FAILED_HOLD preserved

REAL RECONCILIATION:
NOT EXECUTED

NO ATTEMPT #3
```
