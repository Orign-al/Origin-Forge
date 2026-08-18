"use client";

import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { ACCESS_MODE_STATUS, TRANSPORT_TLS_STATUS } from "@h100-portal/config";
import { Button, Card, EmptyState, Input, StatusBadge } from "@h100-portal/ui";
import {
  ApiError,
  alerts,
  approveOperation,
  audit,
  containers,
  createOperation,
  gpus,
  gpuHealth,
  imageInventory,
  monitoring,
  operations,
  productionPilot,
  quotas,
  registries,
  slurmAccounts,
  slurmHistory,
  slurmJobs,
  slurmNodes,
  storage,
  submitOperation,
} from "../lib/api";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
  StaleNotice,
  UnauthorizedBlock,
} from "./PortalShell";

type Row = Record<string, unknown>;

function rowsAt(payload: unknown, key: string): Row[] {
  if (!payload || typeof payload !== "object") return [];
  const root = payload as Row;
  const direct = root[key];
  if (Array.isArray(direct)) {
    return direct.filter(
      (item): item is Row => !!item && typeof item === "object",
    );
  }
  const nested = root.data;
  if (nested && typeof nested === "object") {
    const value = (nested as Row)[key];
    if (Array.isArray(value)) {
      return value.filter(
        (item): item is Row => !!item && typeof item === "object",
      );
    }
  }
  return [];
}

function objectRows(value: unknown, keyName = "name"): Row[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>).map(([key, item]) =>
    item && typeof item === "object" && !Array.isArray(item)
      ? { [keyName]: key, ...(item as Row) }
      : { [keyName]: key, value: item },
  );
}

