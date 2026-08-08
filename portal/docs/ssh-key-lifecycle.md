# 两阶段计算用户 SSH 公钥生命周期

网页密码只用于 Portal 会话，不是 Linux 密码。受管计算用户不设置 Linux 或容器 SSH
密码，只能在独立 Activate 事务中安装经批准的 SSH 公钥。用户通过 Portal 自助生成新
Key 或导入已有 `.pub`；管理员不需要预先在服务器准备公钥文件。

## 浏览器与服务器边界

“生成新密钥”在浏览器内使用固定版本的 `@noble/ed25519` 和 Web Crypto 安全随机源生成
ED25519 key pair，并编码为标准 OpenSSH `openssh-key-v1` 私钥和 `ssh-ed25519` 公钥。
私钥只存在于当前页面内存和用户触发的下载中；不会通过 API、Worker、日志、审计、
PostgreSQL、`/var/lib/h100-portal`、localStorage、sessionStorage、IndexedDB、Service
Worker cache 或 telemetry 保存。当前版本生成未加密的 OpenSSH 私钥；Portal 不自行实现
私钥加密格式。用户下载后可在自己的设备上用 `ssh-keygen -p` 添加本地保护密码。

“导入已有公钥”允许粘贴或上传 `.pub`。扩展名只是前端提示，API 和 Worker 仍独立解析
内容。任何含 `PRIVATE KEY` 装甲、空白、多行、未知类型、无效 Base64、错误 blob 结构、
控制字符注释或超限内容的请求都会 fail-closed。私钥拒绝审计只写
`SSH_PRIVATE_KEY_UPLOAD_REJECTED`，不写请求正文。

服务器只保存规范化 public key、类型、SHA-256 fingerprint、注释、Scope、状态、创建者
和时间。常规列表与审计不返回完整公钥。UI 必须说明：“服务器保存你的公钥。连接 SSH
时，请使用与该公钥匹配的私钥。”SSH User Public Key fingerprint 与 SSH Server Host Key
fingerprint 是不同对象，连接页分别标示。

## Portal API

`POST /api/v1/users/{portal_user_id}/ssh-keys` 只接受：

```json
{
  "key_type": "ssh-ed25519",
  "public_key": "<one OpenSSH public-key line>",
  "comment": "Origin laptop",
  "scope": "BOTH",
  "generation_method": "BROWSER_GENERATED",
  "client_fingerprint_sha256": "SHA256:<fingerprint>",
  "confirmed_private_key_saved": true
}
```

导入方式使用 `generation_method=IMPORTED` 和 `confirmed_public_key=true`。Schema 使用
`extra=forbid`。`private_key`、`raw_private_key`、`private_key_password`、
`private_key_path`、Linux password 以及这些字段的嵌套形式均返回 4xx；API 不接受 URL 或
任意宿主路径。`GET /api/v1/users/{portal_user_id}/ssh-keys` 只返回 public metadata，不
返回 `public_key` 正文。

`user.activate` DRAFT payload 只包含 `managed_user_id`、
`approved_ssh_key_record_ids`、`expected_state=STAGED` 和 approval reference。Operation
envelope 另带服务端生成/验证的幂等键；payload 不接受任意 argv、路径、密码或私钥。

## DRAFT

DRAFT 只存在于 Portal 数据库。`user.plan` 与 `user.stage` dry-run 可以规划和重新验证
用户名、UID/GID、project ID、quota、Slurm、容器与 GPU policy，不预留宿主资源。
SSH key 状态是 `NOT_REQUIRED_FOR_STAGE`。

## STAGED

`user.stage` schema 和 `h100-user-create --stage` 均不接受公钥字段。传入
`public_key_file`、`public_key_path`、公钥正文或 key record ID 会以
`PUBLIC_KEY_NOT_ALLOWED_DURING_STAGE` 拒绝，而不是静默忽略。

