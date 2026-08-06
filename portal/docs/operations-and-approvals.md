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

## Portal-0/1 执行边界

审批通过后任务进入 `QUEUED`，后台调用 Worker 时始终设置 `dry_run=true`。Worker 返回
计划后任务可进入 `SUCCEEDED`；返回拒绝、超时或 schema 错误则进入 `FAILED`。本阶段：

- 不创建、Stage、Activate 或 Suspend 真实 Linux 用户；
- 不改变 quota、QOS、GPU 隔离或容器状态；
- 不执行真实 job cancel；
- 不 DRAIN 新原因，也绝不 RESUME Slurm。

未来启用写执行必须经过独立阶段审批、为每个 handler 增加当前状态检查、备份、验证、
幂等和精确回滚，并移除代码中的全局写禁用开关后重新做安全验收。

## 审计

任务创建、审批、状态转换和 Worker 结果分别写 operation event 与 audit event。审计记录
actor、role、来源 IP、User-Agent digest、对象、operation id、结果、时间和安全 metadata。
递归脱敏 password、token、secret、Cookie、session、私钥、authorized key body、数据库
密码和 MUNGE key。SSH key 只允许类型、fingerprint 和截断 comment 摘要。
