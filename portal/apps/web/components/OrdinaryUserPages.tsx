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
import { SshKeyEnrollment } from "./SshKeyEnrollment";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "./PortalShell";

const APPROVED_IMAGE =
  "nvcr.io#nvidia/cuda:13.2.0-base-ubuntu24.04@sha256:36cccda4bebc3b0b1ebe1907ead8169cf144d45df890be871b36b304cf91145a";

function localTime(value?: string) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function remaining(value = 0) {
  if (value <= 0) return "已到期";
  const days = Math.floor(value / 86400);
  const hours = Math.floor((value % 86400) / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  return days ? `${days}天 ${hours}小时` : `${hours}小时 ${minutes}分`;
}

function bytes(value: number | null | undefined) {
  if (value === null || value === undefined) return "正在计算";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let next = value;
  let unit = 0;
  while (next >= 1024 && unit < units.length - 1) {
    next /= 1024;
    unit += 1;
  }
  return `${next.toFixed(unit > 2 ? 1 : 0)} ${units[unit]}`;
}

export function OrdinaryUnprovisionedDashboard({ user }: { user: User }) {
  const queryClient = useQueryClient();
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
    ? "审批中"
    : approved
      ? "已批准，等待创建"
      : failed
        ? "创建失败，管理员处理中"
        : "尚未申请";
  return (
    <>
      <PageHeading
        title="我的环境"
        description={`Portal 账号已激活，计算环境${environmentLabel}`}
        action={<StatusBadge value="NOT PROVISIONED" />}
      />
      <div className="grid-compact ordinary-stats">
        <Card className="stat-panel">
          <div className="stat-label">账号</div>
          <div className="stat-value compact-stat">
            <StatusBadge value={user.account_state} />
          </div>
          <div className="stat-detail">Portal 登录身份已建立</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">计算环境</div>
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
                ? "管理员正在执行受控 Stage；Lease 尚未开始"
                : "资源尚未执行 Provision，Lease 尚未开始"
              : pending
                ? "等待管理员审批，尚未创建任何资源"
                : failed
                  ? "创建失败；原申请保留，Lease 未启动"
                  : "尚未创建 Linux、Container 或 Lease"}
          </div>
        </Card>
      </div>
      {query.isError ? (
        <ErrorBlock message="计算资源申请状态暂时不可用；未将其显示为未申请。" />
      ) : null}
      <div className="section-grid">
        <SectionCard
          title="申请计算资源"
          subtitle="用户申请 → 管理员审批 → 资源规划"
        >
          {pending && computeRequest ? (
            <>
              <p>
                状态：等待管理员审批 · GPU {computeRequest.requested_gpu_max} ·
                Storage 300GB · Lease 4天
              </p>
              <div className="button-row">
                <Link
                  className="ui-button ui-button-primary"
                  href="/compute-request"
                >
                  查看申请
                </Link>
                {computeRequest.status === "REQUESTED" ? (
                  <Button
                    disabled={cancel.isPending}
                    onClick={() => cancel.mutate(computeRequest.id)}
                  >
                    撤回申请
                  </Button>
                ) : null}
              </div>
            </>
          ) : failed && computeRequest ? (
            <>
              <div className="notice">
                {computeRequest.user_status_message ??
                  "计算环境创建失败，平台管理员正在处理。你的申请仍被保留，无需重新提交。"}
              </div>
              <Link
                className="ui-button ui-button-primary"
                href="/compute-request"
              >
                查看处理状态
              </Link>
            </>
          ) : approved && computeRequest ? (
            <>
              <p>
                {provisioning
                  ? "申请已批准，受控 Stage 正在进行。Lease 仍未启动。"
                  : "申请已批准，正在等待创建。当前仍没有 Container、Quota、Slurm Association 或 Lease。"}
              </p>
              <Link
                className="ui-button ui-button-primary"
                href="/compute-request"
              >
                查看批准状态
              </Link>
            </>
          ) : (
            <>
              {computeRequest?.status === "REJECTED" ? (
                <div className="notice">
                  上次申请未通过：{computeRequest.review_note ?? "未提供原因"}
                </div>
              ) : null}
              <p>
                申请标准开发环境；提交申请不会自动创建服务器资源，也不会开始
                Lease。
              </p>
              <Link
                className="ui-button ui-button-primary"
                href="/compute-request"
              >
                申请计算资源
              </Link>
            </>
          )}
          {cancel.isError ? (
            <div className="form-error">
              {cancel.error instanceof ApiError
                ? cancel.error.message
                : "申请未能撤回"}
            </div>
          ) : null}
        </SectionCard>
        <SectionCard title="设置SSH密钥" subtitle="用于未来自己的开发容器">
          <p>计算身份获批后，可登记 SSH 公钥；平台不会要求上传私钥。</p>
          <Link className="ui-button" href="/ssh-keys">
            设置SSH密钥
          </Link>
        </SectionCard>
      </div>
      <div className="section-grid">
        <SectionCard title="账号与安全">
          <Link className="table-link" href="/account/security">
            修改 Portal 密码与管理会话
          </Link>
        </SectionCard>
        <SectionCard title="帮助">
          <Link className="table-link" href="/help">
            查看开户与资源申请说明
          </Link>
        </SectionCard>
      </div>
    </>
  );
}

