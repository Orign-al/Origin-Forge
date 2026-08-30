import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import {
  OrdinaryConnection,
  OrdinaryContainer,
  OrdinaryDashboard,
  OrdinaryStorage,
} from "../components/OrdinaryUserPages";
import type { SelfEnvironment } from "../lib/api";
import { I18nProvider } from "../lib/i18n";

const GPU_CONTAINER: SelfEnvironment["container"] = {
  id: "00000000-0000-4000-8000-000000000071",
  name: "gpu-dev-umar",
  state: "RUNNING",
  connection_state: "AVAILABLE",
  profile: "GPU_1_8CPU_32GB",
  gpu: 1,
  gpu_allocation_state: "NONE",
  cpus: 8,
  memory_gb: 32,
  pids_limit: 4096,
};

const CPU_CONTAINER: SelfEnvironment["container"] = {
  ...GPU_CONTAINER,
  profile: "STANDARD_8CPU_32GB",
  gpu: 0,
  gpu_allocation_state: "NONE",
};

const ENVIRONMENT: SelfEnvironment = {
  state: "ACTIVE",
  gpu_max: 1,
  host_access: "DISABLED",
  job_submission: "AVAILABLE",
  lease: {
    id: "00000000-0000-4000-8000-000000000072",
    state: "ACTIVE",
    active: true,
    starts_at: "2026-08-25T00:00:00Z",
    expires_at: "2026-08-29T00:00:00Z",
    remaining_seconds: 300_000,
    renewal_available: false,
    gpu_count: 1,
    max_duration_seconds: 345_600,
    renewal_window_seconds: 86_400,
    auto_renew: false,
  },
  container: GPU_CONTAINER,
  storage: {
    quota_bytes: 322_122_547_200,
    state: "ACTIVE",
    workspace: "/storage/users/20005",
    container_mount: "/workspace",
    default_job_workdir: "/storage/users/20005/projects",
  },
};

function renderWithCache(
  node: ReactNode,
  entries: Array<[readonly string[], unknown]>,
) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Number.POSITIVE_INFINITY },
      mutations: { retry: false },
    },
  });
  for (const [key, value] of entries) client.setQueryData(key, value);
  return render(
    <I18nProvider initialLocale="zh-CN">
      <QueryClientProvider client={client}>{node}</QueryClientProvider>
    </I18nProvider>,
  );
}

afterEach(cleanup);

describe("ordinary-user GPU Development visibility", () => {
  it("explains the GPU-less resident container and on-demand H100 jobs", () => {
    renderWithCache(<OrdinaryDashboard />, [
      [["self-environment"], { status: "OK", environment: ENVIRONMENT }],
      [
        ["self-storage"],
        {
          status: "OK",
          storage: {
            root: "/storage/users/20005",
            paths: ["/workspace", "/home/umar"],
            quota_bytes: 322_122_547_200,
            used_bytes: 1_024,
            available_bytes: 322_122_546_176,
            state: "ACTIVE",
            private: true,
          },
        },
      ],
    ]);

    expect(
      screen.getByText("开发容器 · 无 GPU Device；H100 作业按需调度"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "开发容器不挂载 GPU；H100 仅在作业运行期间由 Slurm 按需分配并在结束后自动释放。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/NOT ALLOCATED/)).toBeNull();
    expect(screen.queryByText("CPU Development · 无 GPU Device")).toBeNull();
  });

  it("shows no resident GPU and the on-demand job entitlement", () => {
    renderWithCache(<OrdinaryConnection />, [
      [
        ["self-container-connection"],
        {
          status: "OK",
          connection: {
            available: true,
            host: "20.10.10.3",
            port: 22027,
            username: "umar",
            authentication: "SSH_PUBLIC_KEY",
            profile: "GPU_1_8CPU_32GB",
            gpu: "NONE",
            key_fingerprint: "SHA256:fixture",
            command: "ssh fixture",
            vscode: "Host fixture",
          },
        },
      ],
    ]);

    expect(screen.getByText("NONE")).toBeInTheDocument();
    expect(screen.getByText("H100 × 1 · 按需调度")).toBeInTheDocument();
    expect(screen.queryByText(/NOT ALLOCATED/)).toBeNull();
  });

  it("labels a legacy live allocation as transitional", () => {
    renderWithCache(<OrdinaryConnection />, [
      [
        ["self-container-connection"],
        {
          status: "OK",
          connection: {
            available: true,
            host: "20.10.10.3",
            port: 22027,
            username: "umar",
            authentication: "SSH_PUBLIC_KEY",
            profile: "GPU_1_8CPU_32GB",
            gpu: "SLURM_ALLOCATED_1",
            key_fingerprint: "SHA256:fixture",
            command: "ssh fixture",
            vscode: "Host fixture",
          },
        },
      ],
    ]);

    expect(
      screen.getByText("开发容器 · H100 × 1 过渡分配"),
    ).toBeInTheDocument();
    expect(screen.getByText("H100 × 1 · TRANSITIONAL")).toBeInTheDocument();
  });

  it("distinguishes a stopped GPU profile from the default CPU profile", () => {
    const stoppedGpu = {
      ...GPU_CONTAINER,
      state: "STOPPED",
      connection_state: "DISABLED",
      gpu_allocation_state: "NONE",
    } satisfies SelfEnvironment["container"];
    const first = renderWithCache(<OrdinaryContainer />, [
      [["self-container"], { status: "OK", container: stoppedGpu }],
    ]);

    expect(
      screen.getByText("开发容器 · 无 GPU Device；H100 作业按需调度"),
    ).toBeInTheDocument();
    expect(screen.getByText("H100 × 1 · 按需调度")).toBeInTheDocument();
    first.unmount();

    renderWithCache(<OrdinaryContainer />, [
      [["self-container"], { status: "OK", container: CPU_CONTAINER }],
    ]);
    expect(
      screen.getByText("CPU Development · 无 GPU Device"),
    ).toBeInTheDocument();
    expect(screen.getByText("NONE")).toBeInTheDocument();
  });

  it("labels XFS project usage as the total persistent private quota", () => {
    renderWithCache(<OrdinaryStorage />, [
      [
        ["self-storage"],
        {
          status: "OK",
          storage: {
            root: "/storage/users/20005",
            paths: ["/workspace", "/home/umar"],
            quota_bytes: 300 * 1024 ** 3,
            used_bytes: 80 * 1024 ** 3,
            available_bytes: 220 * 1024 ** 3,
            state: "ACTIVE",
            private: true,
          },
        },
      ],
    ]);

    expect(screen.getByText("私有存储总配额")).toBeInTheDocument();
    expect(screen.getByText("80.0 GB")).toBeInTheDocument();
    expect(screen.getByText(/已使用量包含 \/home/)).toBeInTheDocument();
  });
});
