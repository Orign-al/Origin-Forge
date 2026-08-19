"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { Button, Card, EmptyState, Input, StatusBadge } from "@h100-portal/ui";
import {
  ApiError,
  cancelSelfComputeRequest,
  cancelSelfJob,
  me,
  requestLeaseRenewal,
  requestRestore,
  selfContainer,
  selfContainerAction,
  selfContainerConnection,
  selfComputeRequest,
  selfEnvironment,
  selfJobLogs,
  selfJobs,
  selfRecycleBin,
  selfStorage,
  submitSelfJob,
  type ComputeLease,
  type SelfJob,
  type User,
} from "../lib/api";
import { copyText } from "../lib/ssh-key";
import { randomUuid } from "../lib/random-uuid";
import { useI18n, type Locale } from "../lib/i18n";
import { SshKeyEnrollment } from "./SshKeyEnrollment";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "./PortalShell";

const APPROVED_IMAGE =
  "nvcr.io#nvidia/cuda:13.2.0-base-ubuntu24.04@sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a";
const DEFAULT_JOB_SCRIPT = `set -eu

echo "hello from Slurm"
whoami
id`;

type Translate = ReturnType<typeof useI18n>["t"];

function localTime(value: string | undefined, locale: Locale) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(locale, {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function remaining(value: number | undefined, t: Translate) {
  if (!value || value <= 0) return t("已到期");
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  return days
    ? t("{days}天 {hours}小时", { days, hours })
    : t("{hours}小时 {minutes}分", { hours, minutes });
}

function bytes(value: number | null | undefined, t: Translate) {
  if (value === null || value === undefined) return t("正在计算");
  const units = ["B", "KB", "MB", "GB", "TB"];
  let next = value;
  let unit = 0;
  while (next >= 1024 && unit < units.length - 1) {
    next /= 1024;
    unit += 1;
  }
  return `${next.toFixed(unit > 2 ? 1 : 0)} ${units[unit]}`;
}

function jobErrorMessage(error: unknown, t: Translate) {
  if (
    error instanceof ApiError &&
    ["RESOURCE_OWNERSHIP_REJECTED", "SELF_COMPUTE_CONTEXT_INVALID"].includes(
      error.code,
    )
  ) {
    return t("无法确认当前计算环境，请联系管理员。");
  }
  return error instanceof ApiError ? t(error.message) : t("作业提交失败");
}

export function OrdinaryUnprovisionedDashboard({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const query = useQuery({
    queryKey: ["self-compute-request"],
    queryFn: selfComputeRequest,
    refetchInterval: 30_000,
  });
  const cancel = useMutation({
    mutationFn: (id: string) => cancelSelfComputeRequest(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["self-compute-request"],
      });
    },
  });
  const computeRequest = query.data?.request ?? null;
  const pending = ["REQUESTED", "UNDER_REVIEW"].includes(
    computeRequest?.status ?? "",
  );
  const approved = [
    "APPROVED",
    "RETRY_AUTHORIZED",
    "PROVISION_PLAN_READY",
    "PROVISIONING",
  ].includes(computeRequest?.status ?? "");
  const provisioning = computeRequest?.status === "PROVISIONING";
  const failed = computeRequest?.status === "FAILED";
  const environmentLabel = pending
    ? t("审批中")
    : approved
      ? t("已批准，等待创建")
      : failed
        ? t("创建失败，管理员处理中")
        : t("尚未申请");
  return (
    <>
      <PageHeading
        title="我的环境"
        description={t("Portal 账号已激活，计算环境{state}", {
          state: environmentLabel,
        })}
        action={<StatusBadge value="NOT PROVISIONED" />}
      />
      <div className="grid-compact ordinary-stats">
        <Card className="stat-panel">
          <div className="stat-label">{t("账号")}</div>
          <div className="stat-value compact-stat">
            <StatusBadge value={user.account_state} />
          </div>
          <div className="stat-detail">{t("Portal 登录身份已建立")}</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">{t("计算环境")}</div>
          <div className="stat-value compact-stat">
            <StatusBadge
              value={
                pending
                  ? "PENDING APPROVAL"
                  : approved
                    ? "APPROVED"
                    : failed
                      ? "FAILED"
                      : "NOT PROVISIONED"
              }
            />
          </div>
          <div className="stat-detail">
            {approved
              ? provisioning
                ? t("管理员正在执行受控 Stage；Lease 尚未开始")
                : t("资源尚未执行 Provision，Lease 尚未开始")
              : pending
                ? t("等待管理员审批，尚未创建任何资源")
                : failed
                  ? t("创建失败；原申请保留，Lease 未启动")
                  : t("尚未创建 Linux、Container 或 Lease")}
          </div>
        </Card>
      </div>
      {query.isError ? (
        <ErrorBlock
          message={t("计算资源申请状态暂时不可用；未将其显示为未申请。")}
        />
      ) : null}
      <div className="section-grid">
        <SectionCard
          title="申请计算资源"
          subtitle="用户申请 → 管理员审批 → 资源规划"
        >
          {pending && computeRequest ? (
            <>
              <p>
                {t(
                  "状态：等待管理员审批 · GPU {gpu} · Storage 300GB · Lease 4天",
                  {
                    gpu: computeRequest.requested_gpu_max,
                  },
                )}
              </p>
              <div className="button-row">
                <Link
                  className="ui-button ui-button-primary"
                  href="/compute-request"
                >
                  {t("查看申请")}
                </Link>
                {computeRequest.status === "REQUESTED" ? (
                  <Button
                    disabled={cancel.isPending}
                    onClick={() => cancel.mutate(computeRequest.id)}
                  >
                    {t("撤回申请")}
                  </Button>
                ) : null}
              </div>
            </>
          ) : failed && computeRequest ? (
            <>
              <div className="notice">
                {computeRequest.user_status_message ??
                  t(
                    "计算环境创建失败，平台管理员正在处理。你的申请仍被保留，无需重新提交。",
                  )}
              </div>
              <Link
                className="ui-button ui-button-primary"
                href="/compute-request"
              >
                {t("查看处理状态")}
              </Link>
            </>
          ) : approved && computeRequest ? (
            <>
              <p>
                {provisioning
                  ? t("申请已批准，受控 Stage 正在进行。Lease 仍未启动。")
                  : t(
                      "申请已批准，正在等待创建。当前仍没有 Container、Quota、Slurm Association 或 Lease。",
                    )}
              </p>
              <Link
                className="ui-button ui-button-primary"
                href="/compute-request"
              >
                {t("查看批准状态")}
              </Link>
            </>
          ) : (
            <>
              {computeRequest?.status === "REJECTED" ? (
                <div className="notice">
                  {t("上次申请未通过：{reason}", {
                    reason: computeRequest.review_note ?? t("未提供原因"),
                  })}
                </div>
              ) : null}
              <p>
                {t(
                  "申请标准开发环境；提交申请不会自动创建服务器资源，也不会开始 Lease。",
                )}
              </p>
              <Link
                className="ui-button ui-button-primary"
                href="/compute-request"
              >
                {t("申请计算资源")}
              </Link>
            </>
          )}
          {cancel.isError ? (
            <div className="form-error">
              {cancel.error instanceof ApiError
                ? t(cancel.error.message)
                : t("申请未能撤回")}
            </div>
          ) : null}
        </SectionCard>
        <SectionCard title="设置SSH密钥" subtitle="用于未来自己的开发容器">
          <p>{t("计算身份获批后，可登记 SSH 公钥；平台不会要求上传私钥。")}</p>
          <Link className="ui-button" href="/ssh-keys">
            {t("设置SSH密钥")}
          </Link>
        </SectionCard>
      </div>
      <div className="section-grid">
        <SectionCard title="账号与安全">
          <Link className="table-link" href="/account/security">
            {t("修改 Portal 密码与管理会话")}
          </Link>
        </SectionCard>
        <SectionCard title="帮助">
          <Link className="table-link" href="/help">
            {t("查看开户与资源申请说明")}
          </Link>
        </SectionCard>
      </div>
    </>
  );
}

export function OrdinaryStagedDashboard({ user }: { user: User }) {
  const { t } = useI18n();
  return (
    <>
      <PageHeading
        title="我的环境"
        description="计算环境已准备，下一步设置 SSH 密钥"
        action={<StatusBadge value="KEY ENROLLMENT PENDING" />}
      />
      <div className="grid-compact ordinary-stats">
        <Card className="stat-panel">
          <div className="stat-label">{t("Portal 账号")}</div>
          <div className="stat-value compact-stat">
            <StatusBadge value={user.account_state} />
          </div>
          <div className="stat-detail">{t("登录身份保持 ACTIVE")}</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">{t("计算环境")}</div>
          <div className="stat-value compact-stat">
            <StatusBadge value="STAGED" />
          </div>
          <div className="stat-detail">{t("Container 已创建但保持停止")}</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">Lease</div>
          <div className="stat-value compact-stat">
            <StatusBadge value="NOT STARTED" />
          </div>
          <div className="stat-detail">{t("96小时倒计时尚未启动")}</div>
        </Card>
      </div>
      <SectionCard
        title="设置 Container SSH 密钥"
        subtitle="Scope 固定为 CONTAINER；私钥只保存在你的电脑"
      >
        <p>
          {t(
            "计算资源已经安全 Stage。登记你自己的 ED25519 公钥后，管理员才能在下一阶段激活环境。",
          )}
        </p>
        <Link className="ui-button ui-button-primary" href="/ssh-keys">
          {t("设置 SSH 密钥")}
        </Link>
      </SectionCard>
      <div className="notice">
        {t("当前不能启动容器、打开网页终端或提交作业；Host SSH 始终禁用。")}
      </div>
    </>
  );
}

function LeaseAction({ lease }: { lease: ComputeLease }) {
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const mutation = useMutation({
    mutationFn: () =>
      requestLeaseRenewal({
        duration_seconds: 345600,
        idempotency_key: randomUuid(),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["self-environment"] });
    },
  });
  if (!lease.active) {
    return (
      <Link className="ui-button ui-button-primary" href="/recycle-bin">
        {t("申请恢复")}
      </Link>
    );
  }
  if (lease.pending_renewal_id) {
    return <StatusBadge value={t("续期申请待审批")} />;
  }
  return (
    <div>
      <Button
        tone="primary"
        disabled={!lease.renewal_available || mutation.isPending}
        onClick={() => mutation.mutate()}
      >
        {t("申请续期")}
      </Button>
      {!lease.renewal_available ? (
        <div className="muted compact-help">
          {t("将在到期前24小时开放续期")}
        </div>
      ) : null}
      {mutation.isError ? (
        <div className="form-error">
          {mutation.error instanceof ApiError
            ? t(mutation.error.message)
            : t("续期申请未提交")}
        </div>
      ) : null}
    </div>
  );
}

