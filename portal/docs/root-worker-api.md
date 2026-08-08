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
- `ssh_key.prepare`、`ssh_key.discard`

Portal-3C 开放的宿主资源写路径是：`requested_by=approved_by=origin-al`、固定幂等键、固定
审批引用和逐字段匹配既有计划的 `user.stage(origin-pilot)`。Worker 在执行前再次运行
资源冲突检查并校验全部依赖脚本 hash，只能以 `shell=False` 调用固定
`h100-user-create --stage ... --confirm-stage origin-pilot` argv。所有 Activate 和其他
非 dry-run 写请求仍固定返回 `WRITE_EXECUTION_DISABLED`。

Portal-3D-R 额外开放两个内部固定操作，它们只管理 public-key staging，不修改 Linux
用户或 `authorized_keys`：

- `ssh_key.prepare` 仅接受 canonical UUID、固定 `origin-pilot`、managed-user/Operation
  绑定、规范化 public key、类型、fingerprint、内容 hash 和 Scope；
- `ssh_key.discard` 仅接受同一 record/Operation/hash，用于 API 事务失败时安全清理；
- 两者要求 `requested_by=approved_by` 且幂等键与 record UUID 精确绑定；
- staging 目录是 `/var/lib/h100-portal/ssh-key-staging`，`root:root 0700`；`.pub` 与
  `.meta.json` 均由 UUID 命名、`root:root 0600`、单链接普通文件；
- Worker 使用 directory FD、`O_NOFOLLOW`、`O_EXCL`、flock、fsync 和同目录原子 rename，
  读取时核对 owner/mode/link/inode/size；
- sidecar 绑定 record ID、enrollment Operation ID、managed user ID、fingerprint、hash 和
  Scope，Activate dry-run 逐项与 Portal 数据库记录交叉验证。

自助登记只传 public key。Worker 若在任何字段发现私钥装甲，返回
`SSH_PRIVATE_KEY_UPLOAD_REJECTED`，不得写 staging。真正 `user.activate dry_run=false`
依然返回 `WRITE_EXECUTION_DISABLED`。

Portal-3D-R 同时定义了一个与通用容器管理分离的自助启动闭合路径。API 端点
`POST /api/v1/containers/{name}/start` 只允许当前会话所属的受管容器，并只接收服务端
定义的幂等键及 `ACTIVE`/`STOPPED`/`INSTALLED` 三个精确预期状态。API 不接收 username、
command、argv、路径或 Docker 参数；这些字段由数据库中的受管身份生成。Worker 的真实
`container.start` 只接受这份完整闭合 payload，重新验证：

- target 必须是 `origin-pilot` 与 `gpu-dev-origin-pilot` 的精确绑定；
- 生命周期 state file 为 root 管理的 `ACTIVE`/`INSTALLED`，Linux shell 为 `/bin/bash`
  且密码仍锁定；
- 容器持久 home 中 `authorized_keys` 的 owner/mode/fingerprint 与 Activate state 一致；
- 容器仍为 STOPPED、GPU NONE、非 privileged、非 host network/PID/IPC、无 Docker Socket，
  且只有四个批准的精确 bind mount；
- `h100-container-start` 和 `h100-container-stop` hash 均通过 allowlist。

启动脚本失败或启动后健康/GPU/安全复核失败时，Worker 固定调用 stop 脚本，并重新 inspect
证明容器确实 STOPPED 后才返回普通失败。无法证明停止时返回
`CONTAINER_STOP_RECOVERY_FAILED`，Portal 将 observed state 降为 UNKNOWN，绝不显示虚假的
STOPPED/RUNNING。旧式仅含 `{name}` 的通用 `container.start` payload 仍没有真实执行路径。
当前 `origin-pilot` 为 STAGED，因此本阶段无法触发该自助启动路径。

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
所属受管身份、`VALIDATED`/active/revoked 状态、enrollment Operation、fingerprint、hash
和 Scope 交叉绑定；不能只验证 UUID 格式。HOST 与 CONTAINER 必须都有至少一条有效
Scope 覆盖，否则返回 `SSH_KEY_SCOPE_INCOMPLETE`。

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
hash/回滚测试、受管容器启动的身份/状态/hash/资源/挂载/失败停止测试、其他非 dry-run
拒绝、任意路径拒绝和未知 operation 拒绝。不得用真实 `user.stage`、STAGED 用户容器启动
或非 dry-run `slurm.resume` 做普通冒烟测试。
