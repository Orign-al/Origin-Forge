"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { Button, Card, EmptyState, Input, StatusBadge } from "@h100-portal/ui";
import {
  ApiError,
  cancelSelfJob,
  me,
  requestLeaseRenewal,
  requestRestore,
  selfContainer,
  selfContainerAction,
  selfContainerConnection,
  selfEnvironment,
  selfJobLogs,
  selfJobs,
  selfRecycleBin,
  selfStorage,
  submitSelfJob,
  type ComputeLease,
  type SelfJob,
} from "../lib/api";
import { copyText } from "../lib/ssh-key";
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

function LeaseAction({ lease }: { lease: ComputeLease }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () =>
      requestLeaseRenewal({
        duration_seconds: 345600,
        idempotency_key: crypto.randomUUID(),
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
          <Link className="ui-button ui-button-primary" href="/access">
            查看容器连接
          </Link>
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
          <Button
            tone="primary"
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
        idempotency_key: crypto.randomUUID(),
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
  return (
    <>
      <PageHeading title="SSH密钥" description="仅用于自己的开发容器" />
      <SectionCard title="容器公钥">
        <SshKeyEnrollment
          userId={current.data.user.id}
          username="origin-pilot"
          computeState="ACTIVE"
          managedUserId={current.data.ssh_enrollment.managed_user_id}
          containerOnly
        />
      </SectionCard>
    </>
  );
}

export function OrdinaryHelp() {
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
