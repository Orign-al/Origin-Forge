# H100 受控单机 Pilot 用户指南

状态：**尚未开放给 Pilot 用户**。当前提交入口的作业外 GPU 隔离 Gate 未通过，且 `user.slice` 试验尚未通过 Slurm 作业内设备 open 验收，Slurm 节点保持 DRAIN。本指南用于整改通过后的受控上线，不构成账号或资源授权。

## 平台边界与已知风险

- 平台是单台、4 × NVIDIA H100 PCIe 80GB 的 Pilot，不是全面生产环境。
- MIG 保持 Disabled；GPU 任务只能通过 Slurm + Pyxis/Enroot，禁止在宿主或长期 Docker 中直接使用 GPU。
- Mellanox CRC/symbol error 仍持续增长，管理员已接受其用于单节点 Pilot 的延期风险。可能出现丢包、重传、超时或 SSH 断连。
- 当前无 NFS、SMB、第二节点、跨节点 NCCL、RDMA/RoCE 生产能力或高速共享存储。
- 用户目录是本机 XFS；300GB 是 project quota **硬上限**，不是预留空间。
- 当前没有正式异机备份。重要训练结果必须及时复制到管理员批准的归档位置。
- Docker Hub 是受限镜像源，不得依赖其实时拉取。

## 登录入口

管理员会分别提供宿主提交入口和个人开发容器端口。只使用自己的 SSH 私钥；不得共享账号、私钥或 SSH agent。

宿主提交入口（仅在管理员确认 Gate 通过后启用）：

```bash
ssh USERNAME@10.82.36.1
```

长期个人 Docker：

```bash
ssh -p ASSIGNED_PORT USERNAME@10.82.36.1
```

首次连接前，从管理员提供的独立渠道核对 SSH host-key 指纹。禁止使用 `StrictHostKeyChecking=no`。

VS Code Remote SSH 示例：

```sshconfig
Host h100-pilot-container
    HostName 10.82.36.1
    User USERNAME
    Port ASSIGNED_PORT
    IdentityFile /path/to/your/private_key
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

## 工作目录、配额与持久化

- 宿主工作区：`/srv/gpu-platform/users/USERNAME/workspace`
- 长期容器工作区：`/workspace`
- 个人 home、workspace、shared 以及容器 SSH host key 独立持久化。
- 容器重建不会删除这些绑定目录；未写入绑定目录的容器层数据可能丢失。
- 不要读取、修改或共享其他用户目录。`shared` 也只有在管理员明确配置协作组后才用于共享。
- 接近配额时先归档或申请调整；管理员不会自动删除用户数据、镜像或日志。

## 长期 Docker 的用途与限制

长期容器用于编辑、依赖准备和轻量 CPU 开发，默认 8 CPU、32GiB RAM、4096 PIDs，实际值以分配清单为准。

- 容器默认无 GPU；其中 `nvidia-smi` 不可用是预期行为。
- 容器内可 `sudo` 成为**容器内 root**，不代表宿主权限。
- 不提供 Docker Socket、MUNGE socket/key、host network、host PID/IPC 或 privileged。
- 禁止在容器中寻找或构造绕过 Slurm 的 GPU 通道。

## 镜像政策

允许的来源按优先级为：本地已缓存镜像、NVIDIA NGC、经批准的 GHCR、经批准的 Quay、管理员离线导入的 OCI/Docker archive。后续可使用公司内部 Harbor。

镜像必须记录 registry、repository、tag、digest、导入时间和上传用户；运行时优先使用固定 digest，不能只依赖 tag。禁止 Docker Hub 实时拉取、未知 mirror、insecure registry、关闭 TLS、个人 VPN/代理或 SSH 隧道。

已验收的 CUDA 测试镜像：

```text
nvcr.io#nvidia/cuda:13.2.0-base-ubuntu24.04@sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a
```

## Slurm 作业

查看节点和队列：

```bash
sinfo -Nel
squeue -u "$USER"
```

CPU 作业：

```bash
srun --partition=notebook --time=00:05:00 \
  --cpus-per-task=2 --mem=2G hostname
```

单 GPU Pyxis 作业：

```bash
srun --partition=notebook --gres=gpu:h100:1 \
  --time=00:10:00 --cpus-per-task=4 --mem=8G \
  --container-image='APPROVED_PINNED_IMAGE' \
  nvidia-smi -L
```

- `notebook` 用于有明确时限的交互/短作业；`train` 用于较长训练。
- 默认 QOS 为 `general`，每用户最多 1 张 GPU；两卡必须单独审批。
- 每个作业必须设置合理时间限制，不允许无限期交互式 GPU Job。
- 作业应在容器输出中只看到 Slurm 分配的 GPU。

取消作业和查看历史：

```bash
scancel JOB_ID
sacct -S today -u "$USER" \
  --format=JobID,JobName,Partition,State,ExitCode,Elapsed,AllocTRES
```

批处理日志由 `sbatch --output=PATH --error=PATH` 指定；报告问题时提供 Job ID、时间、分区、镜像 digest、ExitCode 和日志路径，不要发送私钥或 token。

## Pilot 使用规则

- 只运行单节点任务，不使用 NFS、SMB、RDMA/RoCE 或跨节点通信。
- 避免大规模外网下载；公共模型和数据集由管理员统一管理，避免每用户重复复制。
- 不得使用宿主 Docker、加入 `docker` 组、访问 Docker Socket或控制其他用户容器。
- 不得在 Slurm 作业外访问 GPU，也不得申请未获批准的 QOS/GPU 数量。
- 在最终隔离方案通过后，宿主 SSH 中 `nvidia-smi` 失败将是预期行为；当前方案尚未启用，用户不得把现状当作安全边界。
- 网络中断、GPU 异常、作业异常、文件传输错误或数据损坏迹象应立即报告。
- 重要结果应在作业完成后及时归档。

故障报告至少包含：发生时间、用户名、Job ID/容器名、操作命令（去除 secret）、错误文本、是否仍可 SSH、是否影响数据。管理员会按健康与安全条件决定是否紧急 DRAIN。
