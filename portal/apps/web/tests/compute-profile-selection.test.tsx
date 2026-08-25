import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ComputeRequestPage from "../app/(portal)/compute-request/page";
import {
  type ComputeResourceRequest,
  createSelfComputeRequest,
  selfComputeRequest,
} from "../lib/api";

vi.mock("../lib/random-uuid", () => ({
  randomUuid: () => "10000000-0000-4000-8000-000000000004",
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    createSelfComputeRequest: vi.fn(),
    selfComputeRequest: vi.fn(),
  };
});

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ComputeRequestPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("development container profile selection", () => {
  it("keeps CPU as default and binds GPU Development to exactly one GPU", async () => {
    vi.mocked(selfComputeRequest).mockResolvedValue({
      status: "OK",
      compute_identity: "NOT_PROVISIONED",
      request: null,
    });
    vi.mocked(createSelfComputeRequest).mockResolvedValue({
      status: "REQUESTED",
      request: {} as ComputeResourceRequest,
    });
    renderPage();

    const profile = await screen.findByLabelText("Profile");
    const gpu = screen.getByLabelText("GPU 最大数量");
    expect(profile).toHaveValue("STANDARD_8CPU_32GB");
    expect(gpu).toHaveValue("0");
    expect(gpu).toBeEnabled();

    fireEvent.change(profile, { target: { value: "GPU_1_8CPU_32GB" } });
    expect(gpu).toHaveValue("1");
    expect(gpu).toBeDisabled();
    fireEvent.change(screen.getByLabelText("用途 / Project Description"), {
      target: { value: "Interactive CUDA debugging" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交申请" }));

    await waitFor(() =>
      expect(createSelfComputeRequest).toHaveBeenCalledTimes(1),
    );
    expect(createSelfComputeRequest).toHaveBeenCalledWith({
      requested_gpu_max: 1,
      requested_storage_bytes: 322122547200,
      requested_container_profile: "GPU_1_8CPU_32GB",
      requested_lease_seconds: 345600,
      purpose: "Interactive CUDA debugging",
      user_note: null,
      idempotency_key: "10000000-0000-4000-8000-000000000004",
    });
  });
});
