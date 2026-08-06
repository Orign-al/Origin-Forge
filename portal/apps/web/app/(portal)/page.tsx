"use client";

import { useQuery } from "@tanstack/react-query";
import { Card, StatusBadge } from "@h100-portal/ui";

import {
  PageHeading,
  SectionCard,
  LoadingBlock,
  ErrorBlock,
  StaleNotice,
  UnauthorizedBlock,
} from "../../components/PortalShell";
import { ApiError, overview } from "../../lib/api";

function value(item: unknown, fallback = "UNKNOWN") {
  return item === undefined || item === null || item === ""
    ? fallback
    : String(item);
}

export default function DashboardPage() {
  const query = useQuery({
    queryKey: ["overview"],
    queryFn: overview,
    refetchInterval: 30_000,
  });
  if (query.isPending)
    return (
      <>
        <PageHeading title="总览" description="单节点 H100 平台实时概况" />
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
        <PageHeading title="总览" description="单节点 H100 平台实时概况" />
        <UnauthorizedBlock />
      </>
    );
  if (query.isError)
    return (
      <>
        <PageHeading title="总览" description="单节点 H100 平台实时概况" />
        <ErrorBlock message="总览数据暂时不可用；页面不会用 0 伪造健康状态。" />
      </>
    );
  const data = query.data;
  const nodeResult = data.platform.node ?? {};
  const jobs = data.platform.jobs ?? {};
  const gpu = data.platform.gpu ?? { status: "UNKNOWN" };
  const systemd = data.platform.systemd ?? {};
  const identity = data.identity ?? {
    portal_users: 0,
    managed_linux_users: 0,
    account_states: {},
    onboarding_states: {},
  };
  const tasks = data.tasks ?? { pending_approval: 0, failed: 0, total: 0 };
  const jobList = Array.isArray((jobs as { jobs?: unknown[] }).jobs)
    ? (jobs as { jobs: Array<Record<string, unknown>> }).jobs
    : Array.isArray((jobs as { data?: { jobs?: unknown[] } }).data?.jobs)
      ? (jobs as { data: { jobs: Array<Record<string, unknown>> } }).data.jobs
      : [];
  const nodeRows = Array.isArray((nodeResult as { nodes?: unknown[] }).nodes)
    ? (nodeResult as { nodes: Array<Record<string, unknown>> }).nodes
    : Array.isArray(
          (nodeResult as { data?: { nodes?: unknown[] } }).data?.nodes,
        )
      ? (nodeResult as { data: { nodes: Array<Record<string, unknown>> } }).data
          .nodes
      : [];
  const node = nodeRows[0] ?? nodeResult;
  const gpuList = gpu.gpus ?? [];
  const queue = jobList.filter((item) =>
    String(item.state ?? "")
      .toUpperCase()
      .includes("PEND"),
  ).length;
  const running = jobList.filter((item) =>
    String(item.state ?? "")
      .toUpperCase()
      .includes("RUN"),
  ).length;
  const reason = value(
    (node as Record<string, unknown>).Reason ??
      (node as Record<string, unknown>).reason,
  );
  return (
    <>
      <PageHeading
        title="总览"
        description="单节点 H100 平台实时概况"
        action={<StatusBadge value="PRIVATE TUNNEL NETWORK MODE" />}
      />
      {query.isFetching ? <StaleNotice /> : null}
      <div className="grid-compact" style={{ marginTop: 14 }}>
        <Card className="stat-panel">
          <div className="stat-label">Slurm 节点</div>
          <div className="stat-value">
            <StatusBadge
              value={value(
                (node as Record<string, unknown>).State ??
                  (node as Record<string, unknown>).state,
              )}
            />
          </div>
          <div className="stat-detail">sagsh100server · 不自动 RESUME</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">GPU</div>
          <div className="stat-value">
            {gpu.count ?? "—"}
            <span className="muted" style={{ fontSize: 13, marginLeft: 5 }}>
              H100
            </span>
          </div>
          <div className="stat-detail">MIG Disabled · 数据源 {gpu.status}</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">队列 / 运行</div>
          <div className="stat-value">
            {queue}{" "}
            <span className="muted" style={{ fontSize: 13 }}>
              {" "}
              / {running}
            </span>
          </div>
          <div className="stat-detail">当前采样作业 {jobList.length}</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">失败 systemd units</div>
          <div className="stat-value">{value(systemd.count, "—")}</div>
          <div className="stat-detail">不以 API 错误伪装为 0</div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">网页 / 受管用户</div>
          <div className="stat-value">
            {identity.portal_users}{" "}
            <span className="muted">/ {identity.managed_linux_users}</span>
          </div>
          <div className="stat-detail">
            ACTIVE {identity.account_states.ACTIVE ?? 0} · STAGED{" "}
            {identity.onboarding_states.STAGED ?? 0}
          </div>
        </Card>
        <Card className="stat-panel">
          <div className="stat-label">待审批 / 失败任务</div>
          <div className="stat-value">
            {tasks.pending_approval}{" "}
            <span className="muted">/ {tasks.failed}</span>
          </div>
          <div className="stat-detail">任务总数 {tasks.total}</div>
        </Card>
      </div>
      <div className="section-grid">
        <SectionCard title="平台状态" subtitle="Slurm、GPU 与容器数据源">
          <dl className="kv-grid">
            <div className="kv">
              <dt>节点状态</dt>
              <dd>
                <StatusBadge
                  value={value(
                    (node as Record<string, unknown>).State ??
                      (node as Record<string, unknown>).state,
                  )}
                />
              </dd>
            </div>
            <div className="kv">
              <dt>DRAIN Reason</dt>
              <dd className="mono">{reason}</dd>
            </div>
            <div className="kv">
              <dt>GPU 数据源</dt>
              <dd>
                <StatusBadge value={gpu.status} />
              </dd>
            </div>
            <div className="kv">
              <dt>容器数据源</dt>
              <dd>
                <StatusBadge value={data.containers.status} />
              </dd>
            </div>
            <div className="kv">
              <dt>存储数据源</dt>
              <dd>
                <StatusBadge value={data.storage.status} />
              </dd>
            </div>
            <div className="kv">
              <dt>活跃告警</dt>
              <dd>{data.alerts.count ?? "—"}</dd>
            </div>
          </dl>
        </SectionCard>
        <SectionCard title="风险与边界" subtitle="当前已知事项">
          <div className="notice">
            Mellanox 物理网络 P0：由管理员接受为单节点 Pilot 风险。
          </div>
          <div className="notice" style={{ marginTop: 9 }}>
            Docker Hub：DEFERRED / RESTRICTED，需要替代 Registry。
          </div>
          <div className="notice" style={{ marginTop: 9 }}>
            PCI DOE：P1 observation；MIG 未修改。
          </div>
        </SectionCard>
      </div>
      <div className="section-grid">
        <SectionCard
          title="GPU 设备"
          subtitle="UUID、PCI Bus 与 Linux minor 同时展示"
        >
          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>NVML index</th>
                  <th>UUID</th>
                  <th>PCI Bus</th>
                  <th>minor / device</th>
                  <th>显存</th>
                  <th>利用率</th>
                  <th>温度 / 功耗</th>
                  <th>MIG</th>
                  <th>DCGM</th>
                  <th>Xid/AER</th>
                  <th>Slurm Job</th>
                </tr>
              </thead>
              <tbody>
                {gpuList.length ? (
                  gpuList.map((item) => (
                    <tr key={item.uuid}>
                      <td>{item.index}</td>
                      <td className="mono">{item.uuid}</td>
                      <td className="mono">{item["pci.bus_id"]}</td>
                      <td className="mono">
                        {item.minor_number} / {item.device_path}
                      </td>
                      <td>
                        {item["memory.used"] ?? "—"} /{" "}
                        {item["memory.total"] ?? "—"} MiB
                      </td>
                      <td>{item["utilization.gpu"] ?? "—"}%</td>
                      <td>
                        {item["temperature.gpu"] ?? "—"} °C /{" "}
                        {item["power.draw"] ?? "—"} W
                      </td>
                      <td>MIG Disabled</td>
                      <td>
                        <StatusBadge
                          value={String(item.dcgm_status ?? "UNKNOWN")}
                        />
                      </td>
                      <td>
                        <StatusBadge
                          value={String(item.xid_aer_status ?? "UNKNOWN")}
                        />
                      </td>
                      <td className="mono">
                        {String(item.active_slurm_job_ids ?? "—")}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={11}>
                      <div className="ui-empty">GPU 数据不可用</div>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </SectionCard>
        <SectionCard title="Registry 状态" subtitle="镜像来源不使用虚构可用性">
          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>Registry</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {data.registries.registries?.map((registry) => (
                  <tr key={registry.name}>
                    <td>{registry.name}</td>
                    <td>
                      <StatusBadge value={registry.status} />
                    </td>
                  </tr>
                )) ?? (
                  <tr>
                    <td colSpan={2}>数据不可用</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </SectionCard>
      </div>
      <div className="section-grid">
        <SectionCard title="最近作业" subtitle="Slurm 结构化采样">
          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>Job ID</th>
                  <th>用户</th>
                  <th>状态</th>
                  <th>分区</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody>
                {jobList.slice(0, 8).map((job) => (
                  <tr key={String(job.job_id ?? job.id)}>
                    <td className="mono">
                      {String(job.job_id ?? job.id ?? "—")}
                    </td>
                    <td>{String(job.user ?? "—")}</td>
                    <td>
                      <StatusBadge value={String(job.state ?? "UNKNOWN")} />
                    </td>
                    <td>{String(job.partition ?? "—")}</td>
                    <td>{String(job.reason ?? "—")}</td>
                  </tr>
                ))}
                {jobList.length === 0 ? (
                  <tr>
                    <td colSpan={5}>
                      <div className="ui-empty">当前没有作业</div>
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </SectionCard>
        <SectionCard title="活跃告警" subtitle="Prometheus localhost API">
          <div className="ui-table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>级别</th>
                  <th>状态</th>
                  <th>摘要</th>
                </tr>
              </thead>
              <tbody>
                {data.alerts.alerts?.slice(0, 8).map((alert) => (
                  <tr key={String(alert.name)}>
                    <td>{String(alert.name ?? "—")}</td>
                    <td>{String(alert.severity ?? "—")}</td>
                    <td>
                      <StatusBadge value={String(alert.state ?? "UNKNOWN")} />
                    </td>
                    <td>{String(alert.summary ?? "—")}</td>
                  </tr>
                )) ?? (
                  <tr>
                    <td colSpan={4}>告警数据不可用</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </SectionCard>
      </div>
      <div className="section-grid">
        <SectionCard title="存储摘要" subtitle="真实 df / VG / 固定目录采样">
          <dl className="kv-grid">
            {data.storage.mounts?.map((mount) => (
              <div className="kv" key={String(mount.target)}>
                <dt>{String(mount.target)}</dt>
                <dd>
                  {String(mount.percent ?? "—")} · {String(mount.avail ?? "—")}{" "}
                  可用
                </dd>
              </div>
            )) ?? <div className="muted">存储数据不可用</div>}
          </dl>
        </SectionCard>
        <SectionCard
          title="最近审计事件"
          subtitle="不显示密码、token 或 Cookie"
        >
          <TablePreview rows={data.recent_audit ?? []} />
        </SectionCard>
      </div>
    </>
  );
}

function TablePreview({ rows }: { rows: Array<Record<string, unknown>> }) {
  if (!rows.length) return <div className="muted">暂无审计事件</div>;
  return (
    <div className="ui-table-wrap">
      <table className="ui-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>事件</th>
            <th>操作者</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 8).map((row, index) => (
            <tr key={String(row.timestamp ?? index)}>
              <td className="mono">{String(row.timestamp ?? "—")}</td>
              <td>{String(row.event_type ?? "—")}</td>
              <td>{String(row.actor ?? "—")}</td>
              <td>
                <StatusBadge value={String(row.result ?? "UNKNOWN")} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
