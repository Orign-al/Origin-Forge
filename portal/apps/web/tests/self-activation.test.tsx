import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SshKeyEnrollment } from "../components/SshKeyEnrollment";
import {
  activateSelfCompute,
  enrollSshKey,
  syncActiveContainerSshKeys,
  sshKeys,
  type SelfComputeActivationResult,
  type SshKeyList,
} from "../lib/api";

vi.mock("../lib/random-uuid", () => ({
  randomUuid: () => "10000000-0000-4000-8000-000000000004",
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    activateSelfCompute: vi.fn(),
    enrollSshKey: vi.fn(),
    syncActiveContainerSshKeys: vi.fn(),
    sshKeys: vi.fn(),
  };
});

const USER_ID = "10000000-0000-4000-8000-000000000001";
const MANAGED_ID = "10000000-0000-4000-8000-000000000002";
const KEY_ID = "10000000-0000-4000-8000-000000000003";

function keyList(
  scope: "HOST" | "CONTAINER" | "BOTH" = "CONTAINER",
): SshKeyList {
  return {
    status: "OK",
    keys: [
      {
        id: KEY_ID,
        managed_user_id: MANAGED_ID,
        key_type: "ssh-ed25519",
        fingerprint_sha256: "SHA256:owner-container-key",
        comment: "Owner laptop",
        scope,
        state: "VALIDATED",
        generation_method: "IMPORTED",
        created_at: "2026-08-17T08:00:00Z",
        created_by: USER_ID,
        validated_at: "2026-08-17T08:00:00Z",
        installed_at: null,
        revoked_at: null,
      },
    ],
    count: 1,
    maximum_active_keys: 5,
    enrollment: {
      required: true,
      managed_user_id: MANAGED_ID,
      compute_identity: "origin-pilot2",
      compute_state: "STAGED",
      validated_key_count: 1,
      ssh_key_state: "VALIDATED",
      setup_path: "/ssh-keys",
    },
  };
}

function activationResult(): SelfComputeActivationResult {
  return {
    status: "ACTIVE",
    operation_id: "10000000-0000-4000-8000-000000000005",
    idempotent_replay: false,
    environment_state: "ACTIVE",
    container_state: "RUNNING",
    ssh_state: "READY",
    lease: {
      id: "10000000-0000-4000-8000-000000000006",
      state: "ACTIVE",
      starts_at: "2026-08-17T08:00:00Z",
      expires_at: "2026-08-21T08:00:00Z",
      duration_seconds: 345600,
    },
  };
}

function activationInProgress(): SelfComputeActivationResult {
  return {
    status: "ACTIVATING",
    operation_id: "10000000-0000-4000-8000-000000000005",
    idempotent_replay: true,
    environment_state: "STAGED",
    container_state: "ACTIVATING",
    ssh_state: "INSTALLING",
    lease: null,
  };
}

function renderEnrollment(
  options: { containerOnly?: boolean; computeState?: string } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SshKeyEnrollment
        userId={USER_ID}
        username="origin-pilot2"
        computeState={options.computeState ?? "STAGED"}
        containerOnly={options.containerOnly ?? true}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("owner-bound Container activation", () => {
  it("shows one Activate action and no legacy dry-run ceremony", async () => {
    vi.mocked(sshKeys).mockResolvedValue(keyList());
    vi.mocked(activateSelfCompute).mockResolvedValue(activationResult());
    renderEnrollment();

    const activate = await screen.findByRole("button", {
      name: "激活计算环境",
    });
    expect(activate).toBeEnabled();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Staged")).toBeInTheDocument();
    expect(screen.getByText("Starts after activation")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "生成 Activate Dry-Run" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Activate Dry-Run")).not.toBeInTheDocument();
    expect(enrollSshKey).not.toHaveBeenCalled();
  });

  it("submits one owner-scoped request and disables the button while activating", async () => {
    vi.mocked(sshKeys).mockResolvedValue(keyList());
    let finish: ((value: SelfComputeActivationResult) => void) | undefined;
    vi.mocked(activateSelfCompute).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    renderEnrollment();
    const activate = await screen.findByRole("button", {
      name: "激活计算环境",
    });

    fireEvent.click(activate);

    const activating = await screen.findByRole("button", { name: "正在激活…" });
    expect(activating).toBeDisabled();
    expect(activateSelfCompute).toHaveBeenCalledOnce();
    expect(activateSelfCompute).toHaveBeenCalledWith({
      idempotency_key: "10000000-0000-4000-8000-000000000004",
    });
    finish?.(activationResult());
    await waitFor(() =>
      expect(screen.getByText(/计算环境已激活/u)).toBeInTheDocument(),
    );
  });

  it("does not offer Activate without a valid Container-scope key", async () => {
    vi.mocked(sshKeys).mockResolvedValue(keyList("HOST"));
    renderEnrollment();

    expect(
      await screen.findByText("VALIDATED — NOT INSTALLED"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "激活计算环境" }),
    ).not.toBeInTheDocument();
    expect(activateSelfCompute).not.toHaveBeenCalled();
  });

  it("treats an existing in-flight activation as progress, not a second action", async () => {
    vi.mocked(sshKeys).mockResolvedValue(keyList());
    vi.mocked(activateSelfCompute).mockResolvedValue(activationInProgress());
    renderEnrollment();

    fireEvent.click(
      await screen.findByRole("button", { name: "激活计算环境" }),
    );

    expect(
      await screen.findByText(/计算环境正在激活；Lease 尚未开始/u),
    ).toBeInTheDocument();
    expect(activateSelfCompute).toHaveBeenCalledOnce();
    expect(screen.queryByText(/计算环境激活失败/u)).not.toBeInTheDocument();
  });

  it("does not expose self activation in the administrator key component", async () => {
    vi.mocked(sshKeys).mockResolvedValue(keyList());
    renderEnrollment({ containerOnly: false });

    expect(
      await screen.findByText("VALIDATED — NOT INSTALLED"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "激活计算环境" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId("self-activation")).not.toBeInTheDocument();
  });

  it("does not offer activation once the environment is ACTIVE", async () => {
    vi.mocked(sshKeys).mockResolvedValue(keyList());
    vi.mocked(syncActiveContainerSshKeys).mockResolvedValue({
      status: "INSTALLED",
      operation_id: "10000000-0000-4000-8000-000000000005",
      container_state: "RUNNING",
      key_fingerprints: ["SHA256:owner-container-key"],
      idempotent_replay: false,
    });
    renderEnrollment({ computeState: "ACTIVE" });

    expect(
      await screen.findByText(/1 把公钥可用于开发容器/u),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "激活计算环境" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "安装新增密钥" }));
    await waitFor(() =>
      expect(syncActiveContainerSshKeys).toHaveBeenCalledWith(USER_ID, {
        idempotency_key: "10000000-0000-4000-8000-000000000004",
      }),
    );
    expect(
      await screen.findByText("新增 SSH 公钥已安装到运行中的开发容器。"),
    ).toBeInTheDocument();
  });
});
