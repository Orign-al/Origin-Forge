import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import LoginPage from "../app/login/page";
import { getCsrf, login, type SshEnrollment, type User } from "../lib/api";

const navigation = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: navigation.replace }),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    getCsrf: vi.fn(),
    login: vi.fn(),
  };
});

const userB: User = {
  id: "50000000-0000-4000-8000-000000000002",
  login_name: "fixture-user-b",
  normalized_login: "fixture-user-b",
  display_name: "Fixture User B",
  unix_username: "fixture-user-b",
  account_state: "ACTIVE",
  password_state: "SET",
  resource_onboarding_state: "ACTIVE",
  roles: [{ name: "user", description: "普通用户" }],
};

const enrollment: SshEnrollment = {
  required: false,
  managed_user_id: "51000000-0000-4000-8000-000000000002",
  compute_identity: "fixture-user-b",
  compute_state: "ACTIVE",
  validated_key_count: 1,
  ssh_key_state: "SSH_READY",
  setup_path: null,
};

describe("frontend authentication cache isolation", () => {
  beforeEach(() => {
    navigation.replace.mockReset();
    vi.mocked(getCsrf).mockResolvedValue({ csrf_token: "fixture-csrf" });
    vi.mocked(login).mockResolvedValue({
      user: userB,
      ssh_enrollment: enrollment,
    });
  });

  afterEach(() => cleanup());

  it("drops every previous owner query before navigating after a new login", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 60_000 } },
    });
    queryClient.setQueryData(["me"], {
      user: { ...userB, id: "50000000-0000-4000-8000-000000000001" },
    });
    queryClient.setQueryData(["self-jobs"], [{ name: "user-a-private-job" }]);
    queryClient.setQueryData(["self-job-logs", "user-a-job"], "private log");
    queryClient.setQueryData(["ssh-keys", "user-a"], ["SHA256:user-a"]);
    queryClient.setQueryData(["self-environment"], { owner: "user-a" });
    queryClient.setQueryData(["self-container"], { owner: "user-a" });
    queryClient.setQueryData(["self-lease"], { owner: "user-a" });

    render(
      <QueryClientProvider client={queryClient}>
        <LoginPage />
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "登录" })).toBeEnabled(),
    );
    fireEvent.change(screen.getByLabelText("登录名"), {
      target: { value: "fixture-user-b" },
    });
    fireEvent.change(screen.getByLabelText("网页密码"), {
      target: { value: "fixture password only" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() =>
      expect(login).toHaveBeenCalledWith({
        username: "fixture-user-b",
        password: "fixture password only",
      }),
    );
    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/"));
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });
});
