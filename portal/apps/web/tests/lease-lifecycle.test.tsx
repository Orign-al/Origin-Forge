import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  LeaseLifecyclePanel,
  recoveryEligible,
} from "../components/LeaseLifecyclePanel";
import {
  type AdminLeaseRecoveryIncident,
  type User,
  adminLeaseRecoveryIncidents,
  me,
  reauthenticate,
  retryLeaseRecycle,
  updateLeaseRenewalPolicy,
} from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    adminLeaseRecoveryIncidents: vi.fn(),
    me: vi.fn(),
    reauthenticate: vi.fn(),
    retryLeaseRecycle: vi.fn(),
    updateLeaseRenewalPolicy: vi.fn(),
  };
});

const LEASE_ID = "54314628-7b46-4986-bfbd-895f97e0e70f";
const USER_ID = "10000000-0000-4000-8000-000000000001";
const OPERATION_ID = "90964d2b-c1e0-4c12-a036-2b49c11d8558";

const user: User = {
  id: USER_ID,
  login_name: "origin-pilot",
  normalized_login: "origin-pilot",
  display_name: "Origin Pilot",
  unix_username: "origin-pilot",
  account_state: "ACTIVE",
  password_state: "SET",
  resource_onboarding_state: "ACTIVE",
  roles: [{ name: "user", description: "普通用户" }],
  compute_lifecycle: {
    has_lease: true,
    lease_id: LEASE_ID,
    owner: "origin-pilot",
    starts_at: "2026-08-10T05:24:55.083442Z",
    expires_at: "2026-08-14T05:24:55.083442Z",
    time_expired: true,
    lease_state: "ACTIVE",
    renewal_approval_required: true,
  },
  linux_identity: {
    managed_user_id: "20000000-0000-4000-8000-000000000001",
    unix_username: "origin-pilot",
    compute_environment_state: "ACTIVE",
  },
};

const failedIncident: AdminLeaseRecoveryIncident = {
  lease_id: LEASE_ID,
  portal_user_id: USER_ID,
  username: "origin-pilot",
  owner: "origin-pilot",
  starts_at: "2026-08-10T05:24:55.083442Z",
  expires_at: "2026-08-14T05:24:55.083442Z",
  current_time: "2026-08-15T00:00:00Z",
  time_expired: true,
  lease_state: "ACTIVE",
  recycle_state: "FAILED",
  last_transition_at: "2026-08-14T05:25:00Z",
  compute_environment_state: "ACTIVE",
  container_name: "gpu-dev-origin-pilot",
  container_desired_state: "RUNNING",
  container_observed_state: "RUNNING",
  connection_authorization_state: "DENIED_EXPIRED_LEASE",
  container_ssh_authorization_state: "KEY_INSTALLED",
  operation_id: OPERATION_ID,
  operation_type: "lease.expire",
  operation_status: "FAILED",
  operation_started_at: "2026-08-14T05:24:55Z",
  operation_finished_at: "2026-08-14T05:25:00Z",
  error_code: "CONTAINER_STOP_FAILED",
  safe_error_message: "到期回收未完成；需要平台所有者人工确认后重试。",
  attempt_count: null,
  next_retry_at: null,
  manual_review_required: true,
  recovery_available: true,
  data_delete_allowed: false,
};

