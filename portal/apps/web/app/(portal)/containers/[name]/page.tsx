"use client";

import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { Card, StatusBadge } from "@h100-portal/ui";
import {
  ErrorBlock,
  LoadingBlock,
  PageHeading,
  SectionCard,
} from "../../../../components/PortalShell";
import { containerInspect } from "../../../../lib/api";

function display(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export default function ContainerDetailPage() {
  const params = useParams<{ name: string }>();
  const query = useQuery({
    queryKey: ["container", params.name],
    queryFn: () => containerInspect(params.name),
  });
  if (query.isPending)
    return (
      <>
        <PageHeading title="容器详情" />
        <LoadingBlock />
      </>
    );
  if (query.isError)
    return (
      <>
        <PageHeading title="容器详情" />
        <ErrorBlock />
      </>
    );
  const container = (query.data.container ?? {}) as Record<string, unknown>;
  const state = (container.state ?? {}) as Record<string, unknown>;
  const deviceRequests = container.device_requests;
  const gpuNone = !Array.isArray(deviceRequests) || deviceRequests.length === 0;
  return (
    <>
      <PageHeading
        title={display(container.name).replace(/^\//, "")}
        description="固定 docker inspect 字段白名单"
        action={
          <StatusBadge
            value={display(state.Status ?? state.status ?? "UNKNOWN")}
          />
        }
      />
      <div className="security-strip">
        <span>GPU：{gpuNone ? "无" : "检测到 DeviceRequest"}</span>
        <span>
          Privileged：
          {container.privileged === false
            ? "否"
            : display(container.privileged)}
        </span>
        <span>
          Docker Socket：
          {JSON.stringify(container.mounts ?? []).includes("docker.sock")
            ? "检测到"
            : "未挂载"}
        </span>
        <span>
          Host Network：{container.network_mode === "host" ? "是" : "否"}
        </span>
        <span>PID limit：{display(container.pids_limit)}</span>
      </div>
      <Card className="detail-panel">
        <dl className="kv-grid">
          {[
            ["镜像", container.image],
            ["镜像 digest", container.image_digest],
            ["用户", container.owner],
            ["CPU limit", container.cpu_limit],
            ["Memory limit bytes", container.memory_limit_bytes],
            ["PIDs limit", container.pids_limit],
            ["SSH 端口", container.ssh_port],
            ["容器 ID", container.id],
            ["创建时间", container.created],
            ["端口", container.ports],
            ["DeviceRequest", deviceRequests],
            ["Privileged", container.privileged],
            ["NetworkMode", container.network_mode],
            ["PidMode", container.pid_mode],
            ["IpcMode", container.ipc_mode],
            ["RestartPolicy", container.restart_policy],
            [
              "Docker Socket",
              container.docker_socket_mounted ? "检测到" : "未挂载",
            ],
          ].map(([label, value]) => (
            <div className="kv" key={String(label)}>
              <dt>{String(label)}</dt>
              <dd className="mono">{display(value)}</dd>
            </div>
          ))}
        </dl>
      </Card>
      <div className="section-grid">
        <SectionCard title="Mounts" subtitle="只读白名单字段">
          <pre className="mono detail-pre">
            {JSON.stringify(container.mounts ?? [], null, 2)}
          </pre>
        </SectionCard>
        <SectionCard title="运行状态">
          <pre className="mono detail-pre">
            {JSON.stringify(state, null, 2)}
          </pre>
        </SectionCard>
      </div>
      {!gpuNone ||
      container.privileged !== false ||
      container.network_mode === "host" ? (
        <div className="error-box">
          容器安全属性偏离默认值；页面未执行任何自动修复。
        </div>
      ) : null}
    </>
  );
}
