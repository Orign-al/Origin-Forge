# H100 网页管理平台

Portal-2 是 H100 单机平台的私有管理控制面。Web 精确监听批准的 `tun0` 地址
`10.10.10.2:18080`，API 仍仅监听 `127.0.0.1:18081`。受控虚拟网络客户端直接访问：

```text
http://10.10.10.2:18080
```

SSH Tunnel 仍可作为可选回退，但不是当前访问要求：

```bash
ssh -L 18080:10.10.10.2:18080 h100-codex
```

通过 SSH Tunnel 时浏览器访问 `http://127.0.0.1:18080`。管理员已接受当前 Pilot 的
**INTERNAL HTTP · VIRTUAL NETWORK ONLY** 模式。Transport TLS 未启用，且当前 Pilot
范围不要求启用；不得把该入口描述为 HTTPS 或公网服务。访问范围扩大时必须重新评估并
优先启用 HTTPS。

## 边界

- API 非 root，不访问 Docker Socket、MUNGE key、shadow 或 SlurmDBD 密码。
- 高权限宿主操作只能经由限权 Unix Socket Root Worker。
- Portal-2 写操作只提供 DRAFT、审批与 dry-run，不创建 Linux 用户、不创建用户容器、
  不修改 GPU 隔离、不启用 Guard timer，也不 RESUME Slurm。
- Origin-al 网页密码与 Linux/SSH 密码完全分离。
- 内部 HTTP 接受不再阻断未来 Portal-3 真实写操作，但当前仍须等待完整 Portal-3 执行批准；
  本次变更不启用任何写 handler。

完整部署与安全说明见 [`docs/`](docs/)。