export function OrdinaryDashboard() {
  const { locale, t } = useI18n();
  const query = useQuery({
    queryKey: ["self-environment"],
    queryFn: selfEnvironment,
    refetchInterval: 30_000,
  });
  const storage = useQuery({
    queryKey: ["self-storage"],
    queryFn: selfStorage,
    refetchInterval: 60_000,
  });
  if (query.isPending) return <LoadingBlock />;
  if (query.isError)
    return <ErrorBlock message={t("计算环境状态暂时不可用。")} />;
  const environment = query.data.environment;
  const lease = environment.lease;
  const warning = lease.active && (lease.remaining_seconds ?? 0) <= 86400;
  const leaseClass = !lease.active
    ? "lease-band lease-expired"
    : warning
      ? "lease-band lease-warning"
      : "lease-band";
  const available = storage.data?.storage.available_bytes;
  return (
    <>
      <PageHeading
        title="我的环境"
        description="开发容器、计算租约和作业入口"
        action={<StatusBadge value={environment.state} />}
      />
      <section className={leaseClass}>
        <div>
          <div className="stat-label">
            {lease.active
              ? t("当前计算资源有效至")
              : t("计算资源已于以下时间到期")}
          </div>
          <div className="lease-expiry">
            {localTime(lease.expires_at, locale)}
          </div>
          <div className="muted">
            {t("剩余 {remaining} · 每次最多续期4天", {
              remaining: remaining(lease.remaining_seconds, t),
            })}
          </div>
        </div>
        <LeaseAction lease={lease} />
      </section>
      <div className="grid-compact ordinary-stats">
        <Card className="stat-panel">
          <div className="stat-label">{t("计算环境")}</div>
          <div className="stat-value compact-stat">
            <StatusBadge value={environment.state} />
          </div>
          <div className="stat-detail">
            {t("Host SSH 已按普通用户策略关闭")}
          </div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">{t("开发容器")}</div>
          <div className="stat-value compact-stat">
            <StatusBadge value={environment.container.state} />
          </div>
          <div className="stat-detail">{t("用于开发，不直接提供GPU")}</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">{t("GPU任务上限")}</div>
          <div className="stat-value">{environment.gpu_max}</div>
          <div className="stat-detail">{t("通过作业页面提交")}</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">{t("可用空间")}</div>
          <div className="stat-value small-value">{bytes(available, t)}</div>
          <div className="stat-detail">
            {t("总配额 {quota}", {
              quota: bytes(environment.storage.quota_bytes, t),
            })}
          </div>
        </Card>
      </div>
      <div className="section-grid">
        <SectionCard title="开始开发" subtitle="进入自己的长期开发容器">
          <p>
            {t("在开发容器中编写代码、编译和准备数据。容器不直接分配GPU。")}
          </p>
          <div className="button-row">
            <Link className="ui-button ui-button-primary" href="/terminal">
              {t("打开网页终端")}
            </Link>
            <Link className="ui-button" href="/access">
              {t("查看SSH连接")}
            </Link>
          </div>
        </SectionCard>
        <SectionCard title="提交计算任务" subtitle="CPU 或最多1张GPU">
          <p>{t("选择工作区中的脚本，通过Portal提交到Slurm。")}</p>
          <Link className="ui-button ui-button-primary" href="/jobs">
            {t("新建作业")}
          </Link>
        </SectionCard>
      </div>
    </>
  );
}

