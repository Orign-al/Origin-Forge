# 操作、审批与审计状态机

## Operation 生命周期

状态集合为：`DRAFT`、`PENDING_APPROVAL`、`APPROVED`、`QUEUED`、`RUNNING`、
`SUCCEEDED`、`FAILED`、`ROLLING_BACK`、`ROLLED_BACK`、`CANCELLED`、`EXPIRED`。
后端只允许定义好的相邻转换；非法跳转返回冲突，不通过直接数据库赋值绕过。

Portal-2 的成功含义是“Worker dry-run 已验证”，不是宿主变更已经执行。UI 和
`result_summary` 必须明确这一点。

## 创建任务

写请求先创建 `DRAFT` Operation，保存：operation/target type、精确 target id、请求人、
清洗后的 payload、幂等键、风险级别和摘要。HTTP 请求不长时间等待宿主命令。

创建草稿不会隐式进入审批。管理员必须显式提交，状态才转换为
`PENDING_APPROVAL`；对象名确认及高风险重新认证在提交和审批边界重新校验。

- payload 只允许每类 operation 的固定字段；未知字段被拒绝。
- 用户名、容器名、节点名、job id 和 quota 在 API 与 Worker 两层验证。
- `root`、`origin-al`、`codexops` 不能成为计算用户写操作目标。
- Portal-3A 仅允许 `user.plan(origin-pilot)` 为 Origin-al 生成独立计算身份 DRAFT；
  `origin-al` 始终保留为独立管理身份。
- Portal-3C 只允许既有 `portal3b-r-origin-pilot-stage-v1` DRAFT 经当前管理员控制台的
  精确批准文本进入真实 Stage；Stage 不要求公钥。
- Portal-3E-FINAL 只允许固定 `origin-pilot`、重新验收的 dry-run、唯一已批准 BOTH key
  record、精确管理员批准文本和一次性幂等键进入真实 Activate。API 与 Worker 均不接受
  宿主路径、私钥、密码、任意 argv 或其他计算身份。
- 同一请求人和幂等键返回既有任务，不重复执行。

## Portal-3D-R 自助 Key Operation

用户确认浏览器私钥已下载，或确认导入的是自己持有私钥对应的 `.pub` 后，API 创建内部
`ssh_key.enroll` Operation。它只记录 public metadata 与 fingerprint，不构成计算身份
Activate 审批。API 调用 `ssh_key.prepare` 将规范化 public key 写入 root-owned staging；
数据库提交失败时以同一 record/Operation/hash 调用 `ssh_key.discard`。私钥字段或内容在
创建 Operation 前拒绝，因此不会进入 Worker、数据库或普通审计。

登记完成的 record 是 `VALIDATED — NOT INSTALLED`。用户可登记最多五把独立 Key，Scope
可以是 HOST、CONTAINER 或 BOTH。增加 Key 不修改 shell、不创建 authorized_keys、不启动
容器，也不改变 STAGED。

有完整 HOST/CONTAINER Scope 后，Portal 可以创建 `user.activate` DRAFT 并调用 Worker
dry-run。Operation 保存 `execution_enabled=false` 的结构化计划，且不能复用 Stage 审批。
Portal-3E-FINAL 的真实 Operation 还必须引用重新验收的 dry-run；执行前重新运行同一
effective-config 与资源 preflight，之后才经 Root Worker 完成事务式 Activate。

真实 Activate 成功后，Key 为 `INSTALLED`、计算身份为 `ACTIVE`、Host/Container SSH
服务端为 `READY_FOR_CLIENT_VALIDATION`，两项客户端验证仍为 `PENDING`。这不会自动
RESUME Slurm，也不会把调度状态描述为可用。

Activate 完成后的容器启动使用独立 `container.start` Operation，而不是复用 Activate 或
接受任意容器参数。它只能由受管资源所有者发起，API 固定绑定当前 managed user、容器名
和 ACTIVE/STOPPED/INSTALLED 预期状态。Worker 在执行前后独立复核持久公钥 fingerprint、
GPU NONE、容器隔离、精确挂载及脚本 hash；任何失败都调用固定 stop 脚本并重新读取停止
后置条件。无法证明 STOPPED 时 observed state 标记为 UNKNOWN 并要求人工复核。当前 STAGED
状态不能创建可执行的容器启动 Operation。