function scalar(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function Table({
  rows,
  columns,
  empty = "暂无数据",
}: {
  rows: Row[];
  columns: Array<[string, string]>;
  empty?: string;
}) {
  if (!rows.length) {
    return (
      <EmptyState
        title={empty}
        detail="数据源没有返回记录；UNKNOWN 不会被折叠为正常"
      />
    );
  }
  return (
    <div className="ui-table-wrap">
      <table className="ui-table">
        <thead>
          <tr>
            {columns.map(([key, label]) => (
              <th key={key}>{label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 300).map((row, index) => {
            const key = scalar(
              row.id ?? row.job_id ?? row.uuid ?? row.name ?? index,
            );
            return (
              <tr key={key}>
                {columns.map(([column]) => {
                  const value = row[column];
                  const isStatus =
                    column.toLowerCase().includes("state") ||
                    column.toLowerCase().includes("status") ||
                    column === "result" ||
                    column === "health";
                  return (
                    <td key={column}>
                      {column === "Names" && typeof value === "string" ? (
                        <Link
                          className="table-link"
                          href={`/containers/${encodeURIComponent(value)}`}
                        >
                          {value}
                        </Link>
                      ) : isStatus ? (
                        <StatusBadge value={scalar(value, "UNKNOWN")} />
                      ) : value === null ||
                        value === undefined ||
                        value === "" ? (
                        <span className="muted">—</span>
                      ) : (
                        <span
                          className={
                            typeof value === "object" ? "mono" : undefined
                          }
                        >
                          {typeof value === "object"
                            ? JSON.stringify(value)
                            : String(value)}
                        </span>
                      )}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ModuleFrame({
  title,
  description,
  action,
  query,
  children,
}: {
  title: string;
  description: string;
  action?: React.ReactNode;
  query: {
    isPending: boolean;
    isError: boolean;
    isFetching: boolean;
    data?: unknown;
    error?: unknown;
  };
  children: React.ReactNode;
}) {
  if (query.isPending) {
    return (
      <>
        <PageHeading title={title} description={description} action={action} />
        <LoadingBlock />
      </>
    );
  }
  if (
    query.isError &&
    query.error instanceof ApiError &&
    query.error.status === 403
  ) {
    return (
      <>
        <PageHeading title={title} description={description} action={action} />
        <UnauthorizedBlock />
      </>
    );
  }
  if (query.isError || !query.data) {
    return (
      <>
        <PageHeading title={title} description={description} action={action} />
        <ErrorBlock />
      </>
    );
  }
  return (
    <>
      <PageHeading title={title} description={description} action={action} />
      {query.isFetching ? <StaleNotice /> : null}
      {children}
    </>
  );
}

export function ContainersModule() {
  const query = useQuery({
    queryKey: ["containers"],
    queryFn: containers,
    refetchInterval: 30_000,
  });
  const rows = rowsAt(query.data, "containers");
  return (
    <ModuleFrame
      title="容器"
      description="长期开发容器的真实状态与安全属性"
      query={query}
    >
      <Card>
        <Table
          rows={rows}
          columns={[
            ["Names", "名称"],
            ["owner", "用户"],
            ["Status", "状态"],
            ["Image", "镜像"],
            ["image_digest", "镜像 ID / digest"],
            ["Ports", "SSH 端口"],
            ["safe_labels", "受控标签"],
          ]}
          empty="当前没有容器记录"
        />
      </Card>
      <div className="section-grid">
        <SectionCard title="平台约束" subtitle="所有字段来自固定 Worker 白名单">
          <dl className="kv-grid">
            <div className="kv">
              <dt>GPU</dt>
              <dd>无（页面不提供 GPU 开关）</dd>
            </div>
            <div className="kv">
              <dt>Privileged</dt>
              <dd>否（不允许改写）</dd>
            </div>
            <div className="kv">
              <dt>Docker Socket</dt>
              <dd>未挂载（固定校验）</dd>
            </div>
            <div className="kv">
              <dt>Host Network</dt>
              <dd>否（固定校验）</dd>
            </div>
          </dl>
        </SectionCard>
        <SectionCard title="操作边界">
          <div className="notice">
            启动、停止、重建均先生成任务并等待审批；本阶段只执行 Worker
            dry-run。
          </div>
        </SectionCard>
      </div>
    </ModuleFrame>
  );
}

export function SlurmModule() {
  const query = useQuery({
    queryKey: ["slurm-summary"],
    queryFn: async () => {
      const [nodes, accounts, history, production] = await Promise.all([
        slurmNodes(),
        slurmAccounts(),
        slurmHistory(),
        productionPilot(),
      ]);
      return { nodes, accounts, history, production };
    },
    refetchInterval: 30_000,
  });
  const nodeRows = rowsAt(query.data?.nodes, "nodes");
  const accountRows = rowsAt(query.data?.accounts, "accounts");
  const qosRows = rowsAt(query.data?.accounts, "qos");
  const assocRows = rowsAt(query.data?.accounts, "associations");
  const historyRows = rowsAt(query.data?.history, "jobs");
  const production = query.data?.production ?? {};
  return (
    <ModuleFrame
      title="Slurm"
      description="节点、GRES、作业历史、Accounting、QOS 和 Association"
      query={query}
    >
      <Card>
        <Table
          rows={nodeRows}
          columns={[
            ["name", "节点"],
            ["state", "状态"],
            ["reason", "DRAIN Reason"],
            ["partitions", "分区"],
            ["cpus", "CPU"],
            ["real_memory", "RealMemory MiB"],
            ["gres", "GRES"],
            ["gres_used", "GRES Used"],
            ["alloc_tres", "AllocTRES"],
          ]}
          empty="Slurm 节点数据不可用"
        />
      </Card>
      <div className="section-grid">
        <SectionCard
          title="Production Pilot"
          subtitle="固定单节点、单受管用户边界"
        >
          <dl className="kv-grid">
            <div className="kv">
              <dt>状态</dt>
              <dd>
                <StatusBadge value={scalar(production.state, "UNKNOWN")} />
              </dd>
            </div>
            <div className="kv">
              <dt>Scheduler</dt>
              <dd>
                <StatusBadge value={scalar(production.scheduler, "UNKNOWN")} />
              </dd>
            </div>
            <div className="kv">
              <dt>受管用户</dt>
              <dd>{scalar(production.active_managed_user)}</dd>
            </div>
            <div className="kv">
              <dt>每用户最大 GPU</dt>
              <dd>{scalar(production.per_user_max_gpu)}</dd>
            </div>
            <div className="kv">
              <dt>队列</dt>
              <dd>{scalar(production.queue)}</dd>
            </div>
          </dl>
          <div className="notice">
            GPU 仅通过 Slurm / Pyxis / Enroot 分配；长期开发容器保持 GPU NONE。
          </div>
        </SectionCard>
        <SectionCard title="Accounting / QOS">
          <Table
            rows={accountRows}
            columns={[
              ["account", "Account"],
              ["description", "说明"],
            ]}
            empty="无 Account 记录"
          />
          <div className="subsection-title">QOS</div>
          <Table
            rows={qosRows}
            columns={[
              ["name", "名称"],
              ["priority", "优先级"],
              ["max_tres_per_user", "MaxTRES/User"],
            ]}
            empty="无 QOS 记录"
          />
        </SectionCard>
      </div>
      <SectionCard title="Association" subtitle="结构化 sacctmgr 输出">
        <Table
          rows={assocRows}
          columns={[
            ["cluster", "Cluster"],
            ["account", "Account"],
            ["user", "User"],
            ["qos", "QOS"],
            ["default_qos", "Default QOS"],
          ]}
          empty="无 Association 记录"
        />
      </SectionCard>
      <SectionCard
        title="最近 7 天作业历史"
        subtitle="sacct -P；队列为空不代表历史为空"
      >
        <Table
          rows={historyRows}
          columns={[
            ["job_id", "Job ID"],
            ["name", "名称"],
            ["user", "用户"],
            ["partition", "分区"],
            ["state", "状态"],
            ["elapsed", "Elapsed"],
            ["alloc_tres", "AllocTRES"],
            ["exit_code", "ExitCode"],
          ]}
          empty="没有最近作业历史"
        />
      </SectionCard>
    </ModuleFrame>
  );
}

export function JobsModule() {
  const query = useQuery({
    queryKey: ["slurm-jobs-history"],
    queryFn: async () => {
      const [current, history] = await Promise.all([
        slurmJobs(),
        slurmHistory(),
      ]);
      return { current, history };
    },
    refetchInterval: 15_000,
  });
  return (
    <ModuleFrame
      title="作业"
      description="当前队列与最近完成/失败作业"
      query={query}
    >
      <Card>
        <Table
          rows={rowsAt(query.data?.current, "jobs")}
          columns={[
            ["job_id", "Job ID"],
            ["user", "用户"],
            ["state", "状态"],
            ["partition", "分区"],
            ["name", "名称"],
            ["reason", "原因"],
            ["nodes", "节点数"],
          ]}
          empty="当前队列为空"
        />
      </Card>
      <SectionCard title="作业历史">
        <Table
          rows={rowsAt(query.data?.history, "jobs")}
          columns={[
            ["job_id", "Job ID"],
            ["name", "名称"],
            ["user", "用户"],
            ["state", "状态"],
            ["elapsed", "Elapsed"],
            ["exit_code", "ExitCode"],
          ]}
          empty="没有历史记录"
        />
      </SectionCard>
    </ModuleFrame>
  );
}

export function GpusModule() {
  const query = useQuery({
    queryKey: ["gpus"],
    queryFn: gpus,
    refetchInterval: 20_000,
  });
  const health = useQuery({
    queryKey: ["gpu-health"],
    queryFn: gpuHealth,
    refetchInterval: 120_000,
  });
  const rows = rowsAt(query.data, "gpus");
  return (
    <ModuleFrame
      title="GPU"
      description="物理设备身份与健康；NVML index 不等于 Linux minor"
      query={query}
    >
      <Card>
        <Table
          rows={rows}
          columns={[
            ["index", "NVML index"],
            ["uuid", "UUID"],
            ["pci.bus_id", "PCI Bus ID"],
            ["minor_number", "Linux minor"],
            ["device_path", "/dev/nvidia"],
            ["name", "型号"],
            ["memory.total", "总显存 MiB"],
            ["memory.used", "已用 MiB"],
            ["utilization.gpu", "利用率"],
            ["temperature.gpu", "温度 °C"],
            ["power.draw", "功耗 W"],
            ["ecc.errors.uncorrected.volatile.total", "ECC"],
            ["pcie.link.gen.current", "PCIe Gen"],
            ["pcie.link.width.current", "PCIe Width"],
            ["mig.mode.current", "MIG"],
            ["dcgm_status", "DCGM"],
            ["xid_aer_status", "Xid/AER"],
            ["active_slurm_job_ids", "Slurm Job"],
          ]}
          empty="GPU 数据不可用"
        />
      </Card>
      <div className="notice">
        MIG Disabled。Portal 不提供 MIG 创建、GPU reset 或 Compute Mode 修改。
      </div>
      <div className="section-grid">
        <SectionCard title="DCGM Level 1" subtitle="独立健康采样">
          <StatusBadge
            value={
              health.isPending
                ? "PENDING"
                : health.isError
                  ? "UNKNOWN"
                  : scalar(health.data?.status, "UNKNOWN")
            }
          />
        </SectionCard>
        <SectionCard
          title="Xid / AER"
          subtitle="内核日志查询失败不会显示为 CLEAR"
        >
          <StatusBadge
            value={scalar(
              (health.data?.kernel_errors as Row | undefined)?.status,
              "UNKNOWN",
            )}
          />
        </SectionCard>
      </div>
    </ModuleFrame>
  );
}

export function StorageModule() {
  const query = useQuery({
    queryKey: ["storage"],
    queryFn: storage,
    refetchInterval: 60_000,
  });
  const quotaQuery = useQuery({
    queryKey: ["quotas"],
    queryFn: quotas,
    refetchInterval: 60_000,
  });
  const usage = objectRows((query.data as Row | undefined)?.path_usage);
  return (
    <ModuleFrame
      title="存储和配额"
      description="文件系统、VG、Docker、Enroot 与 XFS project quota"
      query={query}
    >
      <Card>
        <Table
          rows={rowsAt(query.data, "mounts")}
          columns={[
            ["target", "挂载点"],
            ["fstype", "类型"],
            ["size", "容量"],
            ["used", "已用"],
            ["avail", "可用"],
            ["percent", "使用率"],
          ]}
          empty="存储数据不可用"
        />
      </Card>
      <div className="section-grid">
        <SectionCard title="目录使用量" subtitle="固定目录采样，不接受任意路径">
          <Table
            rows={usage}
            columns={[
              ["path", "目录"],
              ["status", "状态"],
              ["bytes", "字节"],
            ]}
            empty="没有目录采样"
          />
          <div className="subsection-title">VG Free</div>
          <pre className="mono detail-pre">
            {JSON.stringify(query.data?.volume_groups ?? "UNKNOWN", null, 2)}
          </pre>
        </SectionCard>
        <SectionCard title="Docker 使用量" subtitle="docker system df">
          <Table
            rows={rowsAt(query.data, "docker_usage")}
            columns={[
              ["type", "类型"],
              ["totalcount", "总数"],
              ["active", "活动"],
              ["size", "大小"],
              ["reclaimable", "可回收"],
            ]}
            empty="Docker 使用量不可用"
          />
        </SectionCard>
      </div>
      <SectionCard
        title="XFS project quota"
        subtitle="只读；调整必须经过审批和 dry-run"
      >
        {quotaQuery.isPending ? (
          <LoadingBlock />
        ) : quotaQuery.isError ? (
          <ErrorBlock message="Quota 数据源不可用，未显示为 0。" />
        ) : (
          <>
            <Table
              rows={rowsAt(quotaQuery.data, "projects")}
              columns={[
                ["project_id", "Project ID"],
                ["name", "名称"],
                ["used_blocks", "Used blocks"],
                ["soft_blocks", "Soft"],
                ["hard_blocks", "Hard"],
              ]}
              empty="无 project quota 记录"
            />
            {typeof (quotaQuery.data as Row).report === "string" ? (
              <pre className="mono detail-pre" style={{ marginTop: 10 }}>
                {String((quotaQuery.data as Row).report)}
              </pre>
            ) : null}
          </>
        )}
      </SectionCard>
    </ModuleFrame>
  );
}

export function ImagesModule() {
  const query = useQuery({
    queryKey: ["image-inventory"],
    queryFn: async () => {
      const [registryData, inventoryData] = await Promise.all([
        registries(),
        imageInventory(),
      ]);
      return { registryData, inventoryData };
    },
    refetchInterval: 60_000,
  });
  return (
    <ModuleFrame
      title="镜像和 Registry"
      description="实时 Registry 探测与不可变 digest 库存"
      query={query}
    >
      <Card>
        <Table
          rows={rowsAt(query.data?.registryData, "registries")}
          columns={[
            ["name", "Registry"],
            ["status", "状态"],
            ["detail", "说明"],
            ["endpoint", "Endpoint"],
          ]}
          empty="Registry 数据不可用"
        />
      </Card>
      <div className="notice">
        Docker Hub：DEFERRED / RESTRICTED。镜像唯一身份必须使用 digest，不以 tag
        作为唯一标识。
      </div>
      <Card>
        <Table
          rows={rowsAt(query.data?.inventoryData, "images")}
          columns={[
            ["registry", "Registry"],
            ["repository", "Repository"],
            ["tag", "Tag"],
            ["digest", "Digest"],
            ["architecture", "架构"],
            ["os", "OS"],
            ["size", "大小"],
            ["containers", "容器数"],
            ["immutable", "不可变"],
          ]}
          empty="当前没有本地镜像库存记录"
        />
      </Card>
    </ModuleFrame>
  );
}

export function MonitoringModule() {
  const query = useQuery({
    queryKey: ["monitoring"],
    queryFn: monitoring,
    refetchInterval: 30_000,
  });
  const targets = rowsAt(
    (query.data as Row | undefined)?.prometheus,
    "targets",
  );
  const metrics = rowsAt(query.data, "metrics");
  const alertRows = rowsAt((query.data as Row | undefined)?.alerts, "alerts");
  return (
    <ModuleFrame
      title="监控和告警"
      description="Prometheus targets、GPU、Mellanox、Guard、Docker、Slurm 与磁盘"
      query={query}
    >
      <div className="section-grid">
        <SectionCard title="Prometheus Targets">
          <Table
            rows={targets}
            columns={[
              ["job", "Job"],
              ["instance", "Instance"],
              ["health", "Health"],
              ["last_error", "Last error"],
              ["last_scrape", "Last scrape"],
            ]}
            empty="Targets 不可用"
          />
        </SectionCard>
        <SectionCard title="Grafana 深度分析">
          <dl className="kv-grid">
            <div className="kv">
              <dt>Health</dt>
              <dd>
                <StatusBadge
                  value={scalar(
                    (query.data as Row | undefined)?.grafana &&
                      ((query.data as Row).grafana as Row).status,
                    "UNKNOWN",
                  )}
                />
              </dd>
            </div>
            <div className="kv">
              <dt>入口</dt>
              <dd>
                <a
                  className="table-link"
                  href="http://10.10.10.220:3000"
                  target="_blank"
                  rel="noreferrer"
                >
                  Grafana（私有网络）
                </a>
              </dd>
            </div>
          </dl>
        </SectionCard>
      </div>
      <SectionCard title="活跃告警">
        <Table
          rows={alertRows}
          columns={[
            ["name", "名称"],
            ["severity", "级别"],
            ["state", "状态"],
            ["summary", "摘要"],
            ["active_at", "触发时间"],
          ]}
          empty="当前没有活跃告警"
        />
      </SectionCard>
      <SectionCard title="指标摘要" subtitle="仅展示 Prometheus 实际返回的样本">
        <Table
          rows={metrics}
          columns={[
            ["metric", "指标"],
            ["labels", "标签"],
            ["value", "值"],
          ]}
          empty="指标数据不可用"
        />
      </SectionCard>
      <div className="section-grid">
        <SectionCard title="Mellanox">
          <div className="notice">
            NETWORK PHYSICAL P0：DEFERRED BY ADMINISTRATOR — RISK ACCEPTED FOR
            SINGLE-NODE PILOT
          </div>
          <Table
            rows={rowsAt((query.data as Row | undefined)?.mellanox, "metrics")}
            columns={[
              ["metric", "指标"],
              ["labels", "接口"],
              ["value", "值"],
            ]}
            empty="Mellanox 指标不可用"
          />
        </SectionCard>
        <SectionCard title="Guard / systemd">
          <div className="notice">
            无 Pilot 用户时 Guard timer 必须保持 disabled/inactive。
          </div>
          <Table
            rows={rowsAt((query.data as Row | undefined)?.guard, "metrics")}
            columns={[
              ["metric", "指标"],
              ["value", "值"],
            ]}
            empty="Guard 指标不可用"
          />
        </SectionCard>
      </div>
    </ModuleFrame>
  );
}

export function OperationsModule() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["operations"],
    queryFn: operations,
    refetchInterval: 10_000,
  });
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const rows = rowsAt(query.data, "operations");
  async function createPlan() {
    setBusy(true);
    setMessage(null);
    try {
      await createOperation({
        operation_type: "user.plan",
        target_type: "user",
        target_id: "origin-pilot",
        request_summary:
          "为 Origin-al 规划独立 origin-pilot 计算身份（仅 dry-run）",
        payload: { username: "origin-pilot" },
        idempotency_key: `portal3a-origin-pilot-plan-${Date.now()}`,
      });
      setMessage(
        "已创建 origin-pilot DRAFT；尚未创建用户、策略、quota、association 或容器。",
      );
      await queryClient.invalidateQueries({ queryKey: ["operations"] });
    } catch {
      setMessage("草稿创建失败；宿主状态未改变。");
    } finally {
      setBusy(false);
    }
  }
  async function submit(row: Row) {
    setBusy(true);
    try {
      await submitOperation(String(row.id), String(row.target_id));
      setMessage("草稿已提交审批；仍不会执行宿主写操作。");
      await queryClient.invalidateQueries({ queryKey: ["operations"] });
    } catch {
      setMessage("提交失败；请确认对象名和最近重新认证状态。");
    } finally {
      setBusy(false);
    }
  }
  async function approve(row: Row) {
    setBusy(true);
    try {
      await approveOperation(String(row.id), {
        decision: "APPROVE",
        confirmation: String(row.target_id),
        comment: "Portal-2 dry-run simulation",
      });
      setMessage("审批模拟已提交，Worker 仅执行 dry-run。");
      await queryClient.invalidateQueries({ queryKey: ["operations"] });
    } catch {
      setMessage("审批模拟失败；未执行宿主操作。");
    } finally {
      setBusy(false);
    }
  }
  return (
    <ModuleFrame
      title="审批任务"
      description="DRAFT → PENDING_APPROVAL → APPROVED → QUEUED → RUNNING；Worker 仅 dry-run"
      action={
        <Button tone="primary" onClick={createPlan} disabled={busy}>
          规划 origin-pilot 计算身份
        </Button>
      }
      query={query}
    >
      {message ? (
        <div className="notice" role="status">
          {message}
        </div>
      ) : null}
      <Card>
        <Table
          rows={rows}
          columns={[
            ["id", "任务 ID"],
            ["operation_type", "操作"],
            ["target_id", "对象"],
            ["risk_level", "风险"],
            ["status", "状态"],
            ["request_summary", "摘要"],
            ["result_summary", "结果"],
          ]}
          empty="暂无审批任务"
        />
        {rows.length ? (
          <div className="operation-actions">
            {rows.slice(0, 20).map((row) => (
              <div className="operation-action-row" key={String(row.id)}>
                <span className="mono">{String(row.id).slice(0, 12)}…</span>
                {row.status === "DRAFT" ? (
                  row.operation_type === "user.stage" ||
                  row.operation_type === "user.activate" ? (
                    <Button disabled>生命周期 Gate 未开放</Button>
                  ) : (
                    <Button onClick={() => void submit(row)} disabled={busy}>
                      提交审批
                    </Button>
                  )
                ) : null}
                {row.status === "PENDING_APPROVAL" ? (
                  <Button onClick={() => void approve(row)} disabled={busy}>
                    审批模拟
                  </Button>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}
      </Card>
      <div className="notice">
        Slurm RESUME、用户 Activate、GPU
        隔离和配额变更在本阶段均不触碰宿主状态。
      </div>
    </ModuleFrame>
  );
}

export function AuditModule() {
  const query = useQuery({
    queryKey: ["audit"],
    queryFn: audit,
    refetchInterval: 30_000,
  });
  const [filter, setFilter] = useState("");
  const all = rowsAt(query.data, "events");
  const rows = useMemo(
    () =>
      all.filter(
        (row) =>
          !filter ||
          JSON.stringify(row).toLowerCase().includes(filter.toLowerCase()),
      ),
    [all, filter],
  );
  return (
    <ModuleFrame
      title="审计日志"
      description="登录、token、session、页面访问、审批和 Worker 事件（敏感字段脱敏）"
      query={query}
    >
      <Card style={{ padding: 14 }}>
        <div className="filter-row">
          <Input
            aria-label="筛选审计"
            placeholder="事件、操作者或对象"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <span className="muted">{rows.length} 条</span>
        </div>
        <Table
          rows={rows}
          columns={[
            ["timestamp", "时间"],
            ["event_type", "事件"],
            ["actor", "操作者"],
            ["actor_role", "角色"],
            ["object_type", "对象类型"],
            ["object_id", "对象"],
            ["result", "结果"],
            ["safe_metadata", "安全元数据"],
          ]}
          empty="暂无审计事件"
        />
      </Card>
    </ModuleFrame>
  );
}

export function SystemModule() {
  const query = useQuery({
    queryKey: ["system-overview"],
    queryFn: async () => {
      const response = await fetch("/api/v1/platform/overview", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) throw new Error("system data unavailable");
      return response.json() as Promise<Row>;
    },
    refetchInterval: 30_000,
  });
  const node =
    rowsAt((query.data as Row | undefined)?.platform, "nodes")[0] ??
    ((query.data as Row | undefined)?.platform as Row | undefined)?.node;
  const sshPolicy = (query.data as Row | undefined)?.ssh_policy as
    Row | undefined;
  const globalSshPolicy = sshPolicy?.global_ssh_policy as Row | undefined;
  const managedSshPolicy = sshPolicy?.managed_compute_user_policy as
    Row | undefined;
  return (
    <ModuleFrame
      title="系统"
      description="Portal 服务边界、保护条件和数据源状态"
      query={query}
    >
      <div className="section-grid">
        <SectionCard title="部署边界">
          <dl className="kv-grid">
            <div className="kv">
              <dt>Web</dt>
              <dd className="mono">10.10.10.220:18080</dd>
            </div>
            <div className="kv">
              <dt>API</dt>
              <dd className="mono">127.0.0.1:18081</dd>
            </div>
            <div className="kv">
              <dt>数据库</dt>
              <dd>PostgreSQL Unix Socket</dd>
            </div>
            <div className="kv">
              <dt>访问模式</dt>
              <dd>{ACCESS_MODE_STATUS}</dd>
            </div>
            <div className="kv">
              <dt>Transport TLS</dt>
              <dd>{TRANSPORT_TLS_STATUS}</dd>
            </div>
            <div className="kv">
              <dt>写操作</dt>
              <dd>Worker dry-run · 等待 Portal-3 完整批准</dd>
            </div>
            <div className="kv">
              <dt>Slurm</dt>
              <dd>
                <StatusBadge
                  value={scalar(
                    (node as Row | undefined)?.state ??
                      (node as Row | undefined)?.State,
                    "UNKNOWN",
                  )}
                />
              </dd>
            </div>
            <div className="kv">
              <dt>MIG</dt>
              <dd>MIG Disabled</dd>
            </div>
          </dl>
        </SectionCard>
        <SectionCard title="当前风险">
          <div className="notice">Mellanox P0：Deferred by administrator</div>
          <div className="notice" style={{ marginTop: 8 }}>
            Docker Hub：Deferred / alternative registries required
          </div>
          <div className="notice" style={{ marginTop: 8 }}>
            PCI DOE：P1 observation；NFS / 第二节点未部署
          </div>
        </SectionCard>
        <SectionCard title="Global SSH Policy">
          <dl className="kv-grid">
            <div className="kv">
              <dt>Public Key Authentication</dt>
              <dd>
                {globalSshPolicy?.pubkey_authentication === true
                  ? "ENABLED"
                  : "DISABLED"}
              </dd>
            </div>
            <div className="kv">
              <dt>Global Password Authentication</dt>
              <dd>
                {globalSshPolicy?.password_authentication === true
                  ? "ENABLED"
                  : "DISABLED"}
              </dd>
            </div>
            <div className="kv">
              <dt>Authentication Methods</dt>
              <dd>{scalar(globalSshPolicy?.authentication_methods)}</dd>
            </div>
          </dl>
        </SectionCard>
        <SectionCard title="Managed Compute User Policy">
          <dl className="kv-grid">
            <div className="kv">
              <dt>状态</dt>
              <dd>
                <StatusBadge
                  value={scalar(managedSshPolicy?.status, "UNKNOWN")}
                />
              </dd>
            </div>
            <div className="kv">
              <dt>Managed Compute Users</dt>
              <dd>PUBLIC KEY ONLY</dd>
            </div>
            <div className="kv">
              <dt>Password Authentication</dt>
              <dd>
                {managedSshPolicy?.password_authentication === false
                  ? "DISABLED"
                  : "ENABLED"}
              </dd>
            </div>
            <div className="kv">
              <dt>Keyboard Interactive</dt>
              <dd>
                {managedSshPolicy?.keyboard_interactive_authentication === false
                  ? "DISABLED"
                  : "ENABLED"}
              </dd>
            </div>
            <div className="kv">
              <dt>Required Authentication</dt>
              <dd>PUBLICKEY</dd>
            </div>
          </dl>
        </SectionCard>
      </div>
      <SectionCard title="数据源状态">
        <Table
          rows={[
            {
              name: "platform",
              status: (query.data as Row | undefined)?.status,
            },
            {
              name: "GPU",
              status: (
                (query.data as Row | undefined)?.platform as Row | undefined
              )?.status,
            },
            {
              name: "containers",
              status: (
                (query.data as Row | undefined)?.containers as Row | undefined
              )?.status,
            },
            {
              name: "storage",
              status: (
                (query.data as Row | undefined)?.storage as Row | undefined
              )?.status,
            },
            {
              name: "monitoring",
              status: (
                (query.data as Row | undefined)?.monitoring as Row | undefined
              )?.status,
            },
          ]}
          columns={[
            ["name", "数据源"],
            ["status", "状态"],
          ]}
          empty="状态不可用"
        />
      </SectionCard>
    </ModuleFrame>
  );
}
