# H100 GPU Platform 用户手册

版本：`PORTAL-5A-FULL-PLATFORM-RELEASE + PORTAL-5A-USER-CLI-JOBS-1`
正式入口：<http://20.10.10.3:18080/>
适用角色：普通用户

## 1. 平台介绍

H100 GPU Platform 提供普通用户自助的开发容器、私有持久化存储、网页终端、容器 SSH 和 Slurm CPU/GPU 作业。

- `CPU Development / STANDARD_8CPU_32GB` 是默认 Profile：8 CPU、32 GiB 内存，开发容器无 GPU Device。
- `GPU Development / GPU_1_8CPU_32GB` 必须显式选择：8 CPU、32 GiB 内存，并获得 H100 Slurm Job 权限。
- 开发容器是常驻、无 GPU 的环境；H100 只在 GPU Job 运行期间由 Slurm 分配，并在 Job 结束后自动释放。
- 1 张 GPU 可直接提交；2、3、4 张 GPU 必须提供完整的模型与扩展说明并等待管理员审批。单用户同时最多占用 4 张 GPU。
- 每个用户有 300 GiB 私有持久化存储，`/workspace` 与 `/home/<用户名>` 都属于同一用户配额。
- Lease 有效期为 96 小时。到期后环境进入可恢复的回收流程，持久化数据不会立即删除。
- Host SSH 关闭；用户只连接自己的开发容器。

## 2. EasyTier 连接

Portal 和容器 SSH 都通过 EasyTier 网络访问。不要使用或保存服务器物理管理地址。

1. 安装平台管理员提供的 EasyTier 客户端。
2. 导入平台提供的 EasyTier 网络配置或加入信息，不要转发该配置。
3. 确认 EasyTier 客户端显示已连接。
4. 在浏览器打开 <http://20.10.10.3:18080/>。

连通性检查：

```bash
curl -I http://20.10.10.3:18080/
```

本版本使用 EasyTier 私有网络内的 HTTP 入口。TLS/HTTPS 是后续可选增强，不影响本版本使用。

## 3. 登录与首次设置

### 3.1 设置密码

1. 打开平台发放的一次性设置或重置链接。
2. 设置不少于 14 个字符的高强度密码。
3. 链接使用一次后失效；不要转发或截图保存完整链接。
4. 密码重置会撤销旧 Session 和全部 CLI Token，需要重新登录并创建新 Token。

### 3.2 日常登录

在正式入口输入用户名和密码。普通用户只能查看和操作自己的环境、存储、Job、日志和密钥。

## 4. 创建计算环境

1. 打开“我的环境”，选择“申请计算资源”。
2. 选择 Profile：

   - `STANDARD_8CPU_32GB`：默认 CPU Development。
   - `GPU_1_8CPU_32GB`：显式选择 GPU Development；单 H100 Job 可直接提交，多 GPU Job 使用独立审批。

3. 填写用途说明并提交。
4. 如果平台要求审批，等待管理员正常批准一次。系统会自动完成 Attempt、Reservation、Plan、Dry-run、Stage 和安全回滚检查。
5. 状态变为密钥登记待处理后，继续登记 SSH 公钥并激活。

用户不需要填写 Attempt ID、Container ID、Slurm ID 或 Git SHA，也不需要管理员手工创建容器。

## 5. SSH 公钥和激活

推荐为平台生成专用 ED25519 密钥：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/h100_portal
```

1. 打开“SSH 密钥”。
2. 粘贴 `~/.ssh/h100_portal.pub` 的内容或上传 `.pub` 文件。
3. Scope 选择 `CONTAINER`。
4. 核对 Fingerprint，完成校验和激活。

只上传公钥。私钥必须留在自己的设备上，平台不会要求上传私钥。Host `authorized_keys` 不用于普通用户容器连接。

## 6. 开发容器

“开发容器”页面支持启动、停止、重启、网页终端和 SSH 连接信息。

当前正式模式中，无论 CPU Development 还是 GPU Development，常驻开发容器都不直接挂载 GPU。GPU Development 页面显示以下含义时属于正常状态：

```text
H100 · NOT ALLOCATED
无 GPU Device；H100 作业按需调度
```

这不是 Provision 故障。需要 GPU 时，从“作业”页面提交 GPU=`1` 的 Job；2 至 4 张 GPU 需附完整资料并等待审批。Slurm 只在 Job 运行期间分配获批数量的 H100，Job 结束、取消或超时后自动回收。

停止开发容器不会删除 `/workspace` 或 `/home/<用户名>` 中的数据，也不会提前结束 Lease。

## 7. 统一私有存储与零拷贝

两个持久化路径都可以被开发容器、Slurm CPU Job 和 Slurm GPU Job 直接读写：

```text
/workspace
/home/<你的用户名>
```

例如，开发容器中的文件：

```text
/home/<你的用户名>/models/model-a
/home/<你的用户名>/datasets/train
/workspace/projects/demo
```

在你的 Slurm Job 中仍使用相同路径，不需要 `cp`、`rsync`、重新上传或迁移数据。Job 写回这两个路径的结果，开发容器会立即看到。

建议目录：

```text
/home/<你的用户名>/models/       模型和缓存
/home/<你的用户名>/datasets/     数据集
/home/<你的用户名>/projects/     个人项目
/workspace/projects/             共享工作目录
/workspace/outputs/              作业输出
```

“存储”页面把两个路径作为一个私有逻辑范围显示。300 GiB 用量以 XFS project quota 为准，不会因为 bind alias 重复计数。其他用户的 Home 和 Workspace 不会被挂载，也不能被搜索或列出。

只有写入 `/workspace` 或 `/home/<你的用户名>` 的文件才属于持久化私有存储。容器镜像层、`/tmp` 等临时位置可能不持久化。

## 8. 上传和下载数据

### 8.1 SCP

先从 Portal“连接”页面复制当前 Host、Port 和 Username：

```bash
scp -i ~/.ssh/h100_portal -P <PORT_FROM_PORTAL> ./dataset.tar \
  <USERNAME>@20.10.10.3:/home/<USERNAME>/datasets/
