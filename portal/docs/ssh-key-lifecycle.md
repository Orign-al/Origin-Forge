# 两阶段计算用户 SSH 公钥生命周期

网页密码只用于 Portal 会话，不是 Linux 密码。受管计算用户不设置 Linux 或容器 SSH
密码，只能在独立 Activate 事务中安装经批准的 SSH 公钥。

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

## ACTIVE

`user.activate` 只接受 `managed_user_id` 和一至五个已批准的 SSH key record UUID，
不接受浏览器提供的宿主路径、密码、私钥、URL、command 或 argv。Root Worker 将 UUID
映射为 `/var/lib/h100-portal/ssh-key-staging/<UUID>.pub`。目录必须是
`root:root 0700`；文件必须是 root 所有的单链接普通文件、不可被 group/other 写入且不
超过 16KiB。读取使用 `O_NOFOLLOW`，并核对打开前后的 inode。

每条记录只允许一把 `ssh-ed25519` 或 `sk-ssh-ed25519@openssh.com` 公钥。空文件、私钥
头、PEM、未知类型、无效 base64、重复 fingerprint、symlink、hardlink、可被其他用户写
入或超大文件均 fail-closed。fingerprint 只由 `ssh-keygen -E sha256` 生成；日志和普通
报告只记录类型、SHA-256 fingerprint 与内容摘要，不记录公钥正文。

公钥验证以及 STAGED、UID/GID、nologin、密码锁定、GPU policy、Guard、quota、Slurm、
容器无 GPU、容器停止和禁止组验证全部通过后，Activate 才使用临时文件、fsync 和原子
rename 安装 `0700 .ssh` / `0600 authorized_keys`，重新读取 fingerprint，再切换 shell、
启动容器和写入 `ACTIVE`。失败时恢复 nologin、禁用公钥、停止容器并保留数据与 GPU
policy；Slurm 始终保持 DRAIN。

Portal-3B-R 只部署并 dry-run 验收此契约。真实 Stage、上传真实用户公钥和 Activate 均
继续受独立审批 Gate 阻断。
