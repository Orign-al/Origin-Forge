import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobsModule } from "../components/ModulePages";
import { OrdinaryJobs } from "../components/OrdinaryUserPages";
import {
  type AdminJobGpuApproval,
  type AdminJobMemoryApproval,
  adminJobMemoryApprovals,
  adminJobGpuApprovals,
  decideJobGpuApproval,
  decideJobMemoryApproval,
  me,
  reauthenticate,
  selfJobs,
  slurmHistory,
  slurmJobs,
  submitSelfJob,
} from "../lib/api";
import { I18nProvider } from "../lib/i18n";

vi.mock("../lib/random-uuid", () => ({
  randomUuid: () => "10000000-0000-4000-8000-000000000404",
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    adminJobGpuApprovals: vi.fn(),
    adminJobMemoryApprovals: vi.fn(),
    decideJobGpuApproval: vi.fn(),
    decideJobMemoryApproval: vi.fn(),
    me: vi.fn(),
    reauthenticate: vi.fn(),
    selfJobs: vi.fn(),
    slurmHistory: vi.fn(),
    slurmJobs: vi.fn(),
    submitSelfJob: vi.fn(),
  };
});

const JOB_ID = "10000000-0000-4000-8000-000000000400";
const APPROVAL_ID = "20000000-0000-4000-8000-000000000400";

const pendingApproval: AdminJobGpuApproval = {
  id: APPROVAL_ID,
  state: "PENDING",
  requested_gpu_count: 4,
  approved_gpu_count: null,
  script_sha256: "a".repeat(64),
  requested_at: "2026-09-05T10:00:00Z",
  reviewed_at: null,
  reviewed_by: null,
  decision_comment: null,
  model_name: "Llama 3.1",
  model_architecture: "decoder-only transformer",
  framework: "PyTorch",
  framework_version: "2.6.0",
  parameter_count: "70B",
  workload_description: "full-parameter supervised fine-tuning",
  dataset_description: "curated 2 TB training corpus",
  parallel_strategy: "FSDP full shard across four GPUs",
  scaling_justification: "model and optimizer state do not fit on one GPU",
  portal_job_id: JOB_ID,
  owner: {
    portal_user_id: "30000000-0000-4000-8000-000000000400",
    login_name: "acceptance-user",
    display_name: "Acceptance User",
    managed_user_id: "40000000-0000-4000-8000-000000000400",
    unix_username: "acceptance-user",
  },
  job: {
    id: JOB_ID,
    slurm_job_id: null,
    name: "llama-finetune",
    state: "APPROVAL_PENDING",
    reason: "Administrator approval required",
    cpus: 8,
    memory_mb: 32768,
    gpu_count: 4,
    gpu_approval: null,
    memory_approval: null,
    time_limit_seconds: 7200,
    script_path: ".portal/job-scripts/fixture.sh",
    script_snapshot_path: "/workspace/.portal/job-scripts/fixture.sh",
    source_path: "/workspace/projects/llama/train.sh",
    workdir: "/workspace/projects/llama",
    stdout_path: "/workspace/outputs/fixture.out",
    stderr_path: "/workspace/outputs/fixture.err",
    lease_deadline_at: "2026-09-06T10:00:00Z",
    created_at: "2026-09-05T10:00:00Z",
    submitted_at: null,
    started_at: null,
    finished_at: null,
    elapsed_seconds: null,
    exit_code: null,
    authoritative: true,
  },
};

const pendingMemoryApproval: AdminJobMemoryApproval = {
  id: "20000000-0000-4000-8000-000000000401",
  state: "PENDING",
  requested_memory_mb: 131072,
  approved_memory_mb: null,
  workload_description: "large CPU data preprocessing",
  memory_breakdown: "96 GiB index and 32 GiB runtime",
  memory_justification: "the pipeline cannot stream its index",
  script_sha256: "b".repeat(64),
  requested_at: "2026-09-07T10:00:00Z",
  reviewed_at: null,
  reviewed_by: null,
  decision_comment: null,
  portal_job_id: JOB_ID,
  owner: pendingApproval.owner,
  job: {
    ...pendingApproval.job,
    memory_mb: 131072,
    gpu_count: 0,
    gpu_approval: null,
    memory_approval: null,
  },
};

function renderWithClient(node: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Number.POSITIVE_INFINITY },
      mutations: { retry: false },
    },
  });
  return render(
    <I18nProvider initialLocale="zh-CN">
      <QueryClientProvider client={client}>{node}</QueryClientProvider>
    </I18nProvider>,
  );
}

