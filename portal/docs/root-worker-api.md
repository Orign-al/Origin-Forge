# Root Worker 固定操作 API

## 信任边界

Root Worker 是 Portal 唯一以 root 运行的组件。Web 不连接 Worker；API 通过
`/run/h100-portal/worker.sock` 访问。Socket 为 `root:h100-portal-api 0660`，Worker
使用 `SO_PEERCRED` 再次验证调用 UID 必须是 `h100-portal-api`。仅靠文件权限不视为
充分认证。

API 不挂载 Docker Socket、不读取 MUNGE key、SlurmDBD 密码、shadow 或任意宿主路径。

## 线协议

每个请求和响应为：4 字节 network-order 无符号长度 + UTF-8 JSON。frame 上限 64KiB；
超长、截断、非 JSON、未知字段或错误协议版本均被拒绝。

请求字段固定为：

```json
{
  "protocol_version": 1,
  "request_id": "UUID",
  "operation_type": "gpu.list",
  "payload": {},
  "requested_by": "normalized-login",
  "approved_by": null,
  "idempotency_key": "opaque-safe-key",
  "dry_run": false
}
```

Worker 不接受 `command`、`argv`、任意路径、任意 unit、任意 Docker 参数、任意 SQL 或
任意 `scontrol` 参数。Pydantic schema 使用 `extra=forbid`，Worker 独立重验所有 payload。

## operation allowlist

只读 operation：

- `platform.health.read`、`gpu.list`、`gpu.health.read`
- `slurm.node.read`、`slurm.jobs.read`、`slurm.history.read`、`slurm.accounts.read`
- `containers.list`、`containers.inspect`
- `storage.summary.read`、`quotas.list`、`systemd.failed.read`
- `monitoring.alerts.read`、`monitoring.summary.read`、`registry.status.read`、`images.list`、`gpu_isolation.status.read`

Portal-2 的只读适配器使用固定命令、固定参数和固定 URL：作业历史来自 `sacct -P`，
QOS/Association 来自 `sacctmgr -P`，镜像来自 Docker 的 digest 库存，监控来自 localhost
Prometheus/Grafana。GPU minor 只按 UUID + PCI Bus ID 与 NVIDIA driver procfs 联结，禁止
按 NVML index 猜测。

写框架 operation：

- `user.plan`、`user.stage`、`user.activate`、`user.suspend`
- `container.start`、`container.stop`、`container.restart`、`container.rebuild`
- `slurm.drain`、`slurm.resume`、`job.cancel`、`quota.update`
- `ssh_key.add`、`ssh_key.revoke`

Portal-3C 只开放一条真实写路径：`requested_by=approved_by=origin-al`、固定幂等键、固定
审批引用和逐字段匹配既有计划的 `user.stage(origin-pilot)`。Worker 在执行前再次运行
资源冲突检查并校验全部依赖脚本 hash，只能以 `shell=False` 调用固定
`h100-user-create --stage ... --confirm-stage origin-pilot` argv。所有 Activate 和其他
非 dry-run 写请求仍固定返回 `WRITE_EXECUTION_DISABLED`。

真实 Stage 使用有界长超时并在脚本成功后重新读取 STAGED state、UID/GID、nologin、密码
锁定、authorized_keys 缺失、精确 UID policy、project mapping、Slurm association、停止且
无 GPU 的容器、Guard timer、Slurm DRAIN 和空队列。相同幂等键只在这些后置条件仍精确
匹配时返回幂等成功；不匹配时要求人工复核，绝不重新创建资源。

失败分类必须检查实际宿主资源。尤其 `useradd` 可能先写入 passwd/group/subuid 数据再因
home 创建失败返回非零，因此 state 文件缺失不等于完整回滚。Worker 检测到用户、组、
路径、policy、mapping、association、容器、Guard 或不可读适配器时返回
`PARTIAL_RETAINED` 和安全资源类别；只有全部检查为空才返回 `ROLLED_BACK`。

`user.stage` 使用固定的 UID/GID、project、端口、Slurm、quota 和 GPU-less 容器字段，
不接受任何 SSH key 字段；Worker 返回 `NOT_REQUIRED_FOR_STAGE` 并明确公钥延后到
Activate。`user.activate` 只接受 `managed_user_id`、`approved_ssh_key_record_ids`、
`expected_state=STAGED` 和审批引用。Worker 从 root-owned UUID staging 目录读取记录，
拒绝任意路径、私钥、密码、argv 和 command；缺少 key 返回
`PUBLIC_KEY_REQUIRED_FOR_ACTIVATION`。幂等键继续位于签名/校验后的 Worker envelope，
不得在 payload 中用第二个可篡改字段覆盖。
API 还必须将 `managed_user_id`、目标 `origin-pilot`、Portal owner 和每个 key record 的
所属受管身份、active/revoked 与独立批准状态交叉绑定；不能只验证 UUID 格式。

Portal-3A 将 `user.plan` 进一步限制为精确目标 `origin-pilot`。Worker 使用固定数据源
生成 UID/GID、project ID、SSH 端口、Slurm、GPU isolation、Stage、Activate 和回滚的
结构化计划，所有候选值标记为 `PROPOSED — NOT RESERVED`。它不接受任意 argv、任意
路径或其他用户名；`origin-al` 仍是禁止的计算用户目标。

## 固定命令适配器

命令只从代码内绝对路径 allowlist 选择，`shell=False`，固定 `PATH`/locale/cwd，stdin
关闭，stdout/stderr 有上限且每次调用有超时。异常返回结构化错误或 `UNKNOWN`。

GPU 适配器使用 `nvidia-smi` 获取 NVML index、UUID、PCI Bus 和遥测，再按 PCI Bus ID
读取 `/proc/driver/nvidia/gpus/*/information` 的 `Device Minor`，同时核对 UUID。它不查询
本机不支持的 `minor_number` 字段，也不推断 index 等于 minor。映射不完整时整个 GPU
数据源为 `UNKNOWN`。

Slurm JSON 被规范化为稳定 node/job schema；若 JSON 不可用，才使用固定字段文本回退。
Docker inspect 只允许受管名称前缀并只返回白名单字段。

## 管理脚本完整性

写 dry-run 前检查十个既有 `h100-*` 脚本：

- `lstat` 必须是普通文件且不是 symlink；
- owner UID 必须为 0；
- group/other 不可写；
- SHA-256 必须匹配 `/etc/h100-portal/worker-scripts.json`。

配置缺失、hash 缺失或任意脚本不合格时 fail-closed 为 `SCRIPT_INTEGRITY_FAILED`。

## 运行验收

```bash
sudo -u h100-portal-api \
  /opt/h100-portal/venv/bin/python \
  /opt/h100-portal/tests/worker_socket_smoke.py
```

脚本覆盖所有只读 adapter、全部写 dry-run、唯一 Stage 写路径的 mock 成功/幂等/冲突/
hash/回滚测试、其他非 dry-run 拒绝、任意路径拒绝和未知 operation 拒绝。不得用真实
`user.stage` 或非 dry-run `slurm.resume` 做普通冒烟测试。