function session(role: string) {
  return {
    user: {
      ...user,
      id: "00000000-0000-4000-8000-000000000001",
      normalized_login: role === "platform_owner" ? "origin-al" : "viewer",
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
  role: string,
  incidents: AdminLeaseRecoveryIncident[],
  target = user,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const current = session(role);
  const incidentResult = { status: "OK", incidents, count: incidents.length };
  client.setQueryData(["me"], current);
  client.setQueryData(["admin-lease-recovery-incidents"], incidentResult);
  vi.mocked(me).mockResolvedValue(current);
  vi.mocked(adminLeaseRecoveryIncidents).mockResolvedValue(incidentResult);
  return render(
    <QueryClientProvider client={client}>
      <LeaseLifecyclePanel user={target} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("administrator Lease lifecycle UI", () => {
  it("updates the per-user renewal and restore approval policy without changing pending requests", async () => {
    vi.mocked(updateLeaseRenewalPolicy).mockResolvedValue({
      status: "UPDATED",
      policy: {
        approval_required: false,
        pending_requests_changed: false,
      },
    });
    renderPanel("platform_owner", []);

    const toggle = await screen.findByRole("checkbox", {
      name: "续期与恢复需要管理员审批",
    });
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(updateLeaseRenewalPolicy).toHaveBeenCalledWith(USER_ID, false);
    });
    expect(
      await screen.findByText(
        "该用户后续续期与恢复申请将在服务端校验后自动批准。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "同时控制到期前续期和回收站恢复；只影响后续新申请，已有待审批申请保持不变。",
      ),
    ).toBeInTheDocument();
  });

  it("does not let an ordinary user change another policy", async () => {
    renderPanel("user", []);
    const toggle = await screen.findByRole("checkbox", {
      name: "续期与恢复需要管理员审批",
    });
    expect(toggle).toBeDisabled();
    fireEvent.click(toggle);
    expect(updateLeaseRenewalPolicy).not.toHaveBeenCalled();
  });

  it("shows the expired ACTIVE anomaly and binds a typed, reauthenticated recovery", async () => {
    vi.mocked(reauthenticate).mockResolvedValue({ reauthenticated: true });
    vi.mocked(retryLeaseRecycle).mockResolvedValue({
      status: "SUCCEEDED",
      operation_id: "30000000-0000-4000-8000-000000000001",
      lease_id: LEASE_ID,
      idempotent_replay: false,
    });
    renderPanel("platform_owner", [failedIncident]);

    expect(await screen.findByText("Lease 生命周期异常")).toBeInTheDocument();
    expect(screen.getByText(LEASE_ID)).toBeInTheDocument();
    expect(screen.getByText("CONTAINER_STOP_FAILED")).toBeInTheDocument();
    expect(screen.getByText("DENIED_EXPIRED_LEASE")).toBeInTheDocument();

    const openRecovery = screen.getByRole("button", { name: "重试回收" });
    await waitFor(() => expect(openRecovery).toBeEnabled());
    fireEvent.click(openRecovery);
    const dialog = await screen.findByRole("dialog", {
      name: "确认重试回收",
    });
    const submit = within(dialog).getByRole("button", {
      name: "确认重试回收",
    });
    expect(submit).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("输入完整 Lease ID 确认"), {
      target: { value: LEASE_ID },
    });
    fireEvent.change(within(dialog).getByLabelText("Recovery 原因"), {
      target: { value: "approved lifecycle recovery fixture" },
    });
    fireEvent.change(
      within(dialog).getByLabelText(
        "platform_owner 密码（recent re-authentication）",
      ),
      { target: { value: "fixture password only" } },
    );
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => {
      expect(reauthenticate).toHaveBeenCalledWith("fixture password only");
      expect(retryLeaseRecycle).toHaveBeenCalledWith(LEASE_ID, {
        confirmation: LEASE_ID,
        safe_reason: "approved lifecycle recovery fixture",
      });
    });
    expect(vi.mocked(reauthenticate).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(retryLeaseRecycle).mock.invocationCallOrder[0],
    );
    expect(
      await screen.findByText("30000000-0000-4000-8000-000000000001"),
    ).toBeInTheDocument();
  });

  it("does not expose Recovery to an ordinary user", async () => {
    renderPanel("user", [failedIncident]);
    expect(await screen.findByText(LEASE_ID)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重试回收" }),
    ).not.toBeInTheDocument();
    expect(adminLeaseRecoveryIncidents).not.toHaveBeenCalled();
  });

  it("does not offer Recovery for a normal active Lease", async () => {
    const activeUser: User = {
      ...user,
      compute_lifecycle: {
        ...user.compute_lifecycle!,
        time_expired: false,
        lease_state: "ACTIVE",
      },
    };
    renderPanel("platform_owner", [], activeUser);
    expect(await screen.findByText("有效期内")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重试回收" }),
    ).not.toBeInTheDocument();
  });

  it("does not offer Recovery after the Lease reaches RECYCLE_BIN", async () => {
    const recycled = {
      ...failedIncident,
      lease_state: "RECYCLE_BIN",
      recycle_state: "RECYCLE_BIN",
      operation_status: "SUCCEEDED",
      recovery_available: false,
      manual_review_required: false,
    };
    renderPanel("platform_owner", [recycled]);
    expect((await screen.findAllByText("RECYCLE_BIN")).length).toBeGreaterThan(
      0,
    );
    expect(recoveryEligible(recycled)).toBe(false);
    expect(
      screen.queryByRole("button", { name: "重试回收" }),
    ).not.toBeInTheDocument();
  });
});