export function OrdinaryStagedDashboard({ user }: { user: User }) {
  return (
    <>
      <PageHeading
        title="我的环境"
        description="计算环境已准备，下一步设置 SSH 密钥"
        action={<StatusBadge value="KEY ENROLLMENT PENDING" />}
      />
      <div className="grid-compact ordinary-stats">
        <Card className="stat-panel">
          <div className="stat-label">Portal 账号</div>
          <div className="stat-value compact-stat">
            <StatusBadge value={user.account_state} />
          </div>
          <div className="stat-detail">登录身份保持 ACTIVE</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">计算环境</div>
          <div className="stat-value compact-stat">
            <StatusBadge value="STAGED" />
          </div>
          <div className="stat-detail">Container 已创建但保持停止</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">Lease</div>
          <div className="stat-value compact-stat">
            <StatusBadge value="NOT STARTED" />
          </div>
          <div className="stat-detail">96小时倒计时尚未启动</div>
        </Card>
      </div>
      <SectionCard
        title="设置 Container SSH 密钥"
        subtitle="Scope 固定为 CONTAINER；私钥只保存在你的电脑"
      >
        <p>
          计算资源已经安全 Stage。登记你自己的 ED25519
          公钥后，管理员才能在下一阶段激活环境。
        </p>
        <Link className="ui-button ui-button-primary" href="/ssh-keys">
          设置 SSH 密钥
        </Link>
      </SectionCard>
      <div className="notice">
        当前不能启动容器、打开网页终端或提交作业；Host SSH 始终禁用。
      </div>
    </>
  );
}

function LeaseAction({ lease }: { lease: ComputeLease }) {
  const queryClient = useQueryClient();
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
        申请恢复
      </Link>
    );
  }
  if (lease.pending_renewal_id) {
    return <StatusBadge value="续期申请待审批" />;
  }
  return (
    <div>
      <Button
        tone="primary"
        disabled={!lease.renewal_available || mutation.isPending}
        onClick={() => mutation.mutate()}
      >
        申请续期
      </Button>
      {!lease.renewal_available ? (
        <div className="muted compact-help">将在到期前24小时开放续期</div>
      ) : null}
      {mutation.isError ? (
        <div className="form-error">
          {mutation.error instanceof ApiError
            ? mutation.error.message
            : "续期申请未提交"}
        </div>
      ) : null}
    </div>
  );
}