## 高风险门槛

Slurm DRAIN/RESUME、用户 Activate/Suspend、删除、quota/QOS、运行容器停止、GPU 隔离
和数据删除等操作需要：

1. 输入精确对象名二次确认；
2. 10 分钟内重新认证；
3. 后端逐操作 RBAC；
4. 独立审批记录；
5. Worker 执行前重新检查当前状态与脚本完整性。

前端隐藏按钮不构成授权。`platform_owner` 可审批；其他角色按权限矩阵受限，auditor
只读，user 只能访问自己的资源。

## Portal-5A-1A / Portal-5A-1B 计算资源申请与受控 Stage

首次计算资源申请以 Portal Account 为 owner，因为此时 Managed Compute Identity 尚不存在。
普通用户只通过 `/self/compute-request` 创建、读取和撤回自己的 `REQUESTED` 申请；API、
唯一约束和 `active_slot` 同时保证每个账号只有一条进行中的首次开户申请。申请 schema 固定为
GPU 0/1、300GiB、`STANDARD_8CPU_32GB` 和 96 小时首次 Lease，并拒绝 UID、Project ID、
SSH Port、Docker 参数或其他基础设施字段。提交继续强制 session、Origin、CSRF 与按 IP/账号
rate limit。

只有 `platform_owner` 和 `platform_admin` 可以审批、生成 Plan 或执行 dry-run；operator、
普通用户及申请人本人均不能管理该申请。批准只将申请置为 `APPROVED`，不创建 Linux 用户、
Container、Quota、Slurm Association、GPU policy 或 Lease。allocator 在数据库行锁内结合
宿主身份库、XFS Project ID、Docker、监听端口、Slurm、Portal managed resources 与现有
reservation 选择候选值，并建立 24 小时 Portal DB reservation。过期 reservation 释放其
active key，Plan 标记为 `EXPIRED`，申请返回 `APPROVED` 等待重新规划。

`compute.provision.dry_run` 重新绑定申请、Plan、owner 和五项精确 reservation，并调用 Root
Worker 做只读宿主 preflight。通过后 Plan 为 `READY_FOR_PROVISION`，此时仍固定
`execution_enabled=false`，Lease 的 starts/expires 保持为空。

Portal-5A-1B 只在原申请批准保持不变、reservation 未过期、精确 dry-run Operation 存在、
管理员最近重新认证且本人不是申请人的前提下开放 `compute.provision.stage`。API 先持久化
`PROVISIONING/RUNNING` 再调用 hash-pinned Root Worker；Worker 重新执行宿主 preflight，随后
以固定 argv 创建 nologin/密码锁定 Linux identity、300GiB XFS quota、`company/general`
association、per-UID GPU 隔离，以及 8 CPU/32GiB/GPU NONE 的停止容器。Stage 不安装
authorized_keys、不监听容器 SSH 端口、不启动 Container 或 Lease，也不重复审批原申请。
成功后进入 `KEY_ENROLLMENT_PENDING`，五项 reservation 转为 `CONSUMED`；响应不确定或
检测到残留时资源坐标进入 `FAILED_HOLD`，必须人工对账，不能自动重试。

Stage 失败后 Request 的运营状态可以为 `FAILED`，但 `approved_at`、审批 actor 和唯一审批 Audit
保持不变；每一次 Provision 都由独立 `attempt_number` 表达。管理员不能从 `FAILED` 直接创建
Plan，必须先经最近重新认证调用 `compute.provision.retry_authorize`。该 Gate 锁定并验证失败
Plan/Stage Operation、原 payload 与五项 reservation UUID/值、`NO_SIDE_EFFECT` 或
`PARTIAL_ROLLED_BACK` 分类、全部 `RELEASED`、Portal 零资源，并让 Root Worker 只读复核实时
宿主零残留和修复脚本完整性。授权只把 Request 转为 `RETRY_AUTHORIZED` 并写 Operation/Audit，
不创建任何资源。

