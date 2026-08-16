import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ComputeRequestDetailPage from "../app/(portal)/compute-requests/[id]/page";
import {
  type ComputeResourceRequest,
  type CurrentSession,
  type ProvisionWorkflowResult,
  adminComputeRequest,
  approveAndProvision,
  me,
  reauthenticate,
  retryProvision,
} from "../lib/api";

const REQUEST_ID = "10000000-0000-4000-8000-000000000001";
const PLAN_ID = "10000000-0000-4000-8000-000000000002";
const OPERATION_ID = "10000000-0000-4000-8000-000000000003";
const IDEMPOTENCY_KEY = "10000000-0000-4000-8000-000000000004";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "10000000-0000-4000-8000-000000000001" }),
  usePathname: () =>
    "/compute-requests/10000000-0000-4000-8000-000000000001",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn() }),
}));

vi.mock("../lib/random-uuid", () => ({
  randomUuid: () => "10000000-0000-4000-8000-000000000004",
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    adminComputeRequest: vi.fn(),
    approveAndProvision: vi.fn(),
    me: vi.fn(),
    reauthenticate: vi.fn(),
    retryProvision: vi.fn(),
  };
});

function currentSession(
  recentAuthValid: boolean,
  role = "platform_owner",
): CurrentSession {
  return {
    user: {
      id: "10000000-0000-4000-8000-000000000010",
      login_name: role === "user" ? "ordinary-user" : "origin-al",
      normalized_login: role === "user" ? "ordinary-user" : "origin-al",
      display_name: role === "user" ? "Ordinary User" : "Platform Owner",
      unix_username: null,
      account_state: "ACTIVE",
      password_state: "SET",
      resource_onboarding_state: "NOT_ENROLLED",
      roles: [{ name: role, description: role }],
    },
    role,
    ssh_enrollment: {
      required: false,
      managed_user_id: null,
      compute_identity: null,
      compute_state: "NOT_ENROLLED",
      validated_key_count: 0,
      ssh_key_state: "NOT_APPLICABLE",
      setup_path: null,
    },
    recent_auth_valid: recentAuthValid,
    recent_auth_valid_until: recentAuthValid
      ? "2026-08-16T12:15:00Z"
      : null,
  };
}

