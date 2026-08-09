"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button, StatusBadge } from "@h100-portal/ui";
import { SshKeyEnrollment } from "../../../components/SshKeyEnrollment";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "../../../components/PortalShell";
import {
  me,
  sshKeys,
  startManagedContainer,
  userDetail,
} from "../../../lib/api";
import { copyText } from "../../../lib/ssh-key";

type ConnectionView = "host" | "container" | "vscode" | null;

const APPROVED_HOST = "10.82.36.1";

export default function AccessPage() {
  const [setupOpen, setSetupOpen] = useState(false);
  const [connectionView, setConnectionView] = useState<ConnectionView>(null);
  const [gateMessage, setGateMessage] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const current = useQuery({ queryKey: ["me"], queryFn: me, retry: false });
  const userId = current.data?.user.id ?? "";
  const detail = useQuery({
    queryKey: ["user", userId],
    queryFn: () => userDetail(userId),
    enabled: Boolean(userId),
    retry: false,
  });
  const keyQuery = useQuery({
    queryKey: ["ssh-keys", userId],
    queryFn: () => sshKeys(userId),
    enabled: Boolean(userId),
    retry: false,
  });
  const startContainer = useMutation({
    mutationFn: (name: string) =>
      startManagedContainer(name, {
        idempotency_key: crypto.randomUUID(),
        expected_compute_state: "ACTIVE",
        expected_container_state: "STOPPED",
        expected_ssh_key_state: "INSTALLED",
      }),
    onSuccess: async () => {
      setGateMessage("开发容器已安全启动；正在刷新连接状态。");
      await queryClient.invalidateQueries({ queryKey: ["user", userId] });
    },
    onError: (error) => {
      setGateMessage(
        error instanceof Error
          ? `容器启动被拒绝：${error.message}`
          : "容器启动被拒绝；请刷新状态后重试。",
      );
    },
  });
  if (current.isPending || detail.isPending || keyQuery.isPending) {
    return (
      <>
        <PageHeading title="连接" description="宿主提交环境与长期开发容器" />
        <LoadingBlock />
      </>
    );
  }
  if (
    current.isError ||
    detail.isError ||
    keyQuery.isError ||
    !detail.data ||
    !keyQuery.data
  ) {
    return (
      <>
        <PageHeading title="连接" description="宿主提交环境与长期开发容器" />
        <ErrorBlock message="连接状态暂时不可用；页面不会显示未经验证的 SSH 命令。" />
      </>
    );
  }
  const user = detail.data.user;
  const linux = user.linux_identity ?? {};
  const computeState = String(linux.onboarding_state ?? "NOT_ENROLLED");
  const containerState = String(linux.container_state ?? "UNKNOWN");
  const validKeys = keyQuery.data.keys.filter(
    (key) => key.state === "VALIDATED" || key.state === "INSTALLED",
  );
  const hostKeys = validKeys.filter(
    (key) => key.scope === "HOST" || key.scope === "BOTH",
  );
  const containerKeys = validKeys.filter(
    (key) => key.scope === "CONTAINER" || key.scope === "BOTH",
  );
  const hostInstalledKey = hostKeys.find((key) => key.state === "INSTALLED");
  const containerInstalledKey = containerKeys.find(
    (key) => key.state === "INSTALLED",
  );
  const hostDisplayKey = hostInstalledKey ?? hostKeys[0];
  const containerDisplayKey = containerInstalledKey ?? containerKeys[0];
  const approvedHost = String(linux.approved_host ?? APPROVED_HOST);
  const hostServerState = String(linux.host_ssh_server ?? "NOT_READY");
  const containerServerState = String(
    linux.container_ssh_server ?? "NOT_READY",
  );
  const hostConnectionDisabled =
    hostKeys.length > 0 && (computeState !== "ACTIVE" || !hostInstalledKey);
  const containerConnectionDisabled =
    containerKeys.length > 0 &&
    (computeState !== "ACTIVE" || !containerInstalledKey);
  const hostCommand = `ssh -i <你的私钥路径> ${String(linux.unix_username)}@${approvedHost}`;
  const containerCommand = `ssh -i <你的私钥路径> -p ${String(linux.container_port)} ${String(
    linux.unix_username,
  )}@${approvedHost}`;
  const vscodeConfig = `Host h100-${String(linux.unix_username)}
    HostName ${approvedHost}
    User ${String(linux.unix_username)}
    IdentityFile <你的私钥路径>

Host h100-${String(linux.unix_username)}-dev
    HostName ${approvedHost}
    Port ${String(linux.container_port)}
    User ${String(linux.unix_username)}
    IdentityFile <你的私钥路径>`;

  function requestConnection(view: Exclude<ConnectionView, null>) {
    setConnectionView(null);
    const targetKeys = view === "host" ? hostKeys : containerKeys;
    if (!targetKeys.length) {
      setGateMessage("连接前需要配置 SSH 密钥。");
      setSetupOpen(true);
      return;
    }
    if (computeState === "STAGED") {
      setGateMessage("SSH 密钥已准备，等待计算身份激活。");
      return;
    }
    if (computeState !== "ACTIVE") {
      setGateMessage(`当前阻断状态：计算身份 ${computeState}。`);
      return;
    }
    if (
      (view === "container" || view === "vscode") &&
      containerState === "STOPPED"
    ) {
      setGateMessage(
        "计算身份已激活，但开发容器当前已停止。请先提交容器启动操作。",
      );
      return;
    }
    if (
      (view === "container" || view === "vscode") &&
      containerState !== "RUNNING"
    ) {
      setGateMessage(`当前阻断状态：容器 ${containerState}。`);
      return;
    }
    setGateMessage(null);
    setConnectionView(view);
  }

  return (
    <>
      <PageHeading
        title="连接"
        description="服务器保存公钥；SSH 客户端使用与之匹配的本地私钥"
        action={<StatusBadge value={computeState} />}
      />
      {gateMessage ? (
        <div className="notice connection-gate-message" role="status">
          {gateMessage}
        </div>
      ) : null}
      <div className="access-target-grid">
        <SectionCard
          title="宿主提交环境"
          subtitle="Slurm 提交、队列查看与文件管理"
        >
          <dl className="kv-grid">
            <div className="kv">
              <dt>状态</dt>
              <dd>{hostServerState}</dd>
            </div>
            <div className="kv">
              <dt>Host</dt>
              <dd>{approvedHost}</dd>
            </div>
            <div className="kv">
              <dt>Port</dt>
              <dd>22</dd>
            </div>
            <div className="kv">
              <dt>Username</dt>
              <dd>{String(linux.unix_username ?? "—")}</dd>
            </div>
            <div className="kv">
              <dt>GPU</dt>
              <dd>OUTSIDE SLURM DENIED</dd>
            </div>
            <div className="kv">
              <dt>认证方式</dt>
              <dd>SSH 公钥</dd>
            </div>
            <div className="kv">
              <dt>用途</dt>
              <dd>sbatch / srun / squeue / sacct / 文件管理</dd>
            </div>
            <div className="kv access-key-row">
              <dt>USER AUTHENTICATION KEY FINGERPRINT</dt>
              <dd className="mono ssh-fingerprint-value access-key-fingerprint">
                {hostDisplayKey?.fingerprint_sha256 ?? "未配置"}
              </dd>
            </div>
            <div className="kv">
              <dt>SSH Key State</dt>
              <dd>{hostDisplayKey?.state ?? "NOT_CONFIGURED"}</dd>
            </div>
            <div className="kv">
              <dt>HOST SSH SERVER FINGERPRINT</dt>
              <dd className="mono ssh-fingerprint-value">
                {String(
                  linux.host_server_fingerprint ?? "等待 Activate 后受控探针",
                )}
              </dd>
            </div>
            <div className="kv">
              <dt>Host SSH Client Validation</dt>
              <dd>
                {String(linux.host_ssh_client_validation ?? "NOT_STARTED")}
              </dd>
            </div>
          </dl>
          <div className="button-row access-actions">
            <Button
              tone="primary"
              type="button"
              disabled={hostConnectionDisabled}
              onClick={() => requestConnection("host")}
            >
              连接宿主机
            </Button>
            <Button
              type="button"
              disabled={hostConnectionDisabled}
              onClick={() => requestConnection("host")}
            >
              复制 SSH 命令
            </Button>
            <Button
              type="button"
              disabled={hostConnectionDisabled}
              onClick={() => requestConnection("host")}
            >
              查看连接配置
            </Button>
          </div>
          <div className="notice">
            GPU 不能在宿主登录会话中直接访问。GPU 仅通过 Slurm
            作业提供；当前调度节点保持 DRAIN。
          </div>
        </SectionCard>

        <SectionCard
          title="开发容器"
          subtitle="VS Code、Shell、编译与数据准备；GPU NONE"
        >
          <dl className="kv-grid">
            <div className="kv">
              <dt>状态</dt>
              <dd>{containerServerState}</dd>
            </div>
            <div className="kv">
              <dt>Host</dt>
              <dd>{approvedHost}</dd>
            </div>
            <div className="kv">
              <dt>Port</dt>
              <dd>{String(linux.container_port ?? "—")}</dd>
            </div>
            <div className="kv">
              <dt>Username</dt>
              <dd>{String(linux.unix_username ?? "—")}</dd>
            </div>
            <div className="kv">
              <dt>Container State</dt>
              <dd>
                <StatusBadge value={containerState} />
              </dd>
            </div>
            <div className="kv">
              <dt>GPU</dt>
              <dd>{String(linux.container_gpu ?? "NONE")}</dd>
            </div>
            <div className="kv">
              <dt>认证方式</dt>
              <dd>SSH 公钥</dd>
            </div>
            <div className="kv">
              <dt>用途</dt>
              <dd>VS Code Remote SSH / Shell / 开发 / 编译 / 数据准备</dd>
            </div>
            <div className="kv access-key-row">
              <dt>USER AUTHENTICATION KEY FINGERPRINT</dt>
              <dd className="mono ssh-fingerprint-value access-key-fingerprint">
                {containerDisplayKey?.fingerprint_sha256 ?? "未配置"}
              </dd>
            </div>
            <div className="kv">
              <dt>SSH Key State</dt>
              <dd>{containerDisplayKey?.state ?? "NOT_CONFIGURED"}</dd>
            </div>
            <div className="kv">
              <dt>CONTAINER SSH SERVER FINGERPRINT</dt>
              <dd className="mono ssh-fingerprint-value">
                {String(
                  linux.container_server_fingerprint ??
                    "等待 Activate 后受控探针",
                )}
              </dd>
            </div>
            <div className="kv">
              <dt>Container SSH Client Validation</dt>
              <dd>
                {String(linux.container_ssh_client_validation ?? "NOT_STARTED")}
              </dd>
            </div>
          </dl>
          <div className="button-row access-actions">
            {computeState === "ACTIVE" &&
            containerState === "STOPPED" &&
            containerInstalledKey ? (
              <Button
                tone="primary"
                type="button"
                disabled={startContainer.isPending}
                onClick={() =>
                  startContainer.mutate(String(linux.container_name))
                }
              >
                {startContainer.isPending ? "正在启动…" : "启动开发容器"}
              </Button>
            ) : null}
            <Button
              type="button"
              disabled={containerConnectionDisabled}
              onClick={() => requestConnection("container")}
            >
              连接开发容器
            </Button>
            <Button
              type="button"
              disabled={containerConnectionDisabled}
              onClick={() => requestConnection("vscode")}
            >
              VS Code Remote SSH
            </Button>
            <Button
              type="button"
              disabled={containerConnectionDisabled}
              onClick={() => requestConnection("container")}
            >
              查看连接配置
            </Button>
          </div>
        </SectionCard>
      </div>

      {connectionView ? (
        <section className="connection-output" aria-label="SSH 连接配置">
          <div className="detail-section-heading">
            <div>
              <h2>
                {connectionView === "vscode"
                  ? "VS Code Remote SSH"
                  : "SSH 命令"}
              </h2>
              <p className="muted">
                `&lt;你的私钥路径&gt;`
                仅代表用户本机路径。你需要使用生成该公钥时保存的私钥；Portal
                不保存该私钥。
              </p>
            </div>
            <Button type="button" onClick={() => setConnectionView(null)}>
              关闭
            </Button>
          </div>
          <pre className="connection-command">
            {connectionView === "vscode"
              ? vscodeConfig
              : connectionView === "container"
                ? containerCommand
                : hostCommand}
          </pre>
          <Button
            type="button"
            onClick={() =>
              void copyText(
                connectionView === "vscode"
                  ? vscodeConfig
                  : connectionView === "container"
                    ? containerCommand
                    : hostCommand,
              )
            }
          >
            {connectionView === "vscode" ? "复制配置" : "复制 SSH 命令"}
          </Button>
        </section>
      ) : null}

      {computeState === "STAGED" && validKeys.length > 0 ? (
        <div className="notice staged-waiting">
          SSH Key 已验证但尚未安装。宿主 authorized_keys 仍为 ABSENT，Shell 仍为
          /usr/sbin/nologin，容器保持 STOPPED。
          <Link className="table-link" href={`/users/${user.id}?tab=ssh`}>
            查看 Activate Dry-Run
          </Link>
        </div>
      ) : null}

      {setupOpen ? (
        <div className="drawer-backdrop" role="presentation">
          <aside
            className="ssh-key-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="SSH 密钥设置"
          >
            <SshKeyEnrollment
              userId={user.id}
              username={String(linux.unix_username ?? "origin-pilot")}
              computeState={computeState}
              managedUserId={String(linux.managed_user_id ?? "") || null}
              activateDryRun={
                user.compute_onboarding?.activate_dry_run?.plan ?? null
              }
              onClose={() => setSetupOpen(false)}
            />
          </aside>
        </div>
      ) : null}
    </>
  );
}
