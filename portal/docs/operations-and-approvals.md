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

## Portal-5A-1A 计算资源申请

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
Worker 做只读宿主 preflight。通过后 Plan 为 `READY_FOR_PROVISION`，但固定
`execution_enabled=false`，Lease 的 starts/expires 保持为空。本阶段的真实 Provision API
始终返回 `PROVISION_EXECUTION_DISABLED_NEXT_GATE`；只有后续管理员阶段才能增加执行路径。

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