function requestFixture(
  status: "REQUESTED" | "PROVISIONING" | "FAILED",
  options: { retryAvailable?: boolean; unknown?: string[] } = {},
): ComputeResourceRequest {
  const operation = {
    id: OPERATION_ID,
    operation_type: "compute.provision",
    status: status === "PROVISIONING" ? "RUNNING" : "FAILED",
    started_at: "2026-08-16T10:00:00Z",
    finished_at: status === "PROVISIONING" ? null : "2026-08-16T10:01:00Z",
    error_code: status === "FAILED" ? "COMPUTE_STAGE_FAILED" : null,
    rollback_status:
      status === "FAILED"
        ? options.retryAvailable
          ? "ROLLED_BACK"
          : "REQUIRES_MANUAL_REVIEW"
        : "NOT_REQUIRED",
    safe_summary: status === "FAILED" ? "Provision failed safely" : null,
    safe_root_cause:
      status === "FAILED" ? "Container image validation failed" : null,
    side_effect_classification:
      status === "FAILED"
        ? options.retryAvailable
          ? "PARTIAL_ROLLED_BACK"
          : "PARTIAL_UNKNOWN"
        : null,
    last_successful_step: status === "FAILED" ? "SLURM_ASSOCIATION" : null,
    first_failed_step: status === "FAILED" ? "CONTAINER_IMAGE" : null,
    failed_handler: status === "FAILED" ? "compute.provision.stage" : null,
    workflow_steps: {
      PREPARING: "SUCCEEDED",
      VALIDATING: "SUCCEEDED",
      CREATING_ENVIRONMENT:
        status === "PROVISIONING" ? "RUNNING" : "FAILED",
      FINALIZING: "PENDING",
    },
    deployment_version: "a".repeat(40),
    canonical_execution_contract: "b".repeat(64),
    reconciliation_status: options.retryAvailable ? "VERIFIED" : "MANUAL_REVIEW",
    resource_residue: [],
    unknown_resource_state: options.unknown ?? [],
  };
  const plan = {
    id: PLAN_ID,
    attempt_number: 1,
    attempt_reason: "INITIAL" as const,
    state: status === "PROVISIONING" ? "PROVISIONING" : "FAILED",
    username: "origin-pilot2",
    uid: 20002,
    gid: 20002,
    project_id: 30002,
    container_name: "gpu-dev-origin-pilot2",
    container_ssh_port: 22024,
    storage_bytes: 322122547200 as const,
    container_profile: "STANDARD_8CPU_32GB" as const,
    container_cpus: 8 as const,
    container_memory_gb: 32 as const,
    container_pids_limit: 4096 as const,
    container_gpu: 0 as const,
    slurm_account: "company" as const,
    slurm_qos: "general" as const,
    gpu_max: 1 as const,
    lease_seconds: 345600 as const,
    lease_state: "NOT_STARTED" as const,
    host_ssh: "DISABLED" as const,
    shell: "/usr/sbin/nologin" as const,
    password_state: "LOCKED" as const,
    execution_enabled: status !== "REQUESTED",
    reservation_expires_at: "2026-08-16T11:00:00Z",
    dry_run_at: "2026-08-16T10:00:30Z",
  };
  return {
    id: REQUEST_ID,
    portal_account_id: "10000000-0000-4000-8000-000000000011",
    requested_by: "10000000-0000-4000-8000-000000000011",
    managed_user_id: null,
    username: "origin-pilot2",
    status,
    lifecycle_state: status === "REQUESTED" ? "PENDING" : status,
    approval_state: status === "REQUESTED" ? "NOT_APPROVED" : "APPROVED",
    requested_gpu_max: 1,
    requested_storage_bytes: 322122547200,
    requested_container_profile: "STANDARD_8CPU_32GB",
    requested_lease_seconds: 345600,
    purpose: "multi-user Portal fixture",
    user_note: null,
    submitted_at: "2026-08-16T09:55:00Z",
    review_note: null,
    reviewed_at: status === "REQUESTED" ? null : "2026-08-16T10:00:00Z",
    reviewed_by: status === "REQUESTED" ? null : OPERATION_ID,
    approved_at: status === "REQUESTED" ? null : "2026-08-16T10:00:00Z",
    rejected_at: null,
    cancelled_at: null,
    created_at: "2026-08-16T09:55:00Z",
    updated_at: "2026-08-16T10:01:00Z",
    plan: status === "REQUESTED" ? null : plan,
    attempts:
      status === "REQUESTED"
        ? []
        : [
            {
              attempt_number: 1,
              attempt_reason: "INITIAL",
              plan,
              operations: [operation],
              provision_operation: operation,
              stage_operation: status === "FAILED" ? operation : null,
              reservations: {},
            },
          ],
    retry_available: options.retryAvailable ?? false,
    retry_state:
      status === "FAILED"
        ? options.retryAvailable
          ? "RETRY_ELIGIBLE"
          : "MANUAL_REVIEW"
        : "NOT_REQUIRED",
    portal_user: {
      login_name: "origin-pilot2",
      display_name: "Origin Pilot 2",
      role: "user",
      account_state: "ACTIVE",
      password_state: "SET",
      compute_state: "NOT_ENROLLED",
    },
  };
}

function workflowResult(
  operationType: "compute.provision" | "compute.provision.retry",
): ProvisionWorkflowResult {
  const request = requestFixture("PROVISIONING");
  const operation = request.attempts?.[0]?.provision_operation;
  if (!operation) throw new Error("fixture operation missing");
  return {
    status: "PROVISIONING",
    operation_id: OPERATION_ID,
    operation: { ...operation, operation_type: operationType },
    idempotent_replay: false,
    request,
  };
}