export function OrdinaryConnection() {
  const { t } = useI18n();
  const query = useQuery({
    queryKey: ["self-container-connection"],
    queryFn: selfContainerConnection,
    refetchInterval: 30_000,
  });
  const [message, setMessage] = useState<string | null>(null);
  if (query.isPending) return <LoadingBlock />;
  if (query.isError)
    return <ErrorBlock message={t("开发容器连接信息暂时不可用。")} />;
  const connection = query.data.connection;
  return (
    <>
      <PageHeading
        title="连接"
        description="自己的长期开发容器"
        action={
          <StatusBadge
            value={connection.available ? "AVAILABLE" : "DISABLED"}
          />
        }
      />
      <SectionCard
        title="开发容器"
        subtitle="用于编写代码和开发，不直接提供GPU。"
      >
        <dl className="kv-grid">
          <div className="kv">
            <dt>Host</dt>
            <dd>{connection.host}</dd>
          </div>
          <div className="kv">
            <dt>Port</dt>
            <dd>{connection.port}</dd>
          </div>
          <div className="kv">
            <dt>Username</dt>
            <dd>{connection.username}</dd>
          </div>
          <div className="kv">
            <dt>{t("认证")}</dt>
            <dd>SSH Public Key</dd>
          </div>
          <div className="kv">
            <dt>GPU</dt>
            <dd>NONE</dd>
          </div>
          <div className="kv">
            <dt>{t("状态")}</dt>
            <dd>{connection.available ? "AVAILABLE" : "DISABLED"}</dd>
          </div>
          <div className="kv access-key-row">
            <dt>{t("用户密钥 Fingerprint")}</dt>
            <dd className="mono ssh-fingerprint-value">
              {connection.key_fingerprint ?? t("未安装")}
            </dd>
          </div>
        </dl>
        {connection.command ? (
          <div className="connection-output">
            <div className="field-label">{t("SSH 命令")}</div>
            <pre className="connection-command">{connection.command}</pre>
            <div className="button-row">
              <Button
                onClick={() =>
                  void copyText(connection.command ?? "").then(() =>
                    setMessage(t("SSH命令已复制")),
                  )
                }
              >
                {t("复制命令")}
              </Button>
              <Button
                onClick={() =>
                  void copyText(connection.vscode ?? "").then(() =>
                    setMessage(t("VS Code配置已复制")),
                  )
                }
              >
                {t("复制 VS Code 配置")}
              </Button>
            </div>
          </div>
        ) : (
          <div className="notice">{t("租约、容器或SSH公钥当前不可用。")}</div>
        )}
        {message ? (
          <div className="notice success-notice">{message}</div>
        ) : null}
      </SectionCard>
    </>
  );
}

