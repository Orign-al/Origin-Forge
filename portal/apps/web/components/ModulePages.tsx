"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { Card, EmptyState, StatusBadge } from "@h100-portal/ui";
import {
  PageHeading,
  LoadingBlock,
  ErrorBlock,
  SectionCard,
  StaleNotice,
  UnauthorizedBlock,
} from "./PortalShell";
import {
  ApiError,
  alerts,
  audit,
  containers,
  gpus,
  gpuHealth,
  imageInventory,
  operations,
  quotas,
  registries,
  slurmJobs,
  slurmNodes,
  storage,
} from "../lib/api";

function listAt(payload: unknown, key: string): Array<Record<string, unknown>> {
  if (!payload || typeof payload !== "object") return [];
  const root = payload as Record<string, unknown>;
  const direct = root[key];
  if (Array.isArray(direct))
    return direct.filter(
      (item): item is Record<string, unknown> =>
        !!item && typeof item === "object",
    );
  const nested = root.data;
  if (
    nested &&
    typeof nested === "object" &&
    Array.isArray((nested as Record<string, unknown>)[key])
  )
    return (nested as Record<string, unknown>)[key] as Array<
      Record<string, unknown>
    >;
  return [];
}

function JsonTable({
  rows,
  headers,
  empty,
}: {
  rows: Array<Record<string, unknown>>;
  headers: Array<[string, string]>;
  empty: string;
}) {
  if (!rows.length)
    return (
      <EmptyState
        title={empty}
        detail="数据源没有返回记录；UNKNOWN 不会被折叠为正常"
      />
    );
  return (
    <div className="ui-table-wrap">
      <table className="ui-table">
        <thead>
          <tr>
            {headers.map(([key, label]) => (
              <th key={key}>{label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 200).map((row, index) => (
            <tr
              key={String(
                row.id ?? row.job_id ?? row.uuid ?? row.name ?? index,
              )}
            >
              {headers.map(([key]) => (
                <td key={key}>
                  {key === "Names" && typeof row[key] === "string" ? (
                    <Link
                      className="table-link"
                      href={`/containers/${encodeURIComponent(row[key])}`}
                    >
                      {row[key]}
                    </Link>
                  ) : key.toLowerCase().includes("state") ||
                    key.toLowerCase().includes("status") ? (
                    <StatusBadge value={String(row[key] ?? "UNKNOWN")} />
                  ) : row[key] === undefined ||
                    row[key] === null ||
                    row[key] === "" ? (
                    <span className="muted">—</span>
                  ) : (
                    <span
                      className={typeof row[key] === "object" ? "mono" : ""}
                    >
                      {typeof row[key] === "object"
                        ? JSON.stringify(row[key])
                        : String(row[key])}
                    </span>
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ModuleFrame({
  title,
  description,
  query,
  children,
}: {
  title: string;
  description: string;
  query: {
    isPending: boolean;
    isError: boolean;
    isFetching: boolean;
    data?: unknown;
    error?: unknown;
  };
  children: React.ReactNode;
}) {
  if (query.isPending)
    return (
      <>
        <PageHeading title={title} description={description} />
        <LoadingBlock />
      </>
    );
  if (
    query.isError &&
    query.error instanceof ApiError &&
    query.error.status === 403
  )
    return (
      <>
        <PageHeading title={title} description={description} />
        <UnauthorizedBlock />
      </>
    );
  if (query.isError || !query.data)
    return (
      <>
        <PageHeading title={title} description={description} />
        <ErrorBlock />
      </>
    );
  return (
    <>
      <PageHeading title={title} description={description} />
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
  const rows = listAt(query.data, "containers");
  return (
    <ModuleFrame
      title="容器"
      description="长期开发容器只读总览；默认无 GPU、非 privileged、无 Docker Socket"
      query={query}
    >
      <Card>
        <JsonTable
          rows={rows}
          headers={[
            ["Names", "名称"],
            ["Status", "状态"],
            ["Image", "镜像"],
            ["Ports", "端口"],
            ["Labels", "标签"],
          ]}
          empty="当前没有容器记录"
        />
      </Card>
      <div className="section-grid">
        <SectionCard
          title="安全属性"
          subtitle="由固定 docker inspect 适配器返回"
        >
          <div className="notice">
            页面不提供任意 Docker 参数、宿主目录挂载、capability、privileged 或
            GPU 开关。详细 inspect 失败时显示 UNKNOWN。
          </div>
        </SectionCard>
        <SectionCard title="GPU" subtitle="长期容器默认值">
          <div className="kv">
            <dt>DeviceRequest</dt>
            <dd>无 GPU（需后续审批）</dd>
          </div>
        </SectionCard>
      </div>
    </ModuleFrame>
  );
}

export function SlurmModule() {
  const query = useQuery({
    queryKey: ["slurm-nodes"],
    queryFn: slurmNodes,
    refetchInterval: 30_000,
  });
  const rows = listAt(query.data, "nodes");
  return (
    <ModuleFrame
      title="Slurm 节点"
      description="节点、GRES、DRAIN reason 与资源分配"
      query={query}
    >
      <Card>
        <JsonTable
          rows={rows}
          headers={[
            ["name", "节点"],
            ["state", "状态"],
            ["reason", "DRAIN Reason"],
            ["cpus", "CPU"],
            ["real_memory", "RealMemory MiB"],
            ["gres", "GRES"],
            ["gres_used", "GRES Used"],
            ["alloc_tres", "AllocTRES"],
            ["partitions", "分区"],
          ]}
          empty="Slurm 节点数据不可用"
        />
      </Card>
      <div className="notice" style={{ marginTop: 14 }}>
        当前阶段禁止通过 Portal RESUME Slurm；节点保持 IDLE+DRAIN。
      </div>
    </ModuleFrame>
  );
}

export function JobsModule() {
  const query = useQuery({
    queryKey: ["slurm-jobs"],
    queryFn: slurmJobs,
    refetchInterval: 15_000,
  });
  const rows = listAt(query.data, "jobs");
  return (
    <ModuleFrame
      title="作业"
      description="运行、排队和最近作业的结构化数据"
      query={query}
    >
      <Card>
        <JsonTable
          rows={rows}
          headers={[
            ["job_id", "Job ID"],
            ["user", "用户"],
            ["state", "状态"],
            ["partition", "分区"],
            ["name", "名称"],
            ["reason", "原因"],
          ]}
          empty="队列为空或作业数据不可用"
        />
      </Card>
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
  const rows = (query.data?.gpus ?? []) as Array<Record<string, unknown>>;
  return (
    <ModuleFrame
      title="GPU"
      description="物理设备身份、健康与 PCIe 信息；NVML index 不等于 Linux minor"
      query={query}
    >
      <Card>
        <JsonTable
          rows={rows}
          headers={[
            ["index", "NVML index"],
            ["uuid", "UUID"],
            ["pci.bus_id", "PCI Bus ID"],
            ["minor_number", "Linux minor"],
            ["device_path", "设备节点"],
            ["name", "型号"],
            ["memory.total", "显存 MiB"],
            ["memory.used", "已用 MiB"],
            ["utilization.gpu", "利用率"],
            ["temperature.gpu", "温度"],
            ["power.draw", "功耗 W"],
            ["ecc.errors.uncorrected.volatile.total", "未纠正 ECC"],
            ["pcie.link.gen.current", "PCIe Gen"],
            ["pcie.link.width.current", "PCIe Width"],
            ["mig.mode.current", "MIG"],
          ]}
          empty="GPU 数据不可用"
        />
      </Card>
      <div className="notice" style={{ marginTop: 14 }}>
        MIG Disabled。Portal 不提供 MIG 创建、GPU reset 或 Compute Mode
        修改按钮。
      </div>
      <div className="section-grid">
        <SectionCard title="DCGM Level 1" subtitle="独立健康数据源">
          {health.isPending ? (
            <span className="muted">正在读取…</span>
          ) : health.isError ? (
            <StatusBadge value="UNKNOWN" />
          ) : (
            <StatusBadge value={String(health.data.status ?? "UNKNOWN")} />
          )}
        </SectionCard>
        <SectionCard title="Xid / AER" subtitle="不以缺失数据伪装为正常">
          <StatusBadge
            value={
              health.data && typeof health.data.kernel_errors === "object"
                ? String(
                    (health.data.kernel_errors as Record<string, unknown>)
                      .status ?? "UNKNOWN",
                  )
                : "UNKNOWN"
            }
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
  const rows = listAt(query.data, "mounts");
  return (
    <ModuleFrame
      title="存储和配额"
      description="文件系统、VG free 与 XFS project quota 摘要"
      query={query}
    >
      <Card>
        <JsonTable
          rows={rows}
          headers={[
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
        <SectionCard title="项目空间" subtitle="来自固定 vgs 适配器">
          <pre className="mono" style={{ whiteSpace: "pre-wrap", margin: 0 }}>
            {JSON.stringify(
              (query.data as Record<string, unknown>)?.volume_groups ??
                "UNKNOWN",
              null,
              2,
            )}
          </pre>
        </SectionCard>
        <SectionCard title="配额边界">
          {quotaQuery.isPending ? (
            <span className="muted">正在读取 project quota…</span>
          ) : quotaQuery.isError ? (
            <ErrorBlock message="Quota 数据源不可用，未显示为 0。" />
          ) : (
            <pre className="mono detail-pre">
              {String(
                (quotaQuery.data as Record<string, unknown>).report ??
                  "当前无 quota 记录",
              )}
            </pre>
          )}
          <div className="notice">
            配额调整必须经过任务、审批和 Worker dry-run；本阶段不执行实际变更。
          </div>
        </SectionCard>
      </div>
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
  const rows = listAt(query.data?.registryData, "registries");
  const images = listAt(query.data?.inventoryData, "images");
  return (
    <ModuleFrame
      title="镜像和 Registry"
      description="Registry 状态与不可变 digest 设计"
      query={query}
    >
      <Card>
        <JsonTable
          rows={rows}
          headers={[
            ["name", "Registry"],
            ["status", "状态"],
            ["detail", "说明"],
          ]}
          empty="Registry 数据不可用"
        />
      </Card>
      <div className="notice" style={{ marginTop: 14 }}>
        Docker Hub：DEFERRED / RESTRICTED。镜像唯一身份必须使用 digest，不以 tag
        作为唯一标识。
      </div>
      <Card style={{ marginTop: 14 }}>
        <JsonTable
          rows={images}
          headers={[
            ["registry", "Registry"],
            ["repository", "Repository"],
            ["tag", "Tag"],
            ["digest", "Digest"],
            ["size_bytes", "Size bytes"],
            ["architecture", "架构"],
            ["imported_at", "导入时间"],
            ["approval_state", "审批状态"],
            ["containers_in_use", "使用中容器"],
            ["jobs_in_use", "使用中作业"],
          ]}
          empty="当前没有受管镜像库存记录"
        />
      </Card>
    </ModuleFrame>
  );
}

export function MonitoringModule() {
  const query = useQuery({
    queryKey: ["alerts"],
    queryFn: alerts,
    refetchInterval: 30_000,
  });
  const rows = listAt(query.data, "alerts");
  return (
    <ModuleFrame
      title="监控和告警"
      description="Prometheus、DCGM Exporter、Node Exporter 和 Mellanox 指标"
      query={query}
    >
      <Card>
        <JsonTable
          rows={rows}
          headers={[
            ["name", "告警"],
            ["severity", "级别"],
            ["state", "状态"],
            ["summary", "摘要"],
            ["active_at", "触发时间"],
          ]}
          empty="当前没有活跃告警或告警数据不可用"
        />
      </Card>
    </ModuleFrame>
  );
}

export function OperationsModule() {
  const query = useQuery({
    queryKey: ["operations"],
    queryFn: operations,
    refetchInterval: 10_000,
  });
  const rows = listAt(query.data, "operations");
  return (
    <ModuleFrame
      title="审批任务"
      description="所有写操作先形成任务；Portal-0/1 只执行 dry-run"
      query={query}
    >
      <Card>
        <JsonTable
          rows={rows}
          headers={[
            ["id", "任务 ID"],
            ["operation_type", "操作"],
            ["target_id", "对象"],
            ["risk_level", "风险"],
            ["status", "状态"],
            ["result_summary", "结果"],
          ]}
          empty="暂无审批任务"
        />
      </Card>
      <div className="notice" style={{ marginTop: 14 }}>
        Slurm RESUME、用户 Activate、GPU
        隔离和配额变更在本阶段不会触碰宿主状态。
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
  const rows = listAt(query.data, "events");
  return (
    <ModuleFrame
      title="审计日志"
      description="安全事件、审批和 Worker 执行记录（敏感字段已脱敏）"
      query={query}
    >
      <Card>
        <JsonTable
          rows={rows}
          headers={[
            ["timestamp", "时间"],
            ["event_type", "事件"],
            ["actor", "操作者"],
            ["actor_role", "角色"],
            ["object_type", "对象类型"],
            ["result", "结果"],
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
      return response.json() as Promise<Record<string, unknown>>;
    },
    refetchInterval: 30_000,
  });
  return (
    <ModuleFrame
      title="系统"
      description="Portal 服务边界、版本和当前保护条件"
      query={query}
    >
      <div className="section-grid">
        <SectionCard title="部署边界">
          <dl className="kv-grid">
            <div className="kv">
              <dt>Web</dt>
              <dd className="mono">10.10.10.2:18080</dd>
            </div>
            <div className="kv">
              <dt>API</dt>
              <dd className="mono">127.0.0.1:18081</dd>
            </div>
            <div className="kv">
              <dt>访问方式</dt>
              <dd>私有 tun0 网络（SSH Tunnel 可选）</dd>
            </div>
            <div className="kv">
              <dt>写操作</dt>
              <dd>Worker dry-run</dd>
            </div>
            <div className="kv">
              <dt>Slurm</dt>
              <dd>
                <StatusBadge value="IDLE+DRAIN" />
              </dd>
            </div>
            <div className="kv">
              <dt>MIG</dt>
              <dd>MIG Disabled</dd>
            </div>
          </dl>
        </SectionCard>
        <SectionCard title="当前约束">
          <div className="notice">
            NETWORK PHYSICAL P0：Deferred by administrator
            <br />
            Docker Hub connectivity：Deferred
            <br />
            PCI DOE：P1 observation
            <br />
            NFS / 第二节点：未部署
          </div>
        </SectionCard>
      </div>
      <Card style={{ marginTop: 14, padding: 14 }}>
        <h2 style={{ fontSize: 14, marginTop: 0 }}>最新平台采样</h2>
        <pre
          className="mono"
          style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}
        >
          {JSON.stringify(query.data ?? "UNKNOWN", null, 2)}
        </pre>
      </Card>
    </ModuleFrame>
  );
}