```

上传项目目录：

```bash
scp -r -i ~/.ssh/h100_portal -P <PORT_FROM_PORTAL> ./project \
  <USERNAME>@20.10.10.3:/workspace/projects/
```

下载结果：

```bash
scp -i ~/.ssh/h100_portal -P <PORT_FROM_PORTAL> \
  <USERNAME>@20.10.10.3:/workspace/outputs/result.txt ./
```

### 8.2 SFTP 和 VS Code

SFTP、Remote SSH 和 VS Code 使用 Portal 显示的同一 EasyTier Host、动态端口、用户名和私钥。不要猜测端口；容器重建或恢复后应重新查看连接页面。

## 9. Terminal

### 9.1 网页终端

容器为 `RUNNING` 且 Lease 有效时，可以打开网页终端。平台会校验当前 Session、资源所有权、密钥绑定和容器状态。

网页终端适合编辑代码、下载数据、安装用户级依赖和检查文件。GPU 计算应通过“作业”页面提交；常驻终端中 `nvidia-smi` 看不到 GPU 是预期行为。

### 9.2 SSH

从“连接”页面复制命令，形式如下：

```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/h100_portal \
  -p <PORT_FROM_PORTAL> <USERNAME>@20.10.10.3
```

## 10. 创建 Job

打开“作业”→“新建作业”，填写：

- 名称；
- 执行脚本；
- CPU 数量；
- 内存；
- GPU：`0`、`1`、`2`、`3` 或 `4`；其中 `2` 至 `4` 需要审批；
- 最长运行时间；
- 平台允许的运行镜像。

GPU=`0` 或 `1` 时，平台以当前普通用户身份生成不可变脚本快照并直接提交到 Slurm。GPU=`2` 至 `4` 时，还必须填写模型名称、模型架构、框架及版本、参数量、训练或推理任务、数据集、并行策略和多卡扩展收益；申请先进入“等待审批”，不会调用 Worker、创建 Slurm Job、占用 GPU 或产生运行日志。平台所有者或平台管理员可按申请数量批准、降低 GPU 数量后批准，或附意见驳回。用户只能查看自己的 Job 和日志，且可在审批前取消自己的申请。

首次导入较大的 CUDA/Enroot 镜像会占用额外内存；如低内存 Job 在镜像冷导入期间出现 `OUT_OF_MEMORY`，建议使用至少 8 GiB 后重试。不要通过宿主命令绕过 Portal。

## 11. CLI Job Submission

开发容器 SSH 和网页终端都提供平台管理、普通用户不可修改的 `h100` 命令。新建或重建容器使用只读 bind；既有运行中容器通过 root:root、0555 原子热更新获得 CLI，无需重启；停止的容器下次运行时由 reconciler 补齐。CLI 仍然使用与 Web Jobs 页面相同的 Portal `/self/jobs` API、普通用户身份、Lease、quota、Worker 和 Slurm 路径；它不直接连接宿主 Slurm。

### 11.1 创建和保存 CLI Token

1. 在 Portal 打开“账号安全”→“CLI Tokens”。
2. 输入 Token 名称和当前网页密码，选择有效期，然后创建。
3. 立即复制只显示一次的 Token。
4. 在开发容器中运行：

   ```bash
   h100 auth login
   ```

5. 在无交互自动化中，可通过安全的标准输入运行 `h100 auth login --token-stdin`。不要使用不存在的 `--token TOKEN` 参数，也不要把 Token 写在命令行、脚本、Notebook、Git 或 `/workspace` 中。

CLI 把凭据保存在 `~/.config/h100/credentials`。目录权限是 `0700`，文件权限是 `0600`。检查或删除本地凭据：

```bash
h100 auth status
h100 auth logout
```

`logout` 只删除当前环境中的本地副本。要使 Token 在服务器端失效，请在 Portal“账号安全”中撤销它。Token 只绑定创建者，只能访问自己的 Job；管理员账号不能创建普通用户 CLI Token。Token 到期、被撤销、账号非 ACTIVE、密码变更/重置或账号因登录失败锁定后，认证会被拒绝。Token 明文不会发送给 Worker、Slurm 或 Job 环境。

### 11.2 提交 Job

在项目目录直接提交，不需要复制或上传脚本：

```bash
cd /workspace/projects/demo

