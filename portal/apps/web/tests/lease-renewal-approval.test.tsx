import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import LeaseRenewalsPage from "../app/(portal)/lease-renewals/page";
import {
  type AdminLeaseRenewalRequest,
  type CurrentSession,
  adminLeaseRenewals,
  decideLeaseRenewal,
  me,
  reauthenticate,
} from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    adminLeaseRenewals: vi.fn(),
    decideLeaseRenewal: vi.fn(),
    me: vi.fn(),
    reauthenticate: vi.fn(),
  };
});

const pending: AdminLeaseRenewalRequest = {
  id: "10000000-0000-4000-8000-000000000001",
  owner_managed_user_id: "20000000-0000-4000-8000-000000000001",
  username: "luchenxi",
  display_name: "Lu Chenxi",
  lease_id: "30000000-0000-4000-8000-000000000001",
  lease_state: "RENEWAL_PENDING",
  lease_expires_at: "2026-09-06T14:50:00Z",
  state: "REQUESTED",
  approval_required: true,
  duration_seconds: 345600,
  requested_at: "2026-09-06T03:02:58Z",
  decided_at: null,
  decision_comment: null,
  resulting_lease_id: null,
  actionable: true,
  closed_reason: null,
};

function session(role: string, recentAuthValid = false): CurrentSession {
  return {
    user: {
      id: "40000000-0000-4000-8000-000000000001",
      login_name: role,
      normalized_login: role,
      display_name: role,
      unix_username: role,
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
    recent_auth_valid_until: null,
  };
}

function renderPage(
  role: string,
  requests: AdminLeaseRenewalRequest[] = [pending],
  recentAuthValid = false,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const current = session(role, recentAuthValid);
  const result = {
    status: "OK" as const,
    requests,
    count: requests.length,
    pending_count: requests.filter((row) => row.actionable).length,
  };
  vi.mocked(me).mockResolvedValue(current);
  vi.mocked(adminLeaseRenewals).mockResolvedValue(result);
  return render(
    <QueryClientProvider client={client}>
      <LeaseRenewalsPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("administrator lease renewal approval", () => {
  it("shows a submitted renewal and reauthenticates before approval", async () => {
    vi.mocked(reauthenticate).mockResolvedValue({ reauthenticated: true });
    vi.mocked(decideLeaseRenewal).mockResolvedValue({
      status: "APPROVED",
      renewal_request_id: pending.id,
      resulting_lease_id: "50000000-0000-4000-8000-000000000001",
    });
    renderPage("platform_owner");

    expect(
      await screen.findByRole("heading", { name: "续期审批" }),
    ).toBeInTheDocument();
    expect(screen.getByText("luchenxi · Lu Chenxi")).toBeInTheDocument();
    expect(screen.getByText("RENEWAL_PENDING")).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: "批准续期" });
    expect(approve).toBeDisabled();
    fireEvent.change(screen.getByLabelText("管理员密码"), {
      target: { value: "fixture password only" },
    });
    fireEvent.change(screen.getByLabelText("审批备注（可选）"), {
      target: { value: "  approved after review  " },
    });
    fireEvent.click(approve);

    await waitFor(() => {
      expect(reauthenticate).toHaveBeenCalledWith("fixture password only");
      expect(decideLeaseRenewal).toHaveBeenCalledWith(
        pending.id,
        "APPROVE",
        "approved after review",
      );
    });
    expect(vi.mocked(reauthenticate).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(decideLeaseRenewal).mock.invocationCallOrder[0],
    );
    expect(
      await screen.findByText(
        "续期已批准；后续 Lease 将在当前 Lease 到期时自动生效。",
      ),
    ).toBeInTheDocument();
  });

  it("keeps the auditor view read-only", async () => {
    renderPage("auditor");

    expect(await screen.findByText("luchenxi · Lu Chenxi")).toBeInTheDocument();
    expect(
      screen.getByText("当前角色仅可查看，不能处理续期申请。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "批准续期" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("管理员密码")).not.toBeInTheDocument();
  });

  it("labels an expired requested row as non-actionable history", async () => {
    renderPage("platform_admin", [
      {
        ...pending,
        username: "historical-user",
        lease_state: "RECYCLE_BIN",
        lease_expires_at: "2026-09-01T02:00:00Z",
        actionable: false,
        closed_reason: "LEASE_EXPIRED_RESTORE_REQUIRED",
      },
    ]);

    expect(await screen.findByText("historical-user")).toBeInTheDocument();
    expect(screen.getByText("已失效")).toBeInTheDocument();
    expect(
      screen.getByText("原 Lease 已到期，请使用恢复流程"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "批准续期" }),
    ).not.toBeInTheDocument();
  });
});
