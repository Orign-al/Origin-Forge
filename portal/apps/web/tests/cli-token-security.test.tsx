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

import AccountSecurityPage from "../app/(portal)/account/security/page";
import {
  cliTokens,
  createCliToken,
  me,
  reauthenticate,
  revokeCliToken,
  sessions,
  type CliToken,
  type CurrentSession,
} from "../lib/api";
import { I18nProvider } from "../lib/i18n";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    cliTokens: vi.fn(),
    createCliToken: vi.fn(),
    me: vi.fn(),
    reauthenticate: vi.fn(),
    revokeCliToken: vi.fn(),
    sessions: vi.fn(),
  };
});

const TOKEN_ID = "30000000-0000-4000-8000-000000000001";
const RAW_TOKEN = `h100_cli_${"A".repeat(64)}`;
const token: CliToken = {
  id: TOKEN_ID,
  label: "existing container",
  state: "ACTIVE",
  created_at: "2026-08-30T12:00:00Z",
  expires_at: "2026-11-28T12:00:00Z",
  last_used_at: null,
  revoked_at: null,
  scopes: ["self.jobs.submit", "self.jobs.read", "self.jobs.cancel"],
  owner_bound: true,
};

function current(role: string): CurrentSession {
  return {
    user: {
      id: "20000000-0000-4000-8000-000000000001",
      login_name: role === "user" ? "cli-user" : "origin-al",
      normalized_login: role === "user" ? "cli-user" : "origin-al",
      display_name: "CLI User",
      unix_username: role === "user" ? "cli-user" : "origin-al",
      account_state: "ACTIVE",
      password_state: "SET",
      resource_onboarding_state: "ACTIVE",
      roles: [{ name: role, description: role }],
    },
    role,
    ssh_enrollment: {
      required: false,
      managed_user_id: null,
      compute_identity: null,
      compute_state: "ACTIVE",
      validated_key_count: 1,
      ssh_key_state: "READY",
      setup_path: null,
    },
  };
}

function renderPage(role = "user") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  vi.mocked(me).mockResolvedValue(current(role));
  vi.mocked(sessions).mockResolvedValue([]);
  vi.mocked(cliTokens).mockResolvedValue({
    status: "OK",
    tokens: [token],
    count: 1,
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider initialLocale="zh-CN">
        <AccountSecurityPage />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ordinary-user CLI token security UI", () => {
  it("reauthenticates before create, shows plaintext once, and revokes by opaque ID", async () => {
    vi.mocked(reauthenticate).mockResolvedValue({ reauthenticated: true });
    vi.mocked(createCliToken).mockResolvedValue({
      status: "CREATED",
      token: RAW_TOKEN,
      credential: token,
    });
    vi.mocked(revokeCliToken).mockResolvedValue(undefined);
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "CLI Tokens" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Token名称"), {
      target: { value: "  development container  " },
    });
    fireEvent.change(screen.getAllByLabelText("当前网页密码")[1], {
      target: { value: "fixture password only" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建Token" }));

    await waitFor(() => {
      expect(reauthenticate).toHaveBeenCalledWith("fixture password only");
      expect(createCliToken).toHaveBeenCalledWith({
        label: "development container",
        expires_in_days: 90,
      });
    });
    expect(vi.mocked(reauthenticate).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(createCliToken).mock.invocationCallOrder[0],
    );
    expect(await screen.findByText(RAW_TOKEN)).toBeInTheDocument();

    const existingRow = screen.getByText("existing container").closest("tr");
    expect(existingRow).not.toBeNull();
    fireEvent.click(within(existingRow!).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(revokeCliToken).toHaveBeenCalledWith(TOKEN_ID));
    await waitFor(() =>
      expect(screen.queryByText(RAW_TOKEN)).not.toBeInTheDocument(),
    );
  });

  it("does not expose CLI token controls to an administrator", async () => {
    renderPage("platform_owner");
    expect(
      await screen.findByRole("heading", { name: "账号安全" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "CLI Tokens" }),
    ).not.toBeInTheDocument();
    expect(cliTokens).not.toHaveBeenCalled();
  });
});
