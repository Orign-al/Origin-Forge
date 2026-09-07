"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { isLocale, LOCALE_COOKIE, type Locale } from "./i18n-config";

export { isLocale, LOCALE_COOKIE, SUPPORTED_LOCALES } from "./i18n-config";
export type { Locale } from "./i18n-config";

const EN_US: Record<string, string> = {
  中文: "Chinese",
  英文: "English",
  语言: "Language",
  切换界面语言: "Switch interface language",
  "正在验证会话…": "Verifying your session…",
  "正在跳转登录页…": "Redirecting to sign in…",
  "正在读取平台数据…": "Loading platform data…",
  "数据源不可用，未将其显示为正常或 0。":
    "The data source is unavailable; it is not shown as healthy or zero.",
  "当前账号无权查看此模块。":
    "Your account is not authorized to view this module.",
  "数据来自最近一次成功采样；当前 Worker 或数据源可能暂时不可用。":
    "Data is from the most recent successful sample; the worker or data source may currently be unavailable.",
  平台: "Platform",
  平台所有者: "Platform Owner",
  平台管理员: "Platform Administrator",
  运维操作员: "Operator",
  审计员: "Auditor",
  用户: "User",
  我的环境: "My Environment",
  连接: "Access",
  网页终端: "Web Terminal",
  作业: "Jobs",
  开发容器: "Development Container",
  容器: "Containers",
  存储: "Storage",
  SSH密钥: "SSH Keys",
  "SSH 密钥": "SSH Keys",
  安装新增密钥: "Install new keys",
  "正在安装…": "Installing…",
  "SSH 公钥已验证并安装到运行中的开发容器；私钥未发送到服务器。":
    "The SSH public key was validated and installed in the running development container. The private key was not sent to the server.",
  "SSH 公钥已验证并安装到运行中的开发容器；平台没有接收私钥。":
    "The SSH public key was validated and installed in the running development container. The platform did not receive the private key.",
  "SSH 公钥已验证，但安装到运行中容器失败；请点击重试安装。":
    "The SSH public key was validated, but installation in the running container failed. Select retry installation.",
  "新增 SSH 公钥已安装到运行中的开发容器。":
    "The new SSH public key was installed in the running development container.",
  "SSH 公钥安装失败；原有可用密钥保持不变。":
    "SSH public-key installation failed. The previously working keys remain unchanged.",
  "新增公钥已验证但尚未安装。同步只会原子更新你自己的运行中开发容器，不会启用宿主 SSH。":
    "The new public key is validated but not installed. Synchronization atomically updates only your own running development container and does not enable host SSH.",
  回收站: "Recycle Bin",
  账号与安全: "Account & Security",
  账号安全: "Account Security",
  帮助: "Help",
  镜像: "Images",
  监控: "Monitoring",
  审批: "Approvals",
  审计: "Audit",
  系统: "System",
  资源申请: "Resource Request",
  我的资源: "My Resources",
  "个人计算环境 · Portal-5A-1B": "Personal compute environment · Portal-5A-1B",
  "单机控制面 · Portal-5A-1B": "Single-node control plane · Portal-5A-1B",
  计算资源未配置: "Compute resources not configured",
  "等待 Container SSH 密钥": "Waiting for Container SSH key",
  "Host SSH 已禁用": "Host SSH disabled",
  "Container STOPPED · Lease 未开始": "Container STOPPED · Lease not started",
  "GPU 上限 1 · 租约受控": "GPU limit 1 · Managed lease",
  "单节点 · 单受管用户": "Single node · Managed users",
  "H100 单节点环境": "H100 single-node environment",
  全局模块搜索: "Search all modules",
  搜索模块: "Search modules",
  主导航: "Main navigation",
  "Portal Identity Only": "Portal Identity Only",
  "Production Pilot ACTIVE": "Production Pilot ACTIVE",
  "节点：{node}": "Node: {node}",
  "当前告警 {count} 个": "{count} active alerts",
  "告警 {count}": "Alerts {count}",
  退出: "Sign out",
  "完成 Container SSH 密钥设置后即可申请激活计算环境。":
    "Set up your Container SSH key to request compute environment activation.",
  "Host SSH、容器访问与 Lease 仍保持关闭。":
    "Host SSH, container access, and the lease remain disabled.",
  "设置 SSH 密钥": "Set up SSH key",
  "私有管理入口 · 请使用网页账号登录":
    "Private access portal · Sign in with your web account",
  登录名: "Username",
  请输入登录名: "Enter your username",
  密码: "Password",
  网页密码: "Web password",
  请输入密码: "Enter your password",
  登录: "Sign in",
  "登录中…": "Signing in…",
  "无法建立安全会话，请确认 Portal API 正常运行。":
    "A secure session could not be established. Confirm that the Portal API is running.",
  "请输入登录名和密码。": "Enter your username and password.",
  "用户名或密码不正确，或账号暂时被锁定。":
    "The username or password is incorrect, or the account is temporarily locked.",
  "网页密码与 Linux/SSH 密码分离。当前入口仅绑定已批准的虚拟网络地址；Pilot 阶段由管理员接受内部 HTTP，未启用 TLS。":
    "Your web password is separate from Linux and SSH credentials. This portal is available only on approved virtual-network addresses. During the pilot, administrators have accepted internal HTTP without TLS.",
  "如已收到一次性设置或重置链接，请直接打开管理员交付的完整链接。":
    "If you received a one-time setup or reset link, open the complete link supplied by your administrator.",
  首次登录修改密码: "Change Password on First Sign-in",
  "设置自己的Portal密码后才能进入计算环境。":
    "Set your own Portal password before entering the compute environment.",
  临时密码: "Temporary password",
  新Portal密码: "New Portal password",
  再次输入: "Enter again",
  保存并进入我的环境: "Save and open My Environment",
  "此操作只修改Portal密码，不设置或解锁Linux密码。":
    "This changes only your Portal password. It does not set or unlock a Linux password.",
  "新密码至少14个字符，且两次输入必须一致。":
    "The new password must be at least 14 characters and both entries must match.",
  "密码修改失败。请确认临时密码正确，并使用至少14个字符的新密码。":
    "The password change failed. Confirm the temporary password and use a new password of at least 14 characters.",
  "正在验证一次性链接…": "Validating the one-time link…",
  一次性密码链接: "One-time password link",
  链接已过期: "Link expired",
  链接已使用: "Link already used",
  链接无效: "Invalid link",
  "请联系管理员生成新的密码设置或重置链接。":
    "Ask an administrator to generate a new password setup or reset link.",
  密码重置完成: "Password reset complete",
  使用新密码登录: "Sign in with your new password",
  "该账号的旧 Portal 会话已全部撤销，计算资源没有变化。":
    "All previous Portal sessions for this account were revoked. Compute resources were not changed.",
  返回登录: "Return to sign in",
  密码重置: "Password reset",
  账号邀请: "Account invitation",
  重置你的登录密码: "Reset your sign-in password",
  设置你的登录密码: "Set your sign-in password",
  "此链接只能使用一次。": "This link can only be used once.",
  有效至: "Valid until",
  新密码: "New password",
  "14–128 个字符，可使用中文和 Unicode。":
    "14–128 characters; Unicode characters are supported.",
  确认新密码: "Confirm new password",
  "密码至少 14 个字符": "The password must be at least 14 characters",
  "密码最多 128 个字符": "The password must be no more than 128 characters",
  请再次输入密码: "Enter the password again",
  两次输入的密码不一致: "The passwords do not match",
  密码不符合要求: "The password does not meet the requirements",
  "密码设置失败，请重新获取一次性链接。":
    "Password setup failed. Request a new one-time link.",
  "保存中…": "Saving…",
  设置密码并继续: "Set password and continue",
  "该操作只修改 Portal 身份密码，不修改 Linux、SSH、Container、Lease 或其他计算资源。":
    "This changes only the Portal identity password. It does not change Linux, SSH, containers, leases, or other compute resources.",
  "只管理 Portal 密码与服务端会话":
    "Manage only your Portal password and server-side sessions",
  "只管理 Portal 密码、服务端会话与 CLI Token":
    "Manage your Portal password, server-side sessions, and CLI Tokens",
  当前网页身份: "Current Web Identity",
  角色: "Role",
  账号: "Account",
  计算身份: "Compute Identity",
  "网页密码与 Linux shadow、SSH 密码和 authorized_keys 完全分离。":
    "The web password is fully separate from Linux shadow, SSH passwords, and authorized_keys.",
  修改网页密码: "Change Web Password",
  当前网页密码: "Current web password",
  新网页密码: "New web password",
  保存网页密码: "Save web password",
  "新密码至少 14 个字符，且两次输入必须一致。":
    "The new password must be at least 14 characters and both entries must match.",
  "网页密码已修改，其他会话已撤销，当前会话已旋转。":
    "The web password was changed, other sessions were revoked, and the current session was rotated.",
  "密码修改失败；未修改 Linux 或 SSH 密码。":
    "The password change failed; Linux and SSH passwords were not changed.",
  活动会话: "Active Sessions",
  "空闲 30 分钟，绝对 12 小时":
    "30-minute idle timeout · 12-hour absolute timeout",
  "已撤销 {count} 个其他会话。": "Revoked {count} other sessions.",
  撤销其他会话: "Revoke other sessions",
  创建时间: "Created",
  最近活动: "Last activity",
  来源: "Source",
  绝对到期: "Absolute expiry",
  状态: "Status",
  操作: "Action",
  撤销: "Revoke",
  "CLI Tokens": "CLI Tokens",
  "仅用于自己的Portal Job；Token明文只显示一次":
    "Only for your own Portal Jobs; plaintext is shown once",
  Token名称: "Token label",
  "例如：开发容器": "For example: development container",
  有效期: "Expiration",
  "30天": "30 days",
  "90天": "90 days",
  "365天": "365 days",
  永不过期: "Never expires",
  创建Token: "Create token",
  "新CLI Token（仅显示一次）": "New CLI Token (shown once)",
  复制Token: "Copy token",
  "请输入Token名称和当前网页密码。":
    "Enter a token label and your current web password.",
  "CLI Token已创建；关闭本页后不会再次显示明文。":
    "The CLI Token was created. Its plaintext will not be shown again after this page closes.",
  "CLI Token创建失败；没有生成或保存新的凭据。":
    "CLI Token creation failed. No new credential was generated or saved.",
  取消: "Cancel",
  继续: "Continue",
  总览: "Overview",
  "单节点 H100 平台实时概况": "Live status of the single-node H100 platform",
  平台状态: "Platform Status",
  风险与边界: "Risks & Boundaries",
  当前已知事项: "Known items",
  "GPU 设备": "GPU Devices",
  最近作业: "Recent Jobs",
  活跃告警: "Active Alerts",
  存储摘要: "Storage Summary",
  最近审计事件: "Recent Audit Events",
  我的作业: "My Jobs",
  新建作业: "New Job",
  工作区配额: "Workspace Quota",
  容器公钥: "Container Public Keys",
  当前状态: "Current Status",
  下一步: "Next Step",
  审批中: "under review",
  "已批准，等待创建": "approved and awaiting creation",
  "创建失败，管理员处理中": "provisioning failed; administrator action pending",
  尚未申请: "not requested",
  "Portal 账号已激活，计算环境{state}":
    "The Portal account is active; the compute environment is {state}",
  "Portal 账号": "Portal Account",
  "Portal 登录身份已建立": "Portal sign-in identity created",
  计算环境: "Compute Environment",
  "管理员正在执行受控 Stage；Lease 尚未开始":
    "An administrator is performing the controlled stage; the lease has not started",
  "资源尚未执行 Provision，Lease 尚未开始":
    "Resources have not been provisioned and the lease has not started",
  "等待管理员审批，尚未创建任何资源":
    "Waiting for administrator approval; no resources have been created",
  "创建失败；原申请保留，Lease 未启动":
    "Provisioning failed; the original request is retained and the lease was not started",
  "尚未创建 Linux、Container 或 Lease":
    "No Linux identity, container, or lease has been created",
  "计算资源申请状态暂时不可用；未将其显示为未申请。":
    "The compute request status is temporarily unavailable; it is not shown as not requested.",
  申请计算资源: "Request Compute Resources",
  "用户申请 → 管理员审批 → 资源规划":
    "User request → Administrator approval → Resource planning",
  "状态：等待管理员审批 · GPU {gpu} · Storage 300GB · Lease 4天":
    "Status: awaiting administrator approval · GPU {gpu} · Storage 300 GB · Lease 4 days",
  查看申请: "View request",
  撤回申请: "Withdraw request",
  "计算环境创建失败，平台管理员正在处理。你的申请仍被保留，无需重新提交。":
    "Compute environment provisioning failed and a platform administrator is handling it. Your request is retained; do not submit it again.",
  查看处理状态: "View resolution status",
  "申请已批准，受控 Stage 正在进行。Lease 仍未启动。":
    "The request is approved and controlled staging is in progress. The lease has not started.",
  "申请已批准，正在等待创建。当前仍没有 Container、Quota、Slurm Association 或 Lease。":
    "The request is approved and awaiting creation. There is still no container, quota, Slurm association, or lease.",
  查看批准状态: "View approval status",
  "上次申请未通过：{reason}": "Previous request rejected: {reason}",
  未提供原因: "No reason provided",
  "申请标准开发环境；提交申请不会自动创建服务器资源，也不会开始 Lease。":
    "Request the standard development environment. Submitting a request does not automatically create server resources or start a lease.",
  申请未能撤回: "The request could not be withdrawn",
  设置SSH密钥: "Set up SSH key",
  用于未来自己的开发容器: "For your future development container",
  "计算身份获批后，可登记 SSH 公钥；平台不会要求上传私钥。":
    "After the compute identity is approved, you can register an SSH public key. The platform will never ask you to upload a private key.",
  "修改 Portal 密码与管理会话":
    "Change your Portal password and manage sessions",
  查看开户与资源申请说明: "View account setup and resource request guidance",
  "登录身份保持 ACTIVE": "Sign-in identity remains ACTIVE",
  "计算环境已准备，下一步设置 SSH 密钥":
    "The compute environment is ready. Set up your SSH key next.",
  "Container 已创建但保持停止":
    "The container has been created and remains stopped",
  "96小时倒计时尚未启动": "The 96-hour countdown has not started",
  "设置 Container SSH 密钥": "Set up Container SSH Key",
  "Scope 固定为 CONTAINER；私钥只保存在你的电脑":
    "Scope is fixed to CONTAINER; the private key stays only on your computer",
  "计算资源已经安全 Stage。登记你自己的 ED25519 公钥后，你可以自行确认并激活环境。":
    "Compute resources have been staged safely. After registering your own ED25519 public key, you can confirm and activate the environment yourself.",
  "当前不能启动容器、打开网页终端或提交作业；Host SSH 始终禁用。":
    "You cannot start the container, open the web terminal, or submit jobs yet. Host SSH remains disabled.",
  申请恢复: "Request Restore",
  续期申请待审批: "Renewal request pending approval",
  续期审批: "Renewal Approvals",
  恢复审批: "Restore Approvals",
  申请续期: "Request Renewal",
  将在到期前24小时开放续期: "Renewal opens 24 hours before expiry",
  续期申请未提交: "The renewal request was not submitted",
  提交后需要管理员审批: "Administrator approval is required after submission",
  提交后由平台自动校验并批准:
    "The platform validates and approves the request automatically",
  "计算环境状态暂时不可用。":
    "Compute environment status is temporarily unavailable.",
  "开发容器、计算租约和作业入口":
    "Development container, compute lease, and job entry points",
  当前计算资源有效至: "Current compute resources valid until",
  计算资源已于以下时间到期: "Compute resources expired at",
  "剩余 {remaining} · 每次最多续期4天":
    "{remaining} remaining · Renew for up to 4 days each time",
  已到期: "Expired",
  "{days}天 {hours}小时": "{days}d {hours}h",
  "{hours}小时 {minutes}分": "{hours}h {minutes}m",
  "Host SSH 已按普通用户策略关闭":
    "Host SSH is disabled by ordinary-user policy",
  "CPU Development · 无 GPU Device": "CPU Development · no GPU device",
  "开发容器 · H100 × 1 过渡分配":
    "Development container · transitional H100 × 1 allocation",
  "开发容器 · 无 GPU Device；H100 作业按需调度":
    "Development container · no GPU device; H100 jobs are scheduled on demand",
  "此容器仍处于旧版 H100 过渡绑定；平台将自动收敛为按作业调度。":
    "This container still has a legacy transitional H100 binding; the platform will automatically converge it to job-based scheduling.",
  "开发容器不挂载 GPU；H100 仅在作业运行期间由 Slurm 按需分配并在结束后自动释放。":
    "The development container has no GPU mounted. Slurm allocates an H100 only while a job runs and releases it automatically afterward.",
  "容器 GPU": "Container GPU",
  "GPU 作业": "GPU Job",
  "H100 × 1 · 按需调度": "H100 × 1 · On-demand scheduling",
  GPU任务上限: "GPU Job Limit",
  通过作业页面提交: "Submit through the Jobs page",
  可用空间: "Available Space",
  正在计算: "Calculating",
  "总配额 {quota}": "Total quota {quota}",
  开始开发: "Start Developing",
  进入自己的长期开发容器: "Open your long-running development container",
  "CPU Development 容器用于编写代码、编译和准备数据，不挂载 GPU Device。":
    "Use the CPU Development container to write code, compile, and prepare data; it has no GPU device mounted.",
  打开网页终端: "Open Web Terminal",
  查看SSH连接: "View SSH Access",
  提交计算任务: "Submit a Compute Job",
  "CPU 或最多1张GPU": "CPU or up to 1 GPU",
  "选择工作区中的脚本，通过Portal提交到Slurm。":
    "Select a script in your workspace and submit it to Slurm through the Portal.",
  自己的长期开发容器: "Your long-running development container",
  "开发容器始终不挂载 GPU；GPU Development 开通最多 1 张 H100 的按需作业额度。":
    "Development containers never mount a GPU. GPU Development enables an on-demand job entitlement of up to one H100.",
  "GPU Development 开通 H100 作业额度；常驻开发容器仍不挂载 GPU。":
    "GPU Development enables the H100 job entitlement; the resident development container still has no GPU mounted.",
  "GPU Development 将作业上限固定为 1；H100 仅在作业运行期间分配并在结束后自动释放。":
    "GPU Development fixes the job limit at one. An H100 is allocated only while a job runs and is released automatically afterward.",
  认证: "Authentication",
  "用户密钥 Fingerprint": "User Key Fingerprint",
  未安装: "Not installed",
  "SSH 命令": "SSH Command",
  SSH命令已复制: "SSH command copied",
  "VS Code配置已复制": "VS Code configuration copied",
  复制命令: "Copy Command",
  "复制 VS Code 配置": "Copy VS Code Configuration",
  "租约、容器或SSH公钥当前不可用。":
    "The lease, container, or SSH public key is currently unavailable.",
  "开发容器连接信息暂时不可用。":
    "Development container connection details are temporarily unavailable.",
  自己的长期开发环境: "Your long-running development environment",
  自己的持久化私有存储和总配额:
    "Your persistent private storage and total quota",
  私有存储总配额: "Total Private Storage Quota",
  "已使用量包含 /home/<用户名> 与 /workspace；开发容器、CPU 作业和 GPU 作业均可通过相同路径直接零复制读写，并共享一个 XFS project 配额。":
    "Usage includes /home/<username> and /workspace. The development container and all CPU/GPU jobs read and write both paths directly with zero copy under one XFS project quota.",
  "脚本以当前Linux用户提交到批准的隔离运行时；/workspace 与自己的 /home 路径均为零复制持久化存储":
    "Scripts run as your Linux identity in the approved isolated runtime. /workspace and your /home path are both persistent zero-copy storage.",
  GPU计算请通过作业页面提交: "Submit GPU compute through the Jobs page",
  用途: "Purpose",
  开发与数据准备: "Development and data preparation",
  启动: "Start",
  重启: "Restart",
  停止: "Stop",
  容器操作被拒绝: "The container action was rejected",
  "开发容器状态暂时不可用。":
    "Development container status is temporarily unavailable.",
  还没有Portal作业: "No Portal jobs yet",
  从上方创建CPU或单GPU作业: "Create a CPU or single-GPU job above",
  名称: "Name",
  时限: "Time Limit",
  "{minutes} 分钟": "{minutes} minutes",
  提交中: "Submitting",
  日志: "Logs",
  "通过Portal提交CPU或单GPU Slurm任务":
    "Submit CPU or single-GPU Slurm jobs through the Portal",
  "脚本由Portal保存为自己的不可变作业快照，并以当前Linux用户提交":
    "The Portal saves the script as your immutable job snapshot and submits it as your current Linux user",
  作业名称: "Job Name",
  执行脚本: "Execution Script",
  "内存 MB": "Memory (MB)",
  "最长运行（分钟）": "Maximum Runtime (minutes)",
  运行环境: "Runtime Environment",
  "标准 Slurm": "Standard Slurm",
  "已批准 CUDA 容器": "Approved CUDA Container",
  提交作业: "Submit Job",
  "无法确认当前计算环境，请联系管理员。":
    "The current compute environment could not be verified. Contact an administrator.",
  作业提交失败: "Job submission failed",
  只显示当前账号提交的作业: "Only jobs submitted by the current account",
  "{name} · 日志": "{name} · Logs",
  关闭: "Close",
  取消作业: "Cancel Job",
  暂无输出: "No output yet",
  暂无错误输出: "No error output",
  自己的私有工作区和配额: "Your private workspace and quota",
  "存储状态暂时不可用。": "Storage status is temporarily unavailable.",
  总配额: "Total Quota",
  已使用: "Used",
  可用: "Available",
  隔离: "Isolation",
  "其他普通用户不能读取、列出或写入此目录。共享数据使用单独批准的数据集。":
    "Other ordinary users cannot read, list, or write this directory. Shared data uses separately approved datasets.",
  "过期资源可申请恢复，数据不会立即删除":
    "Expired resources can be restored by request; data is not deleted immediately",
  "资源所有者可自助恢复过期容器，无需管理员审批":
    "Resource owners can restore expired containers without administrator approval",
  "回收站暂时不可用。": "The recycle bin is temporarily unavailable.",
  回收站为空: "Recycle Bin Empty",
  当前没有过期的计算资源: "There are no expired compute resources",
  恢复申请待审批: "Restore request pending approval",
  "恢复申请已提交，等待管理员审批。":
    "Restore request submitted and pending administrator approval.",
  "恢复申请需要管理员审批；批准前不会启动容器或创建新 Lease":
    "Restore requests require administrator approval; no container is started and no new lease is created before approval",
  恢复申请由平台完成所有权和安全校验后自动批准:
    "Restore requests are automatically approved after platform ownership and security validation",
  历史恢复申请待处理: "Legacy restore request pending",
  恢复容器: "Restore Container",
  正在恢复: "Restoring",
  正在提交: "Submitting",
  恢复失败: "Restore Failed",
  "恢复失败，未启动容器。": "Restore failed; the container was not started.",
  "恢复完成，新的 96 小时 Lease 已创建。":
    "Restore completed and a new 96-hour lease was created.",
  "恢复状态：{status}": "Restore status: {status}",
  已过期: "Expired",
  租约到期: "Lease Expiry",
  进入回收站: "Moved to Recycle Bin",
  已停止: "Stopped",
  数据: "Data",
  已保留: "Retained",
  尚未建立计算身份: "Compute Identity Not Yet Created",
  "SSH 公钥登记将在计算资源申请获批后开放。平台只接收公钥，绝不会要求上传私钥。":
    "SSH public key registration opens after the compute resource request is approved. The platform accepts only public keys and will never ask you to upload a private key.",
  仅用于自己的开发容器: "Only for your development container",
  "Portal 新用户开户流程": "Portal onboarding for new users",
  "Portal 账号已激活，但尚未创建 Linux 用户、开发容器、配额、Slurm 或 Lease。":
    "The Portal account is active, but no Linux user, development container, quota, Slurm association, or lease has been created.",
  "下一阶段通过资源申请与管理员审批进入 Compute Provisioning。":
    "Next, submit a resource request and obtain administrator approval to enter compute provisioning.",
  "完成 Container SSH 密钥注册": "Complete Container SSH key registration",
  "计算身份、私有存储、Slurm association 与所选开发容器已经安全 Stage；Container 保持停止且不占用 H100，Lease 尚未启动。":
    "The compute identity, private storage, Slurm association, and selected development container are safely staged. The container remains stopped without occupying an H100, and the lease has not started.",
  "只登记你自己的 ED25519 公钥，Scope 固定为 CONTAINER。":
    "Register only your own ED25519 public key; scope is fixed to CONTAINER.",
  普通用户计算流程: "Ordinary user compute workflow",
  查看连接信息: "View Access Details",
  GPU任务: "GPU Jobs",
  "通过作业页面提交，当前最多使用1张GPU。":
    "Submit through the Jobs page; currently up to 1 GPU can be used.",
  进入作业页面: "Open Jobs",
  租约与恢复: "Lease & Restore",
  "到期前24小时可申请续期，每次最多4天。资源到期后开发容器会停止并进入回收站，数据不会立即删除；批准恢复后可继续使用。":
    "You can request renewal within 24 hours of expiry, for up to 4 days each time. After expiry, the development container stops and enters the recycle bin; data is not immediately deleted. Use can resume after an approved restore.",
  "到期前24小时可申请续期，每次最多4天。资源到期后开发容器会停止并进入回收站，数据不会立即删除；资源所有者可自助恢复。":
    "You can request renewal within 24 hours of expiry, for up to 4 days each time. After expiry, the development container stops and enters the recycle bin; data is retained and the resource owner can restore it directly.",
  宿主机: "Host",
  "宿主机 + 容器": "Host + Container",
  "SSH Key 用途": "SSH key scope",
  类型: "Type",
  注释: "Comment",
  添加时间: "Added",
  "关闭 SSH 密钥设置": "Close SSH key setup",
  "服务器保存你的公钥。连接 SSH 时，请使用与该公钥匹配的私钥。":
    "The server stores your public key. Use the matching private key when connecting over SSH.",
  "用途：仅开发容器": "Scope: development container only",
  "宿主 SSH：未启用": "Host SSH: disabled",
  计算环境启用步骤: "Compute environment activation steps",
  "1. SSH 密钥": "1. SSH key",
  "2. 确认资源": "2. Confirm resources",
  "3. 自助激活": "3. Self-activate",
  "4. 连接环境": "4. Connect to environment",
  "当前 authorized_keys 为 ABSENT，Shell 为 /usr/sbin/nologin，开发容器为 STOPPED。 登记公钥不会自动 Activate。":
    "authorized_keys is ABSENT, the shell is /usr/sbin/nologin, and the development container is STOPPED. Registering a public key does not automatically activate the environment.",
  "正在读取 SSH Key 记录…": "Loading SSH key records…",
  "SSH Key 记录暂时不可用。": "SSH key records are temporarily unavailable.",
  "使用 GPU 平台前，需要配置 SSH 密钥。":
    "Configure an SSH key before using the GPU platform.",
  "你可以生成新的密钥，也可以导入已有公钥。":
    "You can generate a new key or import an existing public key.",
  "{count} 把公钥可用于开发容器":
    "{count} public keys available for the development container",
  "{count} 把公钥已验证": "{count} public keys validated",
  "新增密钥仍只授权自己的开发容器，不会启用宿主访问。":
    "New keys still authorize only your development container; host access is not enabled.",
  "公钥已就绪；激活成功后只会安装到自己的开发容器。":
    "The public key is ready; after activation it will be installed only in your development container.",
  "尚未安装。": "Not installed.",
  生成新密钥: "Generate New Key",
  "浏览器无法生成 SSH Key": "The browser could not generate an SSH key",
  "检测到私钥内容，已阻止导入。Portal 只接受 .pub 公钥。":
    "Private-key material was detected and the import was blocked. The Portal accepts only .pub public keys.",
  "SSH 公钥内容超过 16KB 限制，已阻止导入。":
    "The SSH public key exceeds the 16 KB limit and the import was blocked.",
  "仅支持 .pub 公钥文件；不要选择私钥文件。":
    "Only .pub public-key files are supported; do not select a private-key file.",
  "公钥文件为空或超过 16KB 限制。":
    "The public-key file is empty or exceeds the 16 KB limit.",
  "SSH 公钥格式无效": "Invalid SSH public-key format",
  "SSH 公钥已验证；私钥未发送到服务器，authorized_keys 尚未安装。":
    "The SSH public key was validated. The private key was not sent to the server and authorized_keys has not been installed.",
  "SSH 公钥已验证；平台没有接收私钥，authorized_keys 尚未安装。":
    "The SSH public key was validated. The platform did not receive the private key and authorized_keys has not been installed.",
  "SSH 公钥登记失败": "SSH public-key registration failed",
  "计算环境正在激活；Lease 尚未开始，请稍后刷新状态。":
    "The compute environment is activating; the lease has not started. Refresh the status shortly.",
  "计算环境已激活；Lease 从 {starts} 开始，到 {expires} 到期。":
    "The compute environment is active. The lease starts at {starts} and expires at {expires}.",
  "计算环境激活失败；Lease 未开始":
    "Compute environment activation failed; the lease has not started",
  "SSH 密钥设置": "SSH Key Setup",
  "认证：SSH Public Key": "Authentication: SSH Public Key",
  "生成 ED25519 密钥": "Generate ED25519 Key",
  私钥存储: "Private-key storage",
  仅浏览器内存: "Browser memory only",
  "私钥只在本次设置流程中提供下载。平台不会保存你的私钥；如果丢失，需要重新添加新的 SSH Key。":
    "The private key is available for download only during this setup flow. The platform does not store it; if it is lost, add a new SSH key.",
  下载私钥: "Download Private Key",
  下载公钥: "Download Public Key",
  复制公钥: "Copy Public Key",
  "复制 Fingerprint": "Copy Fingerprint",
  我已经保存私钥: "I have saved the private key",
  "请先下载私钥，之后才能继续。": "Download the private key before continuing.",
  "当前版本生成标准 OpenSSH 未加密私钥。保存后可在本机运行 ssh-keygen -p -f {file} 添加本地保护密码；密码不会发送到 Portal。":
    "This version generates a standard unencrypted OpenSSH private key. After saving it, run ssh-keygen -p -f {file} locally to add a passphrase; the passphrase is never sent to the Portal.",
  导入已有公钥: "Import Existing Public Key",
  "仅支持 SSH 公钥（.pub）。不要上传没有 .pub 后缀的私钥文件。":
    "Only SSH public keys (.pub) are supported. Do not upload a private-key file without a .pub suffix.",
  "上传 .pub 文件": "Upload a .pub file",
  "或粘贴 .pub 内容": "Or paste .pub content",
  校验公钥: "Validate Public Key",
  私钥: "Private key",
  未接收: "Not received",
  "我确认这是与我持有的私钥匹配的 SSH 公钥":
    "I confirm this SSH public key matches the private key I hold",
  激活计算环境: "Activate Compute Environment",
  "后端将自动完成安全预检、安装容器公钥、启动并验证开发容器；Lease 只在全部成功后开始。":
    "The backend will run security prechecks, install the container public key, start and verify the development container. The lease starts only after every step succeeds.",
  "正在激活…": "Activating…",
  网页终端连接失败: "Web terminal connection failed",
  "只连接自己的开发容器；宿主访问保持禁用，GPU 为 NONE。":
    "Connected only to your development container; host access remains disabled and GPU is NONE.",
  "终端已关闭：{reason}": "Terminal closed: {reason}",
  "正在验证 Portal 会话、租约、资源所有权和容器安全状态…":
    "Validating the Portal session, lease, resource ownership, and container security state…",
  "终端启动被拒绝。请检查租约和开发容器状态。":
    "Terminal startup was rejected. Check the lease and development container status.",
  浏览器内连接自己的长期开发容器:
    "Connect to your long-running development container in the browser",
  "容器 Shell，不是宿主 Shell": "Container shell, not a host shell",
  "固定以当前登录身份进入自己的开发容器；GPU、Docker、MUNGE 与宿主访问均不可用。":
    "The current signed-in identity is restricted to its own development container. GPU, Docker, MUNGE, and host access are unavailable.",
  打开终端: "Open Terminal",
  关闭终端: "Close Terminal",
  "开发容器 Shell": "Development Container Shell",
  "空闲15分钟自动关闭；单次最长1小时；租约到期会立即终止":
    "Closes after 15 idle minutes · Maximum 1 hour per session · Terminates immediately when the lease expires",
  开发容器网页终端: "Development container web terminal",
  终端键盘输入: "Terminal keyboard input",
  "网页终端输入和输出不会写入 Portal 审计日志；审计仅记录会话打开、关闭、目标容器与安全结果。GPU任务仍须通过“作业”页面提交。":
    "Web terminal input and output are not written to Portal audit logs. Audit records only session open/close events, the target container, and security results. GPU jobs must still be submitted through the Jobs page.",
  "无法读取你的计算资源申请。":
    "Your compute resource request could not be loaded.",
  "申请标准开发环境；资源只在后续管理员 Gate 获批后创建":
    "Request the standard development environment; resources are created only after the subsequent administrator gate is approved",
  等待管理员审批: "Waiting for administrator approval",
  "已批准，正在等待创建": "Approved and waiting for provisioning",
  "申请 ID：{id}": "Request ID: {id}",
  "GPU 最大额度": "Maximum GPU Quota",
  开发环境: "Development Environment",
  "首次 Lease": "Initial Lease",
  "4天（激活成功后开始）": "4 days (starts after successful activation)",
  提交时间: "Submitted",
  "当前尚未创建 Linux 用户、Container、Quota、Slurm Association、GPU Policy 或 Lease。":
    "No Linux user, container, quota, Slurm association, GPU policy, or lease has been created yet.",
  "撤回中…": "Withdrawing…",
  申请撤回失败: "Request withdrawal failed",
  "上次申请未通过：{reason}。你可以重新申请。":
    "Previous request rejected: {reason}. You can submit a new request.",
  管理员未提供原因: "No reason was provided by the administrator",
  GPU需求: "GPU Requirement",
  "表示 Slurm 最大额度，不独占 GPU":
    "This is the maximum Slurm quota; GPUs are not reserved exclusively",
  "GPU 最大数量": "Maximum GPU Count",
  "GPU 任务通过 Portal 作业页面提交，最多 1 张。":
    "GPU jobs are submitted through the Portal Jobs page, with a maximum of 1 GPU.",
  标准开发环境: "Standard Development Environment",
  规格不可由普通用户修改: "Ordinary users cannot modify this profile",
  申请说明: "Request Details",
  "首次资源有效期 4 天；到期前最后24小时可申请续期。":
    "The initial resource period is 4 days; renewal can be requested during the final 24 hours.",
  "用途 / Project Description": "Purpose / Project Description",
  "例如：多用户平台验收": "Example: multi-user platform validation",
  "备注（可选）": "Note (optional)",
  "提交后进入管理员审批。管理员批准一次后，系统自动完成安全检查和环境创建； Lease 仍只会在你登记 Container 公钥并 Activate 后开始。":
    "After submission, the request enters administrator review. Once approved, the system automatically performs security checks and environment provisioning. The lease starts only after you register a Container public key and activate the environment.",
  计算资源申请提交失败: "Compute resource request submission failed",
  提交申请: "Submit Request",
  暂无数据: "No Data",
  数据源没有返回记录: "The data source returned no records",
  "数据源没有返回记录；UNKNOWN 不会被折叠为正常":
    "The data source returned no records; UNKNOWN is not collapsed into a healthy state",
};