export function OrdinaryDashboard() {
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
  if (query.isError) return <ErrorBlock message="计算环境状态暂时不可用。" />;
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
            {lease.active ? "当前计算资源有效至" : "计算资源已于以下时间到期"}
          </div>
          <div className="lease-expiry">{localTime(lease.expires_at)}</div>
          <div className="muted">
            剩余 {remaining(lease.remaining_seconds)} · 每次最多续期4天
          </div>
        </div>
        <LeaseAction lease={lease} />
      </section>
      <div className="grid-compact ordinary-stats">
        <Card className="stat-panel">
          <div className="stat-label">计算环境</div>
          <div className="stat-value compact-stat">
            <StatusBadge value={environment.state} />
          </div>
          <div className="stat-detail">Host SSH 已按普通用户策略关闭</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">开发容器</div>
          <div className="stat-value compact-stat">
            <StatusBadge value={environment.container.state} />
          </div>
          <div className="stat-detail">用于开发，不直接提供GPU</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">GPU任务上限</div>
          <div className="stat-value">{environment.gpu_max}</div>
          <div className="stat-detail">通过作业页面提交</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">可用空间</div>
          <div className="stat-value small-value">{bytes(available)}</div>
          <div className="stat-detail">
            总配额 {bytes(environment.storage.quota_bytes)}
          </div>
        </Card>
      </div>
      <div className="section-grid">
        <SectionCard title="开始开发" subtitle="进入自己的长期开发容器">
          <p>在开发容器中编写代码、编译和准备数据。容器不直接分配GPU。</p>
          <div className="button-row">
            <Link className="ui-button ui-button-primary" href="/terminal">
              打开网页终端
            </Link>
            <Link className="ui-button" href="/access">
              查看SSH连接
            </Link>
          </div>
        </SectionCard>
        <SectionCard title="提交计算任务" subtitle="CPU 或最多1张GPU">
          <p>选择工作区中的脚本，通过Portal提交到Slurm。</p>
          <Link className="ui-button ui-button-primary" href="/jobs">
            新建作业
          </Link>
        </SectionCard>
      </div>
    </>
  );
}