授权后的 allocator 必须插入新的 Plan row、新的五项 reservation rows 以及新的 Operation 和
idempotency key；旧 Plan 保持 `FAILED`，旧 Reservations 保持 `RELEASED`，旧 Stage Operation
保持 `FAILED`。新 Plan 通过 `previous_plan_id`、`failed_stage_operation_id` 和
`retry_authorization_operation_id` 保存完整 lineage。`PARTIAL_ROLLBACK_FAILED`、
`PARTIAL_UNKNOWN`、`FAILED_HOLD`、缺项或任何绑定漂移均要求人工对账，不能进入 retry Gate。

## 单用户 Lease 续期与恢复审批策略

每个 `PortalManagedUser` 有独立的 `lease_renewal_approval_required` 策略，默认值为 `true`，
同时控制到期前 Lease 续期与到期后回收站恢复。
只有具备 `users.write` 的管理员会话可以通过用户详情页修改；请求仍要求 CSRF，变更会记录
前后值、目标 managed identity 和未改变的待审申请数。策略切换不追溯处理已经创建的
`REQUESTED` 申请，申请行上的 `approval_required` 是创建时的策略快照。

关闭审批仅影响该用户后续新申请。后端仍在 Lease 行锁内检查 24 小时续期窗口、单次最长
96 小时、活动 Lease、资源策略、重复待审申请和幂等键；全部通过后才创建连续 successor
Lease。自动批准记录 `LEASE_RENEWAL_AUTO_APPROVED`，`decided_by` 保持 `NULL`，从而不会伪造
管理员审批人。策略开启时保持现有人工审批流程。普通用户不能读取或修改其他用户策略。

回收站恢复同样在申请行保存 `approval_required` 快照。策略开启时，owner 只能将资源推进到
`REQUESTED/RESTORE_PENDING`；owner/admin 完成最近密码重新认证并批准前，不创建恢复
Operation、不调用 Worker、不创建新 Lease，也不启动容器。auditor 只能查看，operator 和
普通用户不能访问管理员审批 API。策略关闭时，后端完成 owner、回收资源、Lease、SSH key、
时长与安全边界校验后自动恢复，并记录 `RESOURCE_RESTORE_AUTO_APPROVED`。策略切换不会改变
已有待审批恢复；同一资源的重复点击复用原申请，期限不同则返回冲突。

迁移降级在仍有策略约束的 `REQUESTED` 恢复申请时 fail closed。回滚前必须通过正式审批或
拒绝流程清空这些申请，不能直接更新数据库。

人工续期审批使用管理 Portal 的独立“续期审批”页面，而不是到期后的“恢复申请”页面。
`platform_owner` 与 `platform_admin` 拥有 `lease.renewals.read` 和
`lease.renewals.review`，可以查看并在 CSRF 与最近重新认证通过后批准或拒绝；`auditor` 只有
`lease.renewals.read`，只能查看；`operator` 与普通用户均无权读取或处理。前端导航按同一
角色边界展示，但后端权限是最终授权依据。

Lease 到期后，原 `REQUESTED` 续期不再可执行。周期性到期服务将其幂等收敛为
`CANCELLED`，记录 `LEASE_RENEWAL_CANCELLED`，并保留已经进入回收或后来恢复的资源状态；
管理员页面只把该记录显示为“已失效”历史。管理员不得在到期后批准旧续期，用户必须走
正式恢复流程。

## 多 GPU Job 审批

普通用户的 GPU Development 权限允许直接提交 GPU 0 或 1 的 Job。GPU 2、3、4 必须同时
提供模型名称、模型架构、框架与版本、参数量、训练或推理任务、数据集、并行策略以及可衡量
或预期的多卡扩展收益。Backend 在创建请求时锁定 owner/Lease，保存不可变脚本内容及
SHA-256，并将 Job 和 Operation 分别置为 `APPROVAL_PENDING` 与 `PENDING_APPROVAL`。
这一阶段不调用 Worker，不创建 Slurm Job，不分配 GPU，也没有运行日志。