type MessageValues = Record<string, string | number>;

function interpolate(message: string, values?: MessageValues): string {
  if (!values) return message;
  return message.replace(/\{(\w+)\}/g, (token, key: string) =>
    Object.hasOwn(values, key) ? String(values[key]) : token,
  );
}

type I18nValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (source: string, values?: MessageValues) => string;
};

const defaultValue: I18nValue = {
  locale: "zh-CN",
  setLocale: () => undefined,
  t: (source, values) => interpolate(source, values),
};

const I18nContext = createContext<I18nValue>(defaultValue);

export function I18nProvider({
  children,
  initialLocale,
}: {
  children: React.ReactNode;
  initialLocale: Locale;
}) {
  const [locale, updateLocale] = useState<Locale>(initialLocale);

  const setLocale = useCallback((nextLocale: Locale) => {
    updateLocale(nextLocale);
    document.documentElement.lang = nextLocale;
    document.cookie = `${LOCALE_COOKIE}=${nextLocale}; Path=/; Max-Age=31536000; SameSite=Lax`;
  }, []);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const t = useCallback(
    (source: string, values?: MessageValues) => {
      const message = locale === "en-US" ? (EN_US[source] ?? source) : source;
      return interpolate(message, values);
    },
    [locale],
  );

  const value = useMemo(
    () => ({ locale, setLocale, t }),
    [locale, setLocale, t],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  return useContext(I18nContext);
}

export function LanguageSwitcher({ compact = false }: { compact?: boolean }) {
  const { locale, setLocale, t } = useI18n();
  return (
    <label
      className={`language-switcher${compact ? " language-switcher-compact" : ""}`}
    >
      <span className="sr-only">{t("切换界面语言")}</span>
      <select
        aria-label={t("切换界面语言")}
        value={locale}
        onChange={(event) => {
          if (isLocale(event.target.value)) setLocale(event.target.value);
        }}
      >
        <option value="zh-CN">中文</option>
        <option value="en-US">English</option>
      </select>
    </label>
  );
}
