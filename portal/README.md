# H100 网页管理平台

Portal-2 是 H100 单机平台的私有管理控制面。Web 精确监听批准的 `tun0` 地址
`10.10.10.2:18080`，API 仍仅监听 `127.0.0.1:18081`。私有隧道客户端可直接访问：

```text
http://10.10.10.2:18080
```

SSH Tunnel 仍可作为回退：

```bash
ssh -L 18080:10.10.10.2:18080 h100-codex
```

通过 SSH Tunnel 时浏览器访问 `http://127.0.0.1:18080`。当前模式为
**PRIVATE TUNNEL NETWORK MODE**，不是公开 HTTPS 入口。

## 边界

- API 非 root，不访问 Docker Socket、MUNGE key、shadow 或 SlurmDBD 密码。
- 高权限宿主操作只能经由限权 Unix Socket Root Worker。
- Portal-2 写操作只提供 DRAFT、审批与 dry-run，不创建 Linux 用户、不创建用户容器、
  不修改 GPU 隔离、不启用 Guard timer，也不 RESUME Slurm。
- Origin-al 网页密码与 Linux/SSH 密码完全分离。

完整部署与安全说明见 [`docs/`](docs/)。
