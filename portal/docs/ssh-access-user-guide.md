# SSH Key 与连接用户指南

Portal 网页密码只用于登录管理页面。Linux 计算身份没有 SSH 密码；服务器通过保存的
SSH public key 验证客户端持有的匹配 private key。

## 设置 SSH Key

STAGED 计算用户首次登录时，Portal 会显示“完成 SSH 密钥设置后即可启用计算环境”。也可
从“用户 → 当前计算身份 → SSH 公钥”或“连接”页进入同一设置流程。

### 生成新密钥

1. 选择“生成新密钥”，确认注释和 Scope；`BOTH` 同时用于宿主与开发容器。
2. 浏览器本地生成 ED25519 key pair。先下载私钥，也可下载或复制 `.pub` 公钥。
3. 安全保存私钥并勾选“我已经保存私钥”，再继续登记 public key。
4. Portal 只显示类型、`SHA256:` fingerprint、注释、Scope 和 `VALIDATED — NOT INSTALLED`。

当前生成文件是标准、未加密的 OpenSSH 私钥。应在自己的设备上限制文件权限，并可用
`ssh-keygen -p -f <私钥文件>` 添加本地保护密码。私钥丢失时平台无法恢复，需要登记新
Key。不要把私钥或保护密码上传给 Portal 或管理员。

### 导入已有公钥

选择“导入已有公钥”，粘贴一行 `.pub` 内容或上传 `.pub` 文件。不要上传没有 `.pub`
后缀的 private-key 文件；Portal 仍会检查内容，不能靠改名绕过。校验后核对类型、SHA-256
fingerprint、注释和 Scope，再确认登记。

## STAGED 与 Activate

登记 Key 不会自动开放 SSH。`VALIDATED — NOT INSTALLED` 时仍保持：

- shell `/usr/sbin/nologin`；
- Linux password `LOCKED`；
- 宿主与容器 `authorized_keys` 均 `ABSENT`；
- 开发容器 `STOPPED`、GPU `NONE`；
- Slurm 节点 `DRAIN`。

管理员完成 Activate dry-run、核对 fingerprint/Scope 并另行批准后，平台才会把同一 public
key 分别安装到批准的 HOST/CONTAINER 目标。平台不会把宿主 `.ssh` 目录挂载到容器。

## 连接

ACTIVE 后，“连接”页显示真实 Host、Port、Username 和 fingerprint。命令模板中的
`<你的私钥路径>` 是用户自己设备上的路径，Portal 不知道也不保存它。

```text
ssh -i <你的私钥路径> origin-pilot@<APPROVED_HOST>
ssh -i <你的私钥路径> -p 22023 origin-pilot@<APPROVED_HOST>
```

VS Code Remote SSH 配置同样使用本地 `IdentityFile <你的私钥路径>`。宿主环境的 GPU 只
能在 Slurm Job 内访问；长期开发容器是 GPU NONE。连接页的 SSH User Public Key
fingerprint 用于用户认证，SSH Server/Container Host Key fingerprint 用于验证服务端
身份，两者不可混淆。
