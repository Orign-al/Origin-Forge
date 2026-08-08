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
- Portal-3C 已通过审批主体和参数均精确绑定的 Root Worker 链路 Stage `origin-pilot`；
  `user.activate` 真实执行继续禁用，也不 RESUME Slurm。
- Stage 创建锁定且使用 `/usr/sbin/nologin` 的独立计算身份，不读取或安装 SSH 公钥；
  公钥验证、普通 shell、容器启动和登录能力全部保留到后续 Activate 审批。
- Portal-3D-R 提供浏览器本地生成 ED25519 或导入已有 `.pub`。服务器只保存 public key 和
  metadata，绝不接收、生成或保存 private key。有效 Key 仍是 `VALIDATED — NOT INSTALLED`，
  只能生成 Activate dry-run。
- Origin-al 网页密码与 Linux/SSH 密码完全分离。
- 内部 HTTP 接受不阻断当前单节点 Pilot 的已批准 Stage，但不得据此扩大访问范围或绕过
  Portal Operation、审批、幂等键、脚本 hash 和 Root Worker 校验链路。

用户操作见 [`docs/ssh-access-user-guide.md`](docs/ssh-access-user-guide.md)，完整部署与安全
说明见 [`docs/`](docs/)。