Stage 成功时账号保持 `/usr/sbin/nologin`、密码锁定，宿主和受管 home 均不存在
`authorized_keys`；GPU policy、deny self-test、quota、Slurm association 和无 GPU 容器
通过后容器停止，Guard 才能启用。状态写为 `STAGED`，SSH key 状态写为
`REQUIRED_BEFORE_ACTIVATION`。

STAGED 用户首次登录且有效 Key 数量为零时，Portal 跳转到统一的 SSH 密钥设置流程。用户
也可从“用户 → SSH 公钥”或任一连接入口进入。可以暂时离开页面，但宿主 SSH、容器 SSH、
VS Code、Activate 和 `ACTIVE` 状态继续被阻断。

登记成功只创建 `VALIDATED — NOT INSTALLED` record。一个受管用户最多五条有效记录；
fingerprint 在全平台范围唯一，撤销历史中的 fingerprint 也不能重新登记。`scope` 为
`HOST`、`CONTAINER` 或 `BOTH`。当前 `origin-pilot` 默认使用 `BOTH`，但宿主与容器是两个
独立安装目标，不共享 `.ssh` 目录，也不挂载任何私钥。

## ACTIVE

`user.activate` 只接受 `managed_user_id` 和一至五个已验证 SSH key record UUID，
不接受浏览器提供的宿主路径、密码、私钥、URL、command 或 argv。Root Worker 将 UUID
映射为 `/var/lib/h100-portal/ssh-key-staging/<UUID>.pub`。目录必须是
`root:root 0700`；文件必须是 root 所有的单链接普通文件、不可被 group/other 写入且不
超过 16KiB。读取使用 `O_NOFOLLOW`，并核对打开前后的 inode。

每条记录只允许一把平台策略批准的 `ssh-ed25519`、`ecdsa-sha2-nistp256` 或
`sk-ssh-ed25519@openssh.com` 公钥。空文件、私钥头、PEM、未知类型、无效 Base64、重复
fingerprint、symlink、hardlink、可被其他用户写入或超大文件均 fail-closed。fingerprint
由 API 与 Worker 分别使用安全 parser 和 `ssh-keygen -E sha256` 复核；日志和普通报告只
记录类型、SHA-256 fingerprint 与内容摘要，不记录公钥正文。

公钥验证以及 STAGED、UID/GID、nologin、密码锁定、GPU policy、Guard、quota、Slurm、
容器无 GPU、容器停止和禁止组验证全部通过后，Activate 才使用临时文件、fsync 和原子
rename 安装 `0700 .ssh` / `0600 authorized_keys`，重新读取 fingerprint，再切换 shell、
启动容器和写入 `ACTIVE`。失败时恢复 nologin、禁用公钥、停止容器并保留数据与 GPU
policy；Slurm 始终保持 DRAIN。

Portal-3D-R 只允许生成 `user.activate` dry-run。dry-run 必须重新确认用户仍为 STAGED、
shell 为 nologin、密码锁定、宿主和容器 `authorized_keys` 均不存在、GPU policy/Guard/
quota/Slurm 通过、容器停止且 GPU NONE，并确认选中的 Key 覆盖 HOST 和 CONTAINER。成功
计划明确返回：

```text
HOST AUTHORIZED_KEYS INSTALL: PLANNED
CONTAINER AUTHORIZED_KEYS INSTALL: PLANNED
ACTIVATE EXECUTION: DISABLED — ADMINISTRATOR APPROVAL REQUIRED
```

真正 Activate 仍需独立管理员审批。HOST key 原子安装到宿主 home；CONTAINER key 原子
安装到现有受控持久用户目录，不能只写 Docker writable layer。容器 stop/start/rebuild 后
应保持相同 fingerprint。撤销与轮换按独立 record 操作，不覆盖其他有效 Key；撤销最后一
把 Key 必须先明确警告并重新认证。

Portal-3C 已真实 Stage `origin-pilot`。Stage 完成后仍没有 authorized_keys、普通 shell
或运行中的用户容器。Portal-3D-R 可以登记用户自己的真实公钥并显示类型、fingerprint、
注释、Scope 和时间，但不得因此安装 Key 或激活身份。Activate 继续受后续独立 Gate 阻断。