export function OrdinaryConnection() {
  const query = useQuery({
    queryKey: ["self-container-connection"],
    queryFn: selfContainerConnection,
    refetchInterval: 30_000,
  });
  const [message, setMessage] = useState<string | null>(null);
  if (query.isPending) return <LoadingBlock />;
  if (query.isError)
    return <ErrorBlock message="开发容器连接信息暂时不可用。" />;
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
            <dt>认证</dt>
            <dd>SSH Public Key</dd>
          </div>
          <div className="kv">
            <dt>GPU</dt>
            <dd>NONE</dd>
          </div>
          <div className="kv">
            <dt>状态</dt>
            <dd>{connection.available ? "AVAILABLE" : "DISABLED"}</dd>
          </div>
          <div className="kv access-key-row">
            <dt>用户密钥 Fingerprint</dt>
            <dd className="mono ssh-fingerprint-value">
              {connection.key_fingerprint ?? "未安装"}
            </dd>
          </div>
        </dl>
        {connection.command ? (
          <div className="connection-output">
            <div className="field-label">SSH 命令</div>
            <pre className="connection-command">{connection.command}</pre>
            <div className="button-row">
              <Button
                onClick={() =>
                  void copyText(connection.command ?? "").then(() =>
                    setMessage("SSH命令已复制"),
                  )
                }
              >
                复制命令
              </Button>
              <Button
                onClick={() =>
                  void copyText(connection.vscode ?? "").then(() =>
                    setMessage("VS Code配置已复制"),
                  )
                }
              >
                复制 VS Code 配置
              </Button>
            </div>
          </div>
        ) : (
          <div className="notice">租约、容器或SSH公钥当前不可用。</div>
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
  if (query.isError) return <ErrorBlock message="开发容器状态暂时不可用。" />;
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
            <dt>用途</dt>
            <dd>开发与数据准备</dd>
          </div>
        </dl>
        <div className="button-row access-actions">
          <Link
            className={`ui-button ui-button-primary ${container.state !== "RUNNING" ? "link-disabled" : ""}`}
            href={container.state === "RUNNING" ? "/terminal" : "#"}
            aria-disabled={container.state !== "RUNNING"}
          >
            网页终端
          </Link>
          <Button
            disabled={container.state === "RUNNING" || mutation.isPending}
            onClick={() => mutation.mutate("start")}
          >
            启动
          </Button>
          <Button
            disabled={container.state !== "RUNNING" || mutation.isPending}
            onClick={() => mutation.mutate("restart")}
          >
            重启
          </Button>
          <Button
            disabled={container.state !== "RUNNING" || mutation.isPending}
            onClick={() => mutation.mutate("stop")}
          >
            停止
          </Button>
        </div>
        {mutation.isError ? (
          <div className="error-box">
            {mutation.error instanceof Error
              ? mutation.error.message
              : "容器操作被拒绝"}
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
  if (!jobs.length)
    return (
      <EmptyState title="还没有Portal作业" detail="从上方创建CPU或单GPU作业" />
    );
  return (
    <div className="ui-table-wrap">
      <table className="ui-table">
        <thead>
          <tr>
            <th>Job</th>
            <th>名称</th>
            <th>状态</th>
            <th>GPU</th>
            <th>时限</th>
            <th>ExitCode</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id}>
              <td>{job.slurm_job_id ?? "提交中"}</td>
              <td>{job.name}</td>
              <td>
                <StatusBadge value={job.state} />
              </td>
              <td>{job.gpu_count}</td>
              <td>{Math.ceil(job.time_limit_seconds / 60)} 分钟</td>
              <td>{job.exit_code ?? "—"}</td>
              <td>
                <Button onClick={() => onSelect(job)}>日志</Button>
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
  const query = useQuery({
    queryKey: ["self-jobs"],
    queryFn: selfJobs,
    refetchInterval: 10_000,
  });
  const [name, setName] = useState("training-job");
  const [script, setScript] = useState("workspace/job.sh");
  const [workdir, setWorkdir] = useState("workspace");
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
        script_path: script,
        workdir,
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
        subtitle="脚本和工作目录必须位于自己的工作区"
      >
        <form className="job-form-grid" onSubmit={onSubmit}>
          <label>
            作业名称
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label>
            脚本路径
            <Input value={script} onChange={(e) => setScript(e.target.value)} />
          </label>
          <label>
            工作目录
            <Input
              value={workdir}
              onChange={(e) => setWorkdir(e.target.value)}
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
            内存 MB
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
            最长运行（分钟）
            <Input
              type="number"
              min={1}
              max={5760}
              value={minutes}
              onChange={(e) => setMinutes(Number(e.target.value))}
            />
          </label>
          <label>
            运行环境
            <select
              className="ui-input"
              value={containerized ? "approved" : "host"}
              onChange={(e) => setContainerized(e.target.value === "approved")}
            >
              <option value="host">标准 Slurm</option>
              <option value="approved">已批准 CUDA 容器</option>
            </select>
          </label>
          <div className="job-submit-row">
            <Button tone="primary" type="submit" disabled={submit.isPending}>
              提交作业
            </Button>
          </div>
        </form>
        {submit.isError ? (
          <div className="error-box">
            {submit.error instanceof ApiError
              ? `${submit.error.code}：${submit.error.message}`
              : "作业提交失败"}
          </div>
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
          title={`${selected.name} · 日志`}
          action={<Button onClick={() => setSelected(null)}>关闭</Button>}
        >
          <div className="button-row">
            <StatusBadge value={selected.state} />
            {!["COMPLETED", "FAILED", "CANCELLED"].includes(selected.state) ? (
              <Button
                tone="danger"
                disabled={cancel.isPending}
                onClick={() => cancel.mutate(selected.id)}
              >
                取消作业
              </Button>
            ) : null}
          </div>
          <h3>stdout</h3>
          <pre className="job-log">{logs.data?.stdout || "暂无输出"}</pre>
          <h3>stderr</h3>
          <pre className="job-log">{logs.data?.stderr || "暂无错误输出"}</pre>
        </SectionCard>
      ) : null}
    </>
  );
}

export function OrdinaryStorage() {
  const query = useQuery({
    queryKey: ["self-storage"],
    queryFn: selfStorage,
    refetchInterval: 60_000,
  });
  if (query.isPending) return <LoadingBlock />;
  if (query.isError) return <ErrorBlock message="存储状态暂时不可用。" />;
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
            <dt>总配额</dt>
            <dd>{bytes(storage.quota_bytes)}</dd>
          </div>
          <div className="kv">
            <dt>已使用</dt>
            <dd>{bytes(storage.used_bytes)}</dd>
          </div>
          <div className="kv">
            <dt>可用</dt>
            <dd>{bytes(storage.available_bytes)}</dd>
          </div>
          <div className="kv">
            <dt>隔离</dt>
            <dd>PRIVATE</dd>
          </div>
        </dl>
        <div className="notice">
          其他普通用户不能读取、列出或写入此目录。共享数据使用单独批准的数据集。
        </div>
      </SectionCard>
    </>
  );
}

export function OrdinaryRecycleBin() {
  const queryClient = useQueryClient();
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
  if (query.isError) return <ErrorBlock message="回收站暂时不可用。" />;
  return (
    <>
      <PageHeading
        title="回收站"
        description="过期资源可申请恢复，数据不会立即删除"
      />
      {!query.data.items.length ? (
        <Card>
          <EmptyState title="回收站为空" detail="当前没有过期的计算资源" />
        </Card>
      ) : (
        query.data.items.map((item) => (
          <SectionCard
            key={item.id}
            title={item.resource_name}
            action={
              <StatusBadge
                value={
                  item.state === "RESTORE_PENDING" ? "恢复申请待审批" : "已过期"
                }
              />
            }
          >
            <dl className="kv-grid">
              <div className="kv">
                <dt>租约到期</dt>
                <dd>{localTime(item.expires_at)}</dd>
              </div>
              <div className="kv">
                <dt>进入回收站</dt>
                <dd>{localTime(item.recycled_at)}</dd>
              </div>
              <div className="kv">
                <dt>开发容器</dt>
                <dd>已停止</dd>
              </div>
              <div className="kv">
                <dt>数据</dt>
                <dd>已保留</dd>
              </div>
            </dl>
            <div className="button-row access-actions">
              <Button
                tone="primary"
                disabled={restore.isPending || item.state !== "RECYCLE_BIN"}
                onClick={() => restore.mutate(item.id)}
              >
                申请恢复
              </Button>
            </div>
          </SectionCard>
        ))
      )}
    </>
  );
}

export function OrdinarySshKeys() {
  const current = useQuery({ queryKey: ["me"], queryFn: me });
  if (current.isPending) return <LoadingBlock />;
  if (current.isError) return <ErrorBlock />;
  if (current.data.user.resource_onboarding_state === "NOT_ENROLLED") {
    return (
      <>
        <PageHeading title="SSH密钥" description="用于未来自己的开发容器" />
        <SectionCard title="尚未建立计算身份">
          <p>
            SSH
            公钥登记将在计算资源申请获批后开放。平台只接收公钥，绝不会要求上传私钥。
          </p>
          <Button disabled>设置SSH密钥</Button>
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
          managedUserId={current.data.ssh_enrollment.managed_user_id}
          containerOnly
        />
      </SectionCard>
    </>
  );
}

export function OrdinaryHelp() {
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
              Portal 账号已激活，但尚未创建 Linux 用户、开发容器、配额、Slurm 或
              Lease。
            </p>
          </SectionCard>
          <SectionCard title="下一步">
            <p>下一阶段通过资源申请与管理员审批进入 Compute Provisioning。</p>
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
              计算身份、私有存储、Slurm association 与无 GPU 开发容器已经安全
              Stage；Container 保持停止，Lease 尚未启动。
            </p>
          </SectionCard>
          <SectionCard title="下一步">
            <p>只登记你自己的 ED25519 公钥，Scope 固定为 CONTAINER。</p>
            <Link className="table-link" href="/ssh-keys">
              设置 SSH 密钥
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
          <p>用于编写代码和开发，不直接提供GPU。</p>
          <Link className="table-link" href="/access">
            查看连接信息
          </Link>
        </SectionCard>
        <SectionCard title="GPU任务">
          <p>通过作业页面提交，当前最多使用1张GPU。</p>
          <Link className="table-link" href="/jobs">
            进入作业页面
          </Link>
        </SectionCard>
      </div>
      <SectionCard title="租约与恢复">
        <p>
          到期前24小时可申请续期，每次最多4天。资源到期后开发容器会停止并进入回收站，数据不会立即删除；批准恢复后可继续使用。
        </p>
      </SectionCard>
    </>
  );
}
