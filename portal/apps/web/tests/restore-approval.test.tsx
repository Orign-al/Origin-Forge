import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import RestoreRequestsPage from "../app/(portal)/restore-requests/page";
import {
  type AdminRestoreRequest,
  type CurrentSession,
  adminRestoreRequests,
  decideRestoreRequest,
  me,
  reauthenticate,
} from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    adminRestoreRequests: vi.fn(),
    decideRestoreRequest: vi.fn(),
    me: vi.fn(),
    reauthenticate: vi.fn(),
  };
});

const pending: AdminRestoreRequest = {
  id: "10000000-0000-4000-8000-000000000001",
  username: "luchenxi",
  resource_name: "gpu-dev-luchenxi",
  state: "REQUESTED",
  approval_required: true,
  duration_seconds: 345600,
  requested_at: "2026-09-07T02:01:13Z",
  decided_at: null,
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

function renderPage(role: string, recentAuthValid = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  vi.mocked(me).mockResolvedValue(session(role, recentAuthValid));
  vi.mocked(adminRestoreRequests).mockResolvedValue({
    status: "OK",
    requests: [pending],
    count: 1,
  });
  return render(
    <QueryClientProvider client={client}>
      <RestoreRequestsPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("administrator restore approval", () => {
  it("shows a policy-bound restore and reauthenticates before approval", async () => {
    vi.mocked(reauthenticate).mockResolvedValue({ reauthenticated: true });
    vi.mocked(decideRestoreRequest).mockResolvedValue({
      status: "RESTORED",
      restore_request_id: pending.id,
      lease_id: "50000000-0000-4000-8000-000000000001",
    });
    renderPage("platform_owner");

    expect(
      await screen.findByRole("heading", { name: "恢复审批" }),
    ).toBeInTheDocument();
    expect(screen.getByText("luchenxi · gpu-dev-luchenxi")).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: "批准恢复" });
    expect(approve).toBeDisabled();
    fireEvent.change(screen.getByLabelText("管理员密码"), {
      target: { value: "fixture password only" },
    });
    fireEvent.change(screen.getByLabelText("备注（可选）"), {
      target: { value: "  reviewed restore  " },
    });
    fireEvent.click(approve);

    await waitFor(() => {
      expect(reauthenticate).toHaveBeenCalledWith("fixture password only");
      expect(decideRestoreRequest).toHaveBeenCalledWith(
        pending.id,
        "APPROVE",
        "reviewed restore",
      );
    });
    expect(vi.mocked(reauthenticate).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(decideRestoreRequest).mock.invocationCallOrder[0],
    );
    expect(
      await screen.findByText("恢复已完成；新 Lease 已按平台硬上限创建。"),
    ).toBeInTheDocument();
  });

  it("keeps the auditor view read-only", async () => {
    renderPage("auditor");

    expect(
      await screen.findByText("luchenxi · gpu-dev-luchenxi"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("当前角色仅可查看，不能处理恢复申请。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "批准恢复" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("管理员密码")).not.toBeInTheDocument();
  });
});