h100 job submit train.sh \
  --gpus 1 \
  --cpus 8 \
  --memory 32G \
  --time 01:00:00
```

也可以提交自己 Home 下的脚本：

```bash
cd /home/<你的用户名>/projects/demo
h100 job submit train.sh
```

相对路径和绝对路径都可用，但解析后的脚本必须位于 `/workspace/...` 或 `/home/<你的用户名>/...`。`/etc`、`/root`、`/proc`、`/sys`、其他用户 Home 和任意宿主路径会被拒绝。CLI 读取脚本内容后，Portal 仍创建不可变脚本快照；之后修改原文件不会改变已提交 Job 的审计内容。

省略资源选项时，CLI 从 Portal 获取与 Web 表单相同的正式默认值，不在客户端硬编码默认参数。`--gpus 0` 或 `--gpus 1` 直接提交。`--gpus 2` 至 `--gpus 4` 必须同时提供完整审批资料，例如：

```bash
h100 job submit train.sh --gpus 4 \
  --model-name 'Llama 3.1' \
  --model-architecture 'decoder-only transformer' \
  --framework PyTorch \
  --framework-version 2.6.0 \
  --parameter-count 70B \
  --workload-description '全参数微调' \
  --dataset-description '已清洗的 2 TB 训练语料' \
  --parallel-strategy 'FSDP full shard' \
  --scaling-justification '模型与优化器状态无法装入单卡，预期四卡扩展'
```

资料缺失时 CLI 在本地拒绝；Backend 仍会独立校验。完整申请返回 `APPROVAL_PENDING`，此时没有 Slurm Job 或日志。管理员可批准 1 至申请数量之间的任意数量，不能增加用户申请数量。

Job 内存不超过 `32G` 时无需额外审批。申请更高内存时必须说明任务、内存用量拆分和必要性：

```bash
h100 job submit preprocess.sh --memory 128G \
  --memory-workload-description '大规模数据预处理' \
  --memory-breakdown '96 GiB 数据索引，32 GiB 运行时与缓存' \
  --memory-justification '上游流程当前不能流式读取索引'
```

高内存申请也返回 `APPROVAL_PENDING`。管理员可降低批准内存，但不能提高到用户申请以上。
内存审批与多 GPU 审批相互独立；一个 Job 同时触发两项时，必须全部通过后才创建唯一的
Slurm Job。任一项仍待审时不会调用 Worker，任一项驳回则不创建 Slurm Job。

CLI v1 拒绝包含资源变更 `#SBATCH` directive 的脚本。请用 `--cpus`、`--memory`、`--gpus` 和 `--time`，由 Portal backend 再次执行 authoritative policy validation。

### 11.3 列表、状态、日志和取消

```bash
h100 job list
h100 job status <PORTAL_JOB_ID>
h100 job logs <PORTAL_JOB_ID>
h100 job logs <PORTAL_JOB_ID> --stderr --tail 100
h100 job logs <PORTAL_JOB_ID> --follow
h100 job cancel <PORTAL_JOB_ID> --wait
```

`logs --follow` 只轮询 owner-only Portal logs endpoint，并在 Job 进入终态时退出。按 Ctrl-C 只结束本地日志客户端，不会取消 Job。状态来自 Portal 对 authoritative Slurm state 的投影；其他用户 Job、日志和取消请求统一返回资源不存在，不泄露其存在性。

兼容快捷命令为：

```bash
h100 sbatch train.sh
h100 squeue
h100 scancel <JOB_ID>
```

平台不替换系统的裸 `sbatch`、`squeue` 或 `scancel`。不要配置 MUNGE、连接 Host SSH、访问 Worker socket 或直接调用宿主 Slurm。

### 11.4 JSON 和退出码

关键命令都支持 `--json`。普通命令输出一个 `h100.cli.v1` JSON object；`job logs --follow --json` 输出 JSON Lines。JSON 模式下 stdout 只包含 JSON，诊断信息写到 stderr。

稳定退出码：`0` 成功，`1` 通用客户端/服务器失败，`2` 用法或参数错误，`3` 需要认证，`4` 无权限或资源不存在，`5` policy/quota 拒绝。

