import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FailedProvisionReconciliationPanel,
  failedProvisionReconciliationBinding,
} from "../components/FailedProvisionReconciliationPanel";
import {
  type ComputeResourceRequest,
  adminComputeRequest,
  failedProvisionReconciliationReadiness,
  me,
  reauthenticate,
  reconcileFailedProvision,
} from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    adminComputeRequest: vi.fn(),
    failedProvisionReconciliationReadiness: vi.fn(),
    me: vi.fn(),
    reauthenticate: vi.fn(),
    reconcileFailedProvision: vi.fn(),
  };
});

const REQUEST_ID = "25aafaf9-b4f8-4cb7-beb0-127ed9923d83";
const PLAN_ID = "70c75dac-71ba-47b6-9e4d-fc10dc1dddfb";
const STAGE_ID = "981a7fa1-f246-4e6c-bf70-291537291fb6";

const request: ComputeResourceRequest = {
  id: REQUEST_ID,
  portal_account_id: "10000000-0000-4000-8000-000000000002",
  requested_by: "10000000-0000-4000-8000-000000000002",
  managed_user_id: null,
  username: "origin-pilot2",
  status: "FAILED",
  approval_state: "APPROVED",
  requested_gpu_max: 1,
  requested_storage_bytes: 322122547200,
  requested_container_profile: "STANDARD_8CPU_32GB",
  requested_lease_seconds: 345600,
  purpose: "fixture",
  user_note: null,
  submitted_at: "2026-08-14T00:00:00Z",
  review_note: null,
  reviewed_at: "2026-08-14T00:01:00Z",
  approved_at: "2026-08-14T00:01:00Z",
  rejected_at: null,
  cancelled_at: null,
  created_at: "2026-08-14T00:00:00Z",
  updated_at: "2026-08-15T00:00:00Z",
  retry_authorization_available: false,
  retry_state: "RECONCILIATION_REQUIRED",
  plan: {
    id: PLAN_ID,
    attempt_number: 2,
    attempt_reason: "STAGE_RETRY",
    state: "FAILED",
    username: "origin-pilot2",
    uid: 20002,
    gid: 20002,
    project_id: 30002,
    container_name: "gpu-dev-origin-pilot2",
    container_ssh_port: 22024,
    storage_bytes: 322122547200,
    container_profile: "STANDARD_8CPU_32GB",
    container_cpus: 8,
    container_memory_gb: 32,
    container_pids_limit: 4096,
    container_gpu: 0,
    slurm_account: "company",
    slurm_qos: "general",
    gpu_max: 1,
    lease_seconds: 345600,
    lease_state: "NOT_STARTED",
    host_ssh: "DISABLED",
    shell: "/usr/sbin/nologin",
    password_state: "LOCKED",
    execution_enabled: true,
    reservation_expires_at: "2026-08-16T07:27:00Z",
    dry_run_at: "2026-08-15T07:27:00Z",
    failed_stage_operation_id: STAGE_ID,
  },
  attempts: [
    {
      attempt_number: 2,
      attempt_reason: "STAGE_RETRY",
      plan: undefined as never,
      operations: [],
      stage_operation: {
        id: STAGE_ID,
        operation_type: "compute.provision.stage",
        status: "FAILED",
        started_at: "2026-08-15T07:30:00Z",
        finished_at: "2026-08-15T07:31:00Z",
        error_code: "COMPUTE_STAGE_FAILED",
        rollback_status: "REQUIRES_MANUAL_REVIEW",
        safe_summary: "Container image validation failed",
        safe_root_cause: "Docker default root was rejected",
        side_effect_classification: "PARTIAL_UNKNOWN",
        last_successful_step: "SLURM_ASSOCIATION",
        first_failed_step: "CONTAINER_IMAGE",
        failed_handler: "h100-provision-stage",
      },
      reservations: {
        UID: {
          id: "3fac6fc0-19d7-4fcd-9988-8d11deaceea5",
          state: "FAILED_HOLD",
        },
        GID: {
          id: "86f28884-c6cc-4101-b810-2cbbd8b6065b",
          state: "FAILED_HOLD",
        },
        PROJECT_ID: {
          id: "b0d06339-fcc4-4f42-99e4-6f6d60f05b02",
          state: "FAILED_HOLD",
        },
        SSH_PORT: {
          id: "4331901f-066a-4b3d-ac36-cc6a8a32ea52",
          state: "FAILED_HOLD",
        },
        CONTAINER_NAME: {
          id: "a67a3cdd-405d-48d3-98df-615b78f33ae6",
          state: "FAILED_HOLD",
        },
      },
    },
  ],
};
request.attempts![0]!.plan = request.plan!;