`jobs.gpu_approval.read` 和 `jobs.gpu_approval.review` 只授予 `platform_owner` 与
`platform_admin`。审核接口仍要求 Session、CSRF 和最近重新认证；operator、auditor 与
普通 user 的直接 API 调用由 Backend 拒绝，不能依赖前端隐藏。审核人必须填写意见，可以
批准 1 至用户申请数量之间的 GPU，或驳回；不得增加用户申请数量。审批时 Backend 在行锁内
重新验证 owner、活动 Lease、剩余时间、GPU entitlement、脚本 hash 与审批状态，再把包含
审批 ID、申请/批准数量、审核人和审核时间的不可变 contract 交给 Worker。Worker 独立拒绝
无审批、字段不全、脚本 hash 不符、数量不符或 QoS 不符的多卡请求。

直接 Job 始终使用单卡 `general` 调度策略。批准 2 至 4 张的 Job 使用专用受控调度策略；
association 的单 Job 上限、单用户聚合上限和 QoS 上限共同约束单用户最多同时占用 4 张
H100。该策略只调和 `/etc/h100-platform/users/*.state` 代表的受管 Portal 用户，不自动授权
无生命周期记录的其他 Slurm 用户。用户可在审批前取消自己的申请，此操作只更新 Portal
状态，不调用 Worker 或 `scancel`。

## 高内存 Job 审批

每个 Job 请求不超过 32 GiB（32768 MiB）时不需要额外内存审批。请求超过 32 GiB 时，
用户必须提交任务说明、预估内存用量拆分以及高内存必要性；Backend 保存不可变脚本快照和
SHA-256，将 Job 保持为 `APPROVAL_PENDING`，审批完成前不调用 Worker、不创建 Slurm Job。
实际最大值由当前单节点 Slurm `RealMemory=486377 MiB` 合约约束，API、Worker 与数据库均
执行同一上限验证。

内存审批与多 GPU 审批是独立维度。一个 Job 同时触发两种审批时，两项都通过后只提交一次；
任一项仍待审批时不调用 Worker，任一项驳回则 Job 终止且另一待审项取消。管理员可以把批准
内存降低到不高于用户申请且不超过调度上限的值，不能提高用户申请。只有 platform_owner 与
platform_admin 可以读取和审批；审核要求 CSRF 与最近密码重新认证。审批材料只保存在 Portal，
不会传给 Worker、Slurm 环境或作业日志。

## Portal-3C 执行边界

唯一 DRAFT 依次进入 `PENDING_APPROVAL → APPROVED → QUEUED → RUNNING`。API 只有在
requester、approver、target、payload、审批引用和幂等键全部精确匹配时才向 Worker 发送
`dry_run=false`。Worker 返回并通过后置条件验证后，Operation 才进入 `SUCCEEDED`，Portal
managed identity 才写为 STAGED。本阶段：

- 仅创建并 Stage `origin-pilot`，准备精确 quota、association、GPU policy 和停止容器；
- 不 Activate、Suspend 或创建第二个用户；
- 不安装 SSH 公钥，不启用普通 shell，不允许登录；
- 不执行真实 job cancel；
- 不 DRAIN 新原因，也绝不 RESUME Slurm；
- 所有其他真实写 handler 保持关闭。

自助公钥登记不需要管理员在服务器准备文件；Activate 必须重新规划、验证并获得独立明确
审批，不能复用 Stage 批准。

Stage 脚本失败后，Worker 必须重新检查 Linux 用户/组、UID/GID、home/data、state、精确
GPU policy、registry、project mapping、Slurm association、容器和 Guard；不能只凭 state
文件缺失宣称完整回滚。发现任一残留或适配器状态不可读时返回 `PARTIAL_RETAINED` 并进入
人工复核。只有宿主资源已证明全部不存在、Worker dry-run 再次 READY 且管理员重新提交
同一精确批准时，固定管理员 CLI 才可将该 `ROLLED_BACK` Operation 以原幂等键重新打开为
`DRAFT`；HTTP 调用者不能直接重开终态 Operation。

## 审计

任务创建、审批、状态转换和 Worker 结果分别写 operation event 与 audit event。审计记录
actor、role、来源 IP、User-Agent digest、对象、operation id、结果、时间和安全 metadata。
递归脱敏 password、token、secret、Cookie、session、私钥、authorized key body、数据库
密码和 MUNGE key。SSH key 只允许类型、fingerprint 和截断 comment 摘要。
