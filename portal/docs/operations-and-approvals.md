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
  Worker 仍禁止真实 stage/activate，且 `origin-al` 始终保持 NOT_ENROLLED。
- Portal-3C 只允许既有 `portal3b-r-origin-pilot-stage-v1` DRAFT 经当前管理员控制台的
  精确批准文本进入真实 Stage；Stage 不要求公钥。Activate 仍关闭，且未来只能带已批准
  key record UUID，不能带宿主路径、私钥、密码或任意 argv。
- 同一请求人和幂等键返回既有任务，不重复执行。

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

后续公钥上传和 Activate 必须重新规划、验证并获得独立明确审批，不能复用 Stage 批准。

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