function current(role: string) {
  return {
    user: {
      id: "00000000-0000-4000-8000-000000000001",
      login_name: "origin-al",
      normalized_login: "origin-al",
      display_name: "Origin AL",
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
  };
}

function renderPanel(
  role = "platform_owner",
  readinessStatus: "ZERO_VERIFIED" | "CONFLICT" = "ZERO_VERIFIED",
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const session = current(role);
  client.setQueryData(["me"], session);
  vi.mocked(me).mockResolvedValue(session);
  vi.mocked(failedProvisionReconciliationReadiness).mockResolvedValue({
    status: readinessStatus,
    checked_at: "2026-08-15T14:43:02Z",
    request_id: REQUEST_ID,
    attempt_number: 2,
    plan_id: PLAN_ID,
    failed_stage_operation_id: STAGE_ID,
    rollback_status: "REQUIRES_MANUAL_REVIEW",
    failed_hold_reservations: 5,
    portal_residue: [],
    host_residue: readinessStatus === "CONFLICT" ? ["slurm-association"] : [],
    unknown_resource_state: [],
    script_integrity: "PASS",
    state_changed: false,
    attempt_created: false,
  });
  return render(
    <QueryClientProvider client={client}>
      <FailedProvisionReconciliationPanel request={request} />
    </QueryClientProvider>,
  );
}

function completeConfirmation() {
  fireEvent.change(screen.getByLabelText("输入完整 Request ID 确认"), {
    target: { value: REQUEST_ID },
  });
  fireEvent.change(screen.getByLabelText("输入 Attempt 编号确认"), {
    target: { value: "2" },
  });
  fireEvent.change(screen.getByLabelText("输入完整 Plan ID 确认"), {
    target: { value: PLAN_ID },
  });
  fireEvent.change(screen.getByLabelText("输入完整 Stage Operation ID 确认"), {
    target: { value: STAGE_ID },
  });
  fireEvent.change(screen.getByLabelText("人工对账说明"), {
    target: { value: "RC2 fresh zero-residue evidence reviewed" },
  });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.change(
    screen.getByLabelText("platform_owner 密码（recent re-authentication）"),
    { target: { value: "fixture password only" } },
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("failed Provision reconciliation UI", () => {
  it("binds the exact failed attempt, reauthenticates, refetches, then submits once", async () => {
    vi.mocked(reauthenticate).mockResolvedValue({ reauthenticated: true });
    vi.mocked(adminComputeRequest).mockResolvedValue({
      status: "OK",
      request,
    });
    vi.mocked(reconcileFailedProvision).mockResolvedValue({
      status: "RECONCILED",
      operation_id: "20000000-0000-4000-8000-000000000003",
      idempotent_replay: false,
      attempt_created: false,
      rollback: "VERIFIED",
      reservation_state: "RELEASED",
      request,
    });
    renderPanel();

    expect(
      await screen.findByRole("heading", { name: "失败 Provision 正式对账" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/5 条 Reservation/)).toBeInTheDocument();
    expect(screen.getAllByText(/FAILED_HOLD/).length).toBeGreaterThanOrEqual(5);
    expect(await screen.findByText(/ZERO VERIFIED/)).toBeInTheDocument();
    expect(failedProvisionReconciliationReadiness).toHaveBeenCalledWith(
      REQUEST_ID,
      PLAN_ID,
      STAGE_ID,
    );
    const submit = screen.getByRole("button", {
      name: "Reconcile Failed Provision",
    });
    expect(submit).toBeDisabled();
    completeConfirmation();
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => {
      expect(reauthenticate).toHaveBeenCalledWith("fixture password only");
      expect(adminComputeRequest).toHaveBeenCalledWith(REQUEST_ID);
      expect(reconcileFailedProvision).toHaveBeenCalledWith(REQUEST_ID, {
        plan_id: PLAN_ID,
        failed_stage_operation_id: STAGE_ID,
        review_note: "RC2 fresh zero-residue evidence reviewed",
      });
    });
    expect(vi.mocked(reauthenticate).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(adminComputeRequest).mock.invocationCallOrder[0],
    );
    expect(
      vi.mocked(adminComputeRequest).mock.invocationCallOrder[0],
    ).toBeLessThan(
      vi.mocked(reconcileFailedProvision).mock.invocationCallOrder[0],
    );
    expect(
      await screen.findByText(/20000000-0000-4000-8000-000000000003/),
    ).toBeInTheDocument();
  });

  it("does not expose Reconcile to a non-owner administrator", async () => {
    renderPanel("platform_admin");
    await waitFor(() => expect(me).toHaveBeenCalled());
    expect(
      screen.queryByRole("button", { name: "Reconcile Failed Provision" }),
    ).not.toBeInTheDocument();
  });

  it("fails closed when the pre-submit refetch no longer has five failed holds", async () => {
    const changed = structuredClone(request);
    changed.attempts![0]!.reservations.UID.state = "RELEASED";
    vi.mocked(reauthenticate).mockResolvedValue({ reauthenticated: true });
    vi.mocked(adminComputeRequest).mockResolvedValue({
      status: "OK",
      request: changed,
    });
    renderPanel();
    await screen.findByText(/ZERO VERIFIED/);
    completeConfirmation();
    fireEvent.click(
      screen.getByRole("button", { name: "Reconcile Failed Provision" }),
    );

    expect(
      await screen.findByText(/FAILED_HOLD 已变化；未提交 Reconcile/),
    ).toBeInTheDocument();
    expect(reconcileFailedProvision).not.toHaveBeenCalled();
  });

  it("keeps the formal action disabled when fresh Root Worker evidence conflicts", async () => {
    renderPanel("platform_owner", "CONFLICT");
    expect(
      await screen.findByText(/CONFLICT · slurm-association/),
    ).toBeInTheDocument();
    completeConfirmation();
    expect(
      screen.getByRole("button", { name: "Reconcile Failed Provision" }),
    ).toBeDisabled();
  });

  it("does not render for an ineligible failed attempt", () => {
    const changed = structuredClone(request);
    changed.attempts![0]!.stage_operation!.rollback_status = "ROLLED_BACK";
    expect(failedProvisionReconciliationBinding(changed)).toBeNull();
  });
});