function configureAdmin(role: string, approvals = [pendingApproval]) {
  vi.mocked(me).mockResolvedValue({ role } as Awaited<ReturnType<typeof me>>);
  vi.mocked(adminJobGpuApprovals).mockResolvedValue({
    status: "OK",
    approvals,
    count: approvals.length,
  });
  vi.mocked(adminJobMemoryApprovals).mockResolvedValue({
    status: "OK",
    approvals: [],
    count: 0,
  });
  vi.mocked(slurmJobs).mockResolvedValue({ jobs: [] });
  vi.mocked(slurmHistory).mockResolvedValue({ jobs: [] });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("multi-GPU request and review UI", () => {
  it("requires complete model and scaling details for 2-4 GPUs", async () => {
    vi.mocked(selfJobs).mockResolvedValue({ status: "OK", jobs: [], count: 0 });
    vi.mocked(submitSelfJob).mockResolvedValue({
      status: "APPROVAL_PENDING",
      job: pendingApproval.job,
    });
    renderWithClient(<OrdinaryJobs />);

    const gpu = await screen.findByLabelText("GPU");
    fireEvent.change(gpu, { target: { value: "4" } });
    expect(screen.getByText(/多GPU作业不会立即进入Slurm/)).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "提交作业" });
    expect(submit).toBeDisabled();

    const details: Array<[string, string]> = [
      ["模型名称", "Llama 3.1"],
      ["模型架构", "decoder-only transformer"],
      ["框架", "PyTorch"],
      ["框架版本", "2.6.0"],
      ["模型规模 / 参数量", "70B"],
      ["训练/推理任务说明", "full-parameter supervised fine-tuning"],
      ["数据集说明", "curated 2 TB training corpus"],
      ["并行策略", "FSDP full shard across four GPUs"],
      ["多卡扩展收益与合理性", "model state does not fit on one GPU"],
    ];
    for (const [label, value] of details) {
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
    }
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(submitSelfJob).toHaveBeenCalledTimes(1));
    expect(submitSelfJob).toHaveBeenCalledWith(
      expect.objectContaining({
        gpu_count: 4,
        multi_gpu_request: {
          model_name: "Llama 3.1",
          model_architecture: "decoder-only transformer",
          framework: "PyTorch",
          framework_version: "2.6.0",
          parameter_count: "70B",
          workload_description: "full-parameter supervised fine-tuning",
          dataset_description: "curated 2 TB training corpus",
          parallel_strategy: "FSDP full shard across four GPUs",
          scaling_justification: "model state does not fit on one GPU",
        },
        idempotency_key: "10000000-0000-4000-8000-000000000404",
      }),
    );
  });

  it("requires a separate justification above 32 GiB", async () => {
    vi.mocked(selfJobs).mockResolvedValue({ status: "OK", jobs: [], count: 0 });
    vi.mocked(submitSelfJob).mockResolvedValue({
      status: "APPROVAL_PENDING",
      job: pendingMemoryApproval.job,
    });
    renderWithClient(<OrdinaryJobs />);

    fireEvent.change(await screen.findByLabelText(/^内存 MiB/u), {
      target: { value: "131072" },
    });
    expect(
      screen.getByText(/超过32 GiB的作业不会立即进入Slurm/),
    ).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "提交作业" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText("高内存任务说明"), {
      target: { value: "large CPU data preprocessing" },
    });
    fireEvent.change(screen.getByLabelText("内存用量拆分"), {
      target: { value: "96 GiB index and 32 GiB runtime" },
    });
    fireEvent.change(screen.getByLabelText("高内存必要性"), {
      target: { value: "the pipeline cannot stream its index" },
    });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(submitSelfJob).toHaveBeenCalledTimes(1));
    expect(submitSelfJob).toHaveBeenCalledWith(
      expect.objectContaining({
        memory_mb: 131072,
        high_memory_request: {
          workload_description: "large CPU data preprocessing",
          memory_breakdown: "96 GiB index and 32 GiB runtime",
          memory_justification: "the pipeline cannot stream its index",
        },
      }),
    );
  });

  it("lets an owner reduce the GPU count only after reauthentication", async () => {
    configureAdmin("platform_owner");
    vi.mocked(reauthenticate).mockResolvedValue({ reauthenticated: true });
    vi.mocked(decideJobGpuApproval).mockResolvedValue({
      status: "APPROVED",
      idempotent_replay: false,
      approval: {
        ...pendingApproval,
        state: "APPROVED",
        approved_gpu_count: 3,
      },
    });
    renderWithClient(<JobsModule />);

    fireEvent.change(await screen.findByLabelText("批准GPU数量"), {
      target: { value: "3" },
    });
    fireEvent.change(screen.getByLabelText("审批意见"), {
      target: { value: "three GPUs are sufficient for this topology" },
    });
    fireEvent.change(screen.getByLabelText("管理员密码（最近认证）"), {
      target: { value: "fixture password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "按所选GPU数量批准" }));

    await waitFor(() => expect(decideJobGpuApproval).toHaveBeenCalledTimes(1));
    expect(reauthenticate).toHaveBeenCalledWith("fixture password");
    expect(decideJobGpuApproval).toHaveBeenCalledWith(APPROVAL_ID, {
      decision: "APPROVE",
      approved_gpu_count: 3,
      comment: "three GPUs are sufficient for this topology",
    });
    expect(vi.mocked(reauthenticate).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(decideJobGpuApproval).mock.invocationCallOrder[0],
    );
  });

  it("submits rejection without an approved GPU count", async () => {
    configureAdmin("platform_admin");
    vi.mocked(reauthenticate).mockResolvedValue({ reauthenticated: true });
    vi.mocked(decideJobGpuApproval).mockResolvedValue({
      status: "REJECTED",
      idempotent_replay: false,
      approval: { ...pendingApproval, state: "REJECTED" },
    });
    renderWithClient(<JobsModule />);

    await screen.findByLabelText("批准GPU数量");
    fireEvent.change(screen.getByLabelText("审批意见"), {
      target: { value: "the scaling evidence does not justify four GPUs" },
    });
    fireEvent.change(screen.getByLabelText("管理员密码（最近认证）"), {
      target: { value: "fixture password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "驳回" }));

    await waitFor(() => expect(decideJobGpuApproval).toHaveBeenCalledTimes(1));
    expect(decideJobGpuApproval).toHaveBeenCalledWith(APPROVAL_ID, {
      decision: "REJECT",
      approved_gpu_count: null,
      comment: "the scaling evidence does not justify four GPUs",
    });
  });

  it("lets an administrator lower the approved memory", async () => {
    configureAdmin("platform_admin", []);
    vi.mocked(adminJobMemoryApprovals).mockResolvedValue({
      status: "OK",
      approvals: [pendingMemoryApproval],
      count: 1,
    });
    vi.mocked(reauthenticate).mockResolvedValue({ reauthenticated: true });
    vi.mocked(decideJobMemoryApproval).mockResolvedValue({
      status: "APPROVED",
      idempotent_replay: false,
      approval: {
        ...pendingMemoryApproval,
        state: "APPROVED",
        approved_memory_mb: 98304,
      },
    });
    renderWithClient(<JobsModule />);

    fireEvent.change(await screen.findByLabelText(/^批准内存 MiB/u), {
      target: { value: "98304" },
    });
    fireEvent.change(screen.getByLabelText("审批意见"), {
      target: { value: "96 GiB is sufficient" },
    });
    fireEvent.change(screen.getByLabelText("管理员密码（最近认证）"), {
      target: { value: "fixture password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "按所选内存批准" }));

    await waitFor(() =>
      expect(decideJobMemoryApproval).toHaveBeenCalledTimes(1),
    );
    expect(decideJobMemoryApproval).toHaveBeenCalledWith(
      pendingMemoryApproval.id,
      {
        decision: "APPROVE",
        approved_memory_mb: 98304,
        comment: "96 GiB is sufficient",
      },
    );
  });

  it.each(["operator", "auditor", "user"])(
    "does not expose approval controls to %s",
    async (role) => {
      configureAdmin(role);
      renderWithClient(<JobsModule />);

      await waitFor(() => expect(me).toHaveBeenCalled());
      expect(screen.queryByText("多GPU审批")).not.toBeInTheDocument();
      expect(screen.queryByText("内存审批")).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "按所选GPU数量批准" }),
      ).not.toBeInTheDocument();
      expect(adminJobGpuApprovals).not.toHaveBeenCalled();
      expect(adminJobMemoryApprovals).not.toHaveBeenCalled();
    },
  );
});