export function OrdinaryContainer() {
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const query = useQuery({
    queryKey: ["self-container"],
    queryFn: selfContainer,
    refetchInterval: 30_000,
  });
  const mutation = useMutation({
    mutationFn: selfContainerAction,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["self-container"] }),
        queryClient.invalidateQueries({ queryKey: ["self-environment"] }),
        queryClient.invalidateQueries({
          queryKey: ["self-container-connection"],
        }),
      ]);
    },
  });
  if (query.isPending) return <LoadingBlock />;
  if (query.isError)
    return <ErrorBlock message={t("开发容器状态暂时不可用。")} />;
  const container = query.data.container;
  return (
    <>
      <PageHeading
        title="开发容器"
        description="自己的长期开发环境"
        action={<StatusBadge value={container.state} />}
      />
      <SectionCard title={container.name} subtitle="GPU计算请通过作业页面提交">
        <dl className="kv-grid">
          <div className="kv">
            <dt>CPU</dt>
            <dd>{container.cpus}</dd>
          </div>
          <div className="kv">
            <dt>Memory</dt>
            <dd>{container.memory_gb} GB</dd>
          </div>
          <div className="kv">
            <dt>PIDs</dt>
            <dd>{container.pids_limit}</dd>
          </div>
          <div className="kv">
            <dt>GPU</dt>
            <dd>NONE</dd>
          </div>
          <div className="kv">
            <dt>{t("用途")}</dt>
            <dd>{t("开发与数据准备")}</dd>
          </div>
        </dl>
        <div className="button-row access-actions">
          <Link
            className={`ui-button ui-button-primary ${container.state !== "RUNNING" ? "link-disabled" : ""}`}
            href={container.state === "RUNNING" ? "/terminal" : "#"}
            aria-disabled={container.state !== "RUNNING"}
          >
            {t("网页终端")}
          </Link>
          <Button
            disabled={container.state === "RUNNING" || mutation.isPending}
            onClick={() => mutation.mutate("start")}
          >
            {t("启动")}
          </Button>
          <Button
            disabled={container.state !== "RUNNING" || mutation.isPending}
            onClick={() => mutation.mutate("restart")}
          >
            {t("重启")}
          </Button>
          <Button
            disabled={container.state !== "RUNNING" || mutation.isPending}
            onClick={() => mutation.mutate("stop")}
          >
            {t("停止")}
          </Button>
        </div>
        {mutation.isError ? (
          <div className="error-box">
            {mutation.error instanceof Error
              ? mutation.error.message
              : t("容器操作被拒绝")}
          </div>
        ) : null}
      </SectionCard>
    </>
  );
}