function renderPage(request: ComputeResourceRequest, session: CurrentSession) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  vi.mocked(adminComputeRequest).mockResolvedValue({ status: "OK", request });
  vi.mocked(me).mockResolvedValue(session);
  return render(
    <QueryClientProvider client={queryClient}>
      <ComputeRequestDetailPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("simplified Provision administrator workflow", () => {
  it("runs initial Approval as one action and reuses a valid recent-auth window", async () => {
    vi.mocked(approveAndProvision).mockResolvedValue(
      workflowResult("compute.provision"),
    );
    renderPage(requestFixture("REQUESTED"), currentSession(true));

    const approve = await screen.findByRole("button", { name: "批准" });
    expect(screen.queryByLabelText("管理员密码")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "生成 Provision Plan" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "执行 Dry-run" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Stage/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Request UUID/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Root Cause/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Git SHA/i)).not.toBeInTheDocument();

    fireEvent.click(approve);
    await waitFor(() => {
      expect(approveAndProvision).toHaveBeenCalledTimes(1);
    });
    expect(approveAndProvision).toHaveBeenCalledWith(REQUEST_ID, {
      review_note: null,
      idempotency_key: IDEMPOTENCY_KEY,
    });
    expect(reauthenticate).not.toHaveBeenCalled();
  });

  it("combines an expired recent-auth refresh and Approval in the same click", async () => {
    vi.mocked(reauthenticate).mockResolvedValue({ reauthenticated: true });
    vi.mocked(approveAndProvision).mockResolvedValue(
      workflowResult("compute.provision"),
    );
    renderPage(requestFixture("REQUESTED"), currentSession(false));

    const password = await screen.findByLabelText("管理员密码");
    fireEvent.change(password, { target: { value: "fixture password" } });
    fireEvent.click(screen.getByRole("button", { name: "批准" }));
    await waitFor(() => {
      expect(reauthenticate).toHaveBeenCalledTimes(1);
      expect(approveAndProvision).toHaveBeenCalledTimes(1);
    });
    expect(vi.mocked(reauthenticate).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(approveAndProvision).mock.invocationCallOrder[0],
    );
  });

  it("offers one Retry after verified rollback without incident ceremony", async () => {
    vi.mocked(retryProvision).mockResolvedValue(
      workflowResult("compute.provision.retry"),
    );
    renderPage(
      requestFixture("FAILED", { retryAvailable: true }),
      currentSession(true),
    );

    expect(
      await screen.findByText("Container image validation failed"),
    ).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "重试 Provision" });
    expect(screen.queryByLabelText("Retry Reason")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Root Cause")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Remediation Git SHA")).not.toBeInTheDocument();

    fireEvent.click(retry);
    await waitFor(() => expect(retryProvision).toHaveBeenCalledTimes(1));
    expect(retryProvision).toHaveBeenCalledWith(REQUEST_ID, {
      admin_note: null,
      idempotency_key: IDEMPOTENCY_KEY,
    });
    expect(reauthenticate).not.toHaveBeenCalled();
  });

  it("shows Manual Review only for an authoritative UNKNOWN resource state", async () => {
    renderPage(
      requestFixture("FAILED", {
        retryAvailable: false,
        unknown: ["ROOT_WORKER:WORKER_UNAVAILABLE"],
      }),
      currentSession(true),
    );

    expect(await screen.findByTestId("manual-review")).toBeInTheDocument();
    expect(
      screen.getByText("ROOT_WORKER:WORKER_UNAVAILABLE"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重试 Provision" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Reconcile Failed Provision/i }),
    ).not.toBeInTheDocument();
  });

  it("renders simple progress and keeps implementation diagnostics collapsed", async () => {
    renderPage(requestFixture("PROVISIONING"), currentSession(true));

    expect(await screen.findByTestId("provisioning-progress")).toBeInTheDocument();
    expect(screen.getByText("Preparing")).toBeInTheDocument();
    expect(screen.getByText("Validating")).toBeInTheDocument();
    expect(screen.getByText("Creating environment")).toBeInTheDocument();
    expect(screen.getByText("Finalizing")).toBeInTheDocument();
    const diagnostics = screen.getByText("Advanced diagnostics").closest("details");
    expect(diagnostics).not.toHaveAttribute("open");
    expect(screen.queryByText(/Arg13/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/validator hash/i)).not.toBeInTheDocument();
  });

  it("does not expose administrator controls when an ordinary user is denied", async () => {
    vi.mocked(adminComputeRequest).mockRejectedValue(new Error("FORBIDDEN"));
    vi.mocked(me).mockResolvedValue(currentSession(true, "user"));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <ComputeRequestDetailPage />
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/无权查看/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批准" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重试 Provision" }),
    ).not.toBeInTheDocument();
  });
});