## 12. CPU Job 示例

以下脚本同时使用 Home 和 Workspace，路径在开发容器与 Job 中保持一致：

```bash
#!/usr/bin/env bash
set -euo pipefail

user_name="$(id -un)"
home_input="/home/${user_name}/datasets/example/input.txt"
workspace_output="/workspace/outputs/cpu-result.txt"

mkdir -p /workspace/outputs
wc -l "$home_input" > "$workspace_output"
cat "$workspace_output"
```

资源参数由 Portal 表单生成，不需要在脚本中自行调用 `salloc` 或 `srun`。

## 13. GPU Job 示例

在 Job 表单中选择 GPU=`1`：

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-UNSET}"
nvidia-smi -L
nvidia-smi --query-gpu=uuid,name,pci.bus_id --format=csv,noheader

user_name="$(id -un)"
model="/home/${user_name}/models/model-a"
output="/workspace/outputs/gpu-job-finished.txt"

test -e "$model"
mkdir -p /workspace/outputs
date -Is > "$output"
```

正常情况下：

- `CUDA_VISIBLE_DEVICES=0`；
- `nvidia-smi -L` 只显示调度器分配的一张 H100；
- 其他 GPU 不可访问；
- Job 结束后 GPU 自动释放。

镜像中的 `NVIDIA_VISIBLE_DEVICES` 可能保留为基础镜像默认值。实际安全边界以 Slurm allocation、容器 device namespace 和 `nvidia-smi -L` 的可访问设备为准。

## 14. Job 状态、日志和结果

“我的作业”显示 Awaiting Approval、Rejected、Pending、Running、Completed、Failed、Cancelled、Timeout 或 Out of Memory 等状态。

- `stdout` 和 `stderr` 只对作业所有者可见。
- 结果必须写入 `/workspace` 或 `/home/<你的用户名>` 才会持久化。
- Job 写回后，正在运行的开发容器无需同步即可立即读取。
- 已提交 Slurm 的 GPU Job 因单用户 4 卡并发总上限而 Pending 时，等待当前 GPU Job 结束或在 Portal 中取消它。

## 15. Lease、回收和恢复

- Lease 时长：96 小时。
- 开发容器 Stop/Start：不删除数据，不等同于 Lease 回收。
- Lease 到期：系统停止环境并进入可恢复回收状态。
- 数据保留：`/workspace` 与 `/home/<用户名>` 中的持久化数据继续保留。
- Restore：通过 Portal 正式恢复流程重新激活；不需要复制数据，也不要手工启动旧容器。

恢复后先确认两个持久化路径中的文件和权限，再继续提交 Job。

## 16. 常见问题

### 页面显示 H100 / NOT ALLOCATED，是否故障？

不是。常驻开发容器与 GPU allocation 已解耦。1 张 GPU 可直接提交，2 至 4 张需审批；GPU 只在 Job 运行期间可见。

### 下载了几十 GiB，但存储用量很小？

确认文件位于 `/workspace` 或 `/home/<你的用户名>`。当前版本会把两个路径计入同一 XFS project quota。写在镜像层或临时目录中的内容不属于持久化配额，刷新“存储”页面后再检查。

### GPU Job 为什么 Pending？

单用户所有直接提交和获批 Job 合计最多同时占用 4 张 GPU。资源不足、总量达到 4 张或其他 Slurm 调度条件未满足时，已提交的 Job 会保持 Pending，并显示权威 Slurm 原因。

### 为什么 GPU=2 至 GPU=4 没有立即运行？

多 GPU 请求必须经过审批。请完整填写模型、框架和版本、参数量、任务、数据集、并行策略与扩展收益。等待审批期间不会调用 Worker、创建 Slurm Job 或占用 GPU；管理员可以降低 GPU 数量后批准，也可以说明理由后驳回。

### SSH Connection refused

检查 EasyTier 是否在线、开发容器是否 `RUNNING`，并从 Portal 重新复制当前端口。不要把远端 EasyTier IP 绑定到本机网卡。

### SSH Permission denied (publickey)

检查用户名、私钥和 Portal 显示的 Fingerprint；使用 `IdentitiesOnly=yes`，确认公钥 Scope 为 `CONTAINER` 且状态为已安装。

### Job 看不到 Home 文件

使用当前账号自己的 `/home/<你的用户名>`，不要提交任意宿主路径。若路径或所有者不匹配，Job 会安全拒绝挂载。

### CPU Job 在开始阶段 Out of Memory

首次导入大型运行镜像可能需要更多内存。提高到 8 GiB 后重新通过 Portal 提交，并保留失败 Job 作为历史记录。

### Portal 无法打开

检查 EasyTier 连接和本机出站防火墙，确认访问的是 <http://20.10.10.3:18080/>。不要改用物理管理地址。