function JobRows({
  jobs,
  onSelect,
}: {
  jobs: SelfJob[];
  onSelect: (job: SelfJob) => void;
}) {
  const { t } = useI18n();
  if (!jobs.length)
    return (
      <EmptyState
        title={t("还没有Portal作业")}
        detail={t("从上方创建CPU或单GPU作业")}
      />
    );
  return (
    <div className="ui-table-wrap">
      <table className="ui-table">
        <thead>
          <tr>
            <th>Job</th>
            <th>{t("名称")}</th>
            <th>{t("状态")}</th>
            <th>GPU</th>
            <th>{t("时限")}</th>
            <th>ExitCode</th>
            <th>{t("操作")}</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id}>
              <td>{job.slurm_job_id ?? t("提交中")}</td>
              <td>{job.name}</td>
              <td>
                <StatusBadge value={job.state} />
              </td>
              <td>{job.gpu_count}</td>
              <td>
                {t("{minutes} 分钟", {
                  minutes: Math.ceil(job.time_limit_seconds / 60),
                })}
              </td>
              <td>{job.exit_code ?? "—"}</td>
              <td>
                <Button onClick={() => onSelect(job)}>{t("日志")}</Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function OrdinaryJobs() {
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const query = useQuery({
    queryKey: ["self-jobs"],
    queryFn: selfJobs,
    refetchInterval: 10_000,
  });
  const [name, setName] = useState("training-job");
  const [script, setScript] = useState(DEFAULT_JOB_SCRIPT);
  const [cpus, setCpus] = useState(2);
  const [memory, setMemory] = useState(4096);
  const [gpu, setGpu] = useState<0 | 1>(0);
  const [minutes, setMinutes] = useState(30);
  const [containerized, setContainerized] = useState(false);
  const [selected, setSelected] = useState<SelfJob | null>(null);
  const logs = useQuery({
    queryKey: ["self-job-logs", selected?.id],
    queryFn: () => selfJobLogs(selected?.id ?? ""),
    enabled: Boolean(selected),
    refetchInterval:
      selected && !["COMPLETED", "FAILED", "CANCELLED"].includes(selected.state)
        ? 5000
        : false,
  });
  const submit = useMutation({
    mutationFn: () =>
      submitSelfJob({
        name,
        script,
        cpus,
        memory_mb: memory,
        gpu_count: gpu,
        time_limit_seconds: minutes * 60,
        image_ref: containerized ? APPROVED_IMAGE : null,
        idempotency_key: randomUuid(),
      }),
    onSuccess: async () =>
      queryClient.invalidateQueries({ queryKey: ["self-jobs"] }),
  });
  const cancel = useMutation({
    mutationFn: cancelSelfJob,
    onSuccess: async () =>
      queryClient.invalidateQueries({ queryKey: ["self-jobs"] }),
  });
  function onSubmit(event: FormEvent) {
    event.preventDefault();
    submit.mutate();
  }
  return (
    <>
      <PageHeading
        title="作业"
        description="通过Portal提交CPU或单GPU Slurm任务"
        action={<StatusBadge value="MAX 1 GPU" />}
      />
      <SectionCard
        title="新建作业"
        subtitle="脚本由Portal保存为自己的不可变作业快照，并以当前Linux用户提交"
      >
        <form className="job-form-grid" onSubmit={onSubmit}>
          <label>
            {t("作业名称")}
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="job-script-field">
            {t("执行脚本")}
            <textarea
              className="ui-textarea mono"
              value={script}
              onChange={(event) => setScript(event.target.value)}
              rows={14}
              maxLength={8192}
              required
              spellCheck={false}
            />
          </label>
          <label>
            CPU
            <Input
              type="number"
              min={1}
              max={8}
              value={cpus}
              onChange={(e) => setCpus(Number(e.target.value))}
            />
          </label>
          <label>
            {t("内存 MB")}
            <Input
              type="number"
              min={256}
              max={32768}
              value={memory}
              onChange={(e) => setMemory(Number(e.target.value))}
            />
          </label>
          <label>
            GPU
            <select
              className="ui-input"
              value={gpu}
              onChange={(e) => setGpu(Number(e.target.value) as 0 | 1)}
            >
              <option value={0}>0</option>
              <option value={1}>1</option>
            </select>
          </label>
          <label>
            {t("最长运行（分钟）")}
            <Input
              type="number"
              min={1}
              max={5760}
              value={minutes}
              onChange={(e) => setMinutes(Number(e.target.value))}
            />
          </label>
          <label>
            {t("运行环境")}
            <select
              className="ui-input"
              value={containerized ? "approved" : "host"}
              onChange={(e) => setContainerized(e.target.value === "approved")}
            >
              <option value="host">{t("标准 Slurm")}</option>
              <option value="approved">{t("已批准 CUDA 容器")}</option>
            </select>
          </label>
          <div className="job-submit-row">
            <Button
              tone="primary"
              type="submit"
              disabled={submit.isPending || script.length === 0}
            >
              {t("提交作业")}
            </Button>
          </div>
        </form>
        {submit.isError ? (
          <div className="error-box">{jobErrorMessage(submit.error, t)}</div>
        ) : null}
      </SectionCard>
      <SectionCard title="我的作业" subtitle="只显示当前账号提交的作业">
        {query.isPending ? (
          <LoadingBlock />
        ) : query.isError ? (
          <ErrorBlock />
        ) : (
          <JobRows jobs={query.data.jobs} onSelect={setSelected} />
        )}
      </SectionCard>
      {selected ? (
        <SectionCard
          title={t("{name} · 日志", { name: selected.name })}
          action={
            <Button onClick={() => setSelected(null)}>{t("关闭")}</Button>
          }
        >
          <div className="button-row">
            <StatusBadge value={selected.state} />
            {!["COMPLETED", "FAILED", "CANCELLED"].includes(selected.state) ? (
              <Button
                tone="danger"
                disabled={cancel.isPending}
                onClick={() => cancel.mutate(selected.id)}
              >
                {t("取消作业")}
              </Button>
            ) : null}
          </div>
          <h3>stdout</h3>
          <pre className="job-log">{logs.data?.stdout || t("暂无输出")}</pre>
          <h3>stderr</h3>
          <pre className="job-log">
            {logs.data?.stderr || t("暂无错误输出")}
          </pre>
        </SectionCard>
      ) : null}
    </>
  );
}

export function OrdinaryStorage() {
  const { t } = useI18n();
  const query = useQuery({
    queryKey: ["self-storage"],
    queryFn: selfStorage,
    refetchInterval: 60_000,
  });
  if (query.isPending) return <LoadingBlock />;
  if (query.isError) return <ErrorBlock message={t("存储状态暂时不可用。")} />;
  const storage = query.data.storage;
  return (
    <>
      <PageHeading
        title="存储"
        description="自己的私有工作区和配额"
        action={<StatusBadge value={storage.state} />}
      />
      <SectionCard title="工作区配额">
        <dl className="kv-grid">
          <div className="kv">
            <dt>{t("总配额")}</dt>
            <dd>{bytes(storage.quota_bytes, t)}</dd>
          </div>
          <div className="kv">
            <dt>{t("已使用")}</dt>
            <dd>{bytes(storage.used_bytes, t)}</dd>
          </div>
          <div className="kv">
            <dt>{t("可用")}</dt>
            <dd>{bytes(storage.available_bytes, t)}</dd>
          </div>
          <div className="kv">
            <dt>{t("隔离")}</dt>
            <dd>PRIVATE</dd>
          </div>
        </dl>
        <div className="notice">
          {t(
            "其他普通用户不能读取、列出或写入此目录。共享数据使用单独批准的数据集。",
          )}
        </div>
      </SectionCard>
    </>
  );
}

export function OrdinaryRecycleBin() {
  const queryClient = useQueryClient();
  const { locale, t } = useI18n();
  const query = useQuery({
    queryKey: ["self-recycle-bin"],
    queryFn: selfRecycleBin,
  });
  const restore = useMutation({
    mutationFn: (itemId: string) => requestRestore(itemId),
    onSuccess: async () =>
      queryClient.invalidateQueries({ queryKey: ["self-recycle-bin"] }),
  });
  if (query.isPending) return <LoadingBlock />;
  if (query.isError) return <ErrorBlock message={t("回收站暂时不可用。")} />;
  return (
    <>
      <PageHeading
        title="回收站"
        description="过期资源可申请恢复，数据不会立即删除"
      />
      {!query.data.items.length ? (
        <Card>
          <EmptyState
            title={t("回收站为空")}
            detail={t("当前没有过期的计算资源")}
          />
        </Card>
      ) : (
        query.data.items.map((item) => (
          <SectionCard
            key={item.id}
            title={item.resource_name}
            action={
              <StatusBadge
                value={
                  item.state === "RESTORE_PENDING"
                    ? t("恢复申请待审批")
                    : t("已过期")
                }
              />
            }
          >
            <dl className="kv-grid">
              <div className="kv">
                <dt>{t("租约到期")}</dt>
                <dd>{localTime(item.expires_at, locale)}</dd>
              </div>
              <div className="kv">
                <dt>{t("进入回收站")}</dt>
                <dd>{localTime(item.recycled_at, locale)}</dd>
              </div>
              <div className="kv">
                <dt>{t("开发容器")}</dt>
                <dd>{t("已停止")}</dd>
              </div>
              <div className="kv">
                <dt>{t("数据")}</dt>
                <dd>{t("已保留")}</dd>
              </div>
            </dl>
            <div className="button-row access-actions">
              <Button
                tone="primary"
                disabled={restore.isPending || item.state !== "RECYCLE_BIN"}
                onClick={() => restore.mutate(item.id)}
              >
                {t("申请恢复")}
              </Button>
            </div>
          </SectionCard>
        ))
      )}
    </>
  );
}

export function OrdinarySshKeys() {
  const { t } = useI18n();
  const current = useQuery({ queryKey: ["me"], queryFn: me });
  if (current.isPending) return <LoadingBlock />;
  if (current.isError) return <ErrorBlock />;
  if (current.data.user.resource_onboarding_state === "NOT_ENROLLED") {
    return (
      <>
        <PageHeading title="SSH密钥" description="用于未来自己的开发容器" />
        <SectionCard title="尚未建立计算身份">
          <p>
            {t(
              "SSH 公钥登记将在计算资源申请获批后开放。平台只接收公钥，绝不会要求上传私钥。",
            )}
          </p>
          <Button disabled>{t("设置SSH密钥")}</Button>
        </SectionCard>
      </>
    );
  }
  return (
    <>
      <PageHeading title="SSH密钥" description="仅用于自己的开发容器" />
      <SectionCard title="容器公钥">
        <SshKeyEnrollment
          userId={current.data.user.id}
          username={current.data.user.normalized_login}
          computeState={current.data.ssh_enrollment.compute_state}
          containerOnly
        />
      </SectionCard>
    </>
  );
}

export function OrdinaryHelp() {
  const { t } = useI18n();
  const current = useQuery({ queryKey: ["me"], queryFn: me });
  if (current.isPending) return <LoadingBlock />;
  if (current.isError) return <ErrorBlock />;
  if (current.data.user.resource_onboarding_state === "NOT_ENROLLED") {
    return (
      <>
        <PageHeading title="帮助" description="Portal 新用户开户流程" />
        <div className="section-grid">
          <SectionCard title="当前状态">
            <p>
              {t(
                "Portal 账号已激活，但尚未创建 Linux 用户、开发容器、配额、Slurm 或 Lease。",
              )}
            </p>
          </SectionCard>
          <SectionCard title="下一步">
            <p>
              {t("下一阶段通过资源申请与管理员审批进入 Compute Provisioning。")}
            </p>
          </SectionCard>
        </div>
      </>
    );
  }
  if (current.data.user.resource_onboarding_state === "STAGED") {
    return (
      <>
        <PageHeading title="帮助" description="完成 Container SSH 密钥注册" />
        <div className="section-grid">
          <SectionCard title="当前状态">
            <p>
              {t(
                "计算身份、私有存储、Slurm association 与无 GPU 开发容器已经安全 Stage；Container 保持停止，Lease 尚未启动。",
              )}
            </p>
          </SectionCard>
          <SectionCard title="下一步">
            <p>{t("只登记你自己的 ED25519 公钥，Scope 固定为 CONTAINER。")}</p>
            <Link className="table-link" href="/ssh-keys">
              {t("设置 SSH 密钥")}
            </Link>
          </SectionCard>
        </div>
      </>
    );
  }
  return (
    <>
      <PageHeading title="帮助" description="普通用户计算流程" />
      <div className="section-grid">
        <SectionCard title="开发容器">
          <p>{t("用于编写代码和开发，不直接提供GPU。")}</p>
          <Link className="table-link" href="/access">
            {t("查看连接信息")}
          </Link>
        </SectionCard>
        <SectionCard title="GPU任务">
          <p>{t("通过作业页面提交，当前最多使用1张GPU。")}</p>
          <Link className="table-link" href="/jobs">
            {t("进入作业页面")}
          </Link>
        </SectionCard>
      </div>
      <SectionCard title="租约与恢复">
        <p>
          {t(
            "到期前24小时可申请续期，每次最多4天。资源到期后开发容器会停止并进入回收站，数据不会立即删除；批准恢复后可继续使用。",
          )}
        </p>
      </SectionCard>
    </>
  );
}
