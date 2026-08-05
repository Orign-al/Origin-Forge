# H100 网页管理平台

Portal-0/1 是 H100 单机平台的本地管理控制面。Web 和 API 仅监听 localhost；管理员
必须先建立 SSH Tunnel：

```bash
ssh -L 18080:127.0.0.1:18080 h100-codex
```

浏览器访问 `http://127.0.0.1:18080`。当前模式为 **LOCAL SSH TUNNEL MODE**，
不是公开 HTTPS 入口。

## 边界

- API 非 root，不访问 Docker Socket、MUNGE key、shadow 或 SlurmDBD 密码。
- 高权限宿主操作只能经由限权 Unix Socket Root Worker。
- Portal-0/1 写操作只提供审批与 dry-run，不创建 Linux 用户、不创建用户容器、
  不修改 GPU 隔离、不启用 Guard timer，也不 RESUME Slurm。
- Origin-al 网页密码与 Linux/SSH 密码完全分离。

完整部署与安全说明见 [`docs/`](docs/)。
