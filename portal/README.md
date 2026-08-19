# Origin Forge Portal

Origin Forge 是 H100 单机多用户 GPU 平台的私有管理控制面。Web 用户入口监听所有 IPv4 接口，但 systemd
网络策略仅允许批准的 VPN、EasyTier 和管理 LAN 网段；API 仍仅监听 `127.0.0.1:18081`。
用户通过 EasyTier 主地址访问：

```text
http://20.10.10.3:18080
```

旧 EasyTier 网络入口 `http://10.10.10.220:18080` 同时保留。品牌名称只影响用户界面与
文档；`h100-*` unit、命令和包名作为稳定部署接口继续保留。

SSH Tunnel 仍可作为可选回退，但不是当前访问要求：

```bash
ssh -L 18080:20.10.10.3:18080 h100-codex
```

通过 SSH Tunnel 时浏览器访问 `http://127.0.0.1:18080`。管理员已接受当前 Pilot 的
**INTERNAL HTTP · VIRTUAL NETWORK ONLY** 模式。Transport TLS 未启用，且当前 Pilot
范围不要求启用；不得把该入口描述为 HTTPS 或公网服务。访问范围扩大时必须重新评估并
优先启用 HTTPS。

## 边界

- API 非 root，不访问 Docker Socket、MUNGE key、shadow 或 SlurmDBD 密码。
- 高权限宿主操作只能经由限权 Unix Socket Root Worker。
- 多用户 Provision 通过审批主体和参数均精确绑定的 Root Worker 链路自动完成 Stage；旧
  `user.activate` 只保留给 `origin-pilot` 历史兼容路径，新产品 UI 不再调用。
- Stage 创建锁定且使用 `/usr/sbin/nologin` 的独立计算身份，不读取或安装 SSH 公钥；
  公钥验证、普通 shell、容器启动和登录能力全部保留到后续 Activate 审批。
- Portal 提供浏览器本地生成 ED25519 或导入已有 `.pub`。服务器只保存 public key 和
  metadata，绝不接收、生成或保存 private key。普通用户登记有效 CONTAINER Key 后可一次
  点击激活自己的 STAGED 环境；API 以会话身份绑定 owner，内部自动预检、安装容器 Key、
  启动并验证容器，成功后才开始精确 96 小时 Lease。宿主 SSH 始终禁用。
- Origin-al 网页密码与 Linux/SSH 密码完全分离。
- 内部 HTTP 接受不阻断当前单节点 Pilot 的已批准 Stage，但不得据此扩大访问范围或绕过
  Portal Operation、审批、幂等键、脚本 hash 和 Root Worker 校验链路。

仓库总览见 [`../README.md`](../README.md)，配置见
[`../docs/configuration.md`](../docs/configuration.md)，用户操作见
[`docs/ssh-access-user-guide.md`](docs/ssh-access-user-guide.md)，完整部署与安全说明见
[`docs/`](docs/)。
