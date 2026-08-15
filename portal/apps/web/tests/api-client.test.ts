import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "@h100-portal/api-client";
import {
  closeSelfTerminal,
  enrollSshKey,
  reconcileFailedProvision,
  resizeSelfTerminal,
  retryLeaseRecycle,
  sendSelfTerminalInput,
  startManagedContainer,
  startSelfTerminal,
} from "../lib/api";

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = "h100_csrf=; Max-Age=0; Path=/";
});

describe("API client", () => {
  it("adds the CSRF token to state-changing requests", async () => {
    document.cookie = "h100_csrf=test-csrf-value; Path=/";
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, init?: RequestInit) => {
        expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe(
          "test-csrf-value",
        );
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      apiFetch<{ ok: boolean }>("/test", {
        method: "POST",
        body: JSON.stringify({}),
      }),
    ).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("binds formal reconciliation to CSRF and exact failed-stage identifiers", async () => {
    document.cookie = "h100_csrf=test-csrf-value; Path=/";
    const requestId = "25aafaf9-b4f8-4cb7-beb0-127ed9923d83";
    const planId = "70c75dac-71ba-47b6-9e4d-fc10dc1dddfb";
    const stageId = "981a7fa1-f246-4e6c-bf70-291537291fb6";
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        expect(String(input)).toBe(
          `/api/v1/admin/compute-resource-requests/${requestId}/reconcile-failed-provision`,
        );
        expect(init?.method).toBe("POST");
        expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe(
          "test-csrf-value",
        );
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        expect(body.plan_id).toBe(planId);
        expect(body.failed_stage_operation_id).toBe(stageId);
        expect(body.review_note).toBe("fresh zero-residue evidence reviewed");
        expect(body.idempotency_key).toMatch(
          /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u,
        );
        expect(body).not.toHaveProperty("password");
        expect(body).not.toHaveProperty("session");
        expect(body).not.toHaveProperty("csrf");
        return new Response(
          JSON.stringify({
            status: "RECONCILED",
            operation_id: "20000000-0000-4000-8000-000000000003",
            idempotent_replay: false,
            attempt_created: false,
            rollback: "VERIFIED",
            reservation_state: "RELEASED",
            request: {},
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    await reconcileFailedProvision(requestId, {
      plan_id: planId,
      failed_stage_operation_id: stageId,
      review_note: "fresh zero-residue evidence reviewed",
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("does not invent a successful response for API failures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              detail: { code: "SOURCE_UNAVAILABLE", message: "数据源不可用" },
            }),
            { status: 503, headers: { "Content-Type": "application/json" } },
          ),
      ),
    );

    await expect(apiFetch("/platform/overview")).rejects.toEqual(
      expect.objectContaining({
        status: 503,
        code: "SOURCE_UNAVAILABLE",
        message: "数据源不可用",
      }),
    );
  });

  it("sends only public material in an SSH key enrollment request", async () => {
    document.cookie = "h100_csrf=test-csrf-value; Path=/";
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, init?: RequestInit) => {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        expect(Object.keys(body).sort()).toEqual([
          "client_fingerprint_sha256",
          "comment",
          "confirmed_private_key_saved",
          "generation_method",
          "key_type",
          "public_key",
          "scope",
        ]);
        expect(body).not.toHaveProperty("private_key");
        expect(body).not.toHaveProperty("private_key_password");
        expect(body).not.toHaveProperty("private_key_path");
        return new Response(
          JSON.stringify({
            status: "VALIDATED",
            key: {},
            private_key_received: false,
            authorized_keys_installed: false,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    await enrollSshKey("00000000-0000-4000-8000-000000000001", {
      key_type: "ssh-ed25519",
      public_key: "ssh-ed25519 TEST-ONLY-PUBLIC-BLOB Browser test",
      comment: "Browser test",
      scope: "BOTH",
      generation_method: "BROWSER_GENERATED",
      client_fingerprint_sha256: "SHA256:test-only",
      confirmed_private_key_saved: true,
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("uses the closed managed-container start contract", async () => {
    document.cookie = "h100_csrf=test-csrf-value; Path=/";
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        expect(String(input)).toContain(
          "/containers/gpu-dev-origin-pilot/start",
        );
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        expect(body).toEqual({
          idempotency_key: "00000000-0000-4000-8000-000000000099",
          expected_compute_state: "ACTIVE",
          expected_container_state: "STOPPED",
          expected_ssh_key_state: "INSTALLED",
        });
        expect(body).not.toHaveProperty("command");
        expect(body).not.toHaveProperty("argv");
        expect(body).not.toHaveProperty("path");
        return new Response(
          JSON.stringify({
            status: "SUCCEEDED",
            operation_id: "00000000-0000-4000-8000-000000000098",
            container: {
              name: "gpu-dev-origin-pilot",
              state: "RUNNING",
              gpu: "NONE",
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    await startManagedContainer("gpu-dev-origin-pilot", {
      idempotency_key: "00000000-0000-4000-8000-000000000099",
      expected_compute_state: "ACTIVE",
      expected_container_state: "STOPPED",
      expected_ssh_key_state: "INSTALLED",
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("uses only self-scoped terminal session endpoints", async () => {
    document.cookie = "h100_csrf=test-csrf-value; Path=/";
    const requests: Array<{ url: string; method: string; body: unknown }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        requests.push({
          url: String(input),
          method: String(init?.method ?? "GET"),
          body: init?.body ? JSON.parse(String(init.body)) : null,
        });
        return new Response(
          JSON.stringify({ status: "ACCEPTED", terminal: {} }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }),
    );

    await startSelfTerminal({
      idempotency_key: "00000000-0000-4000-8000-000000000091",
      cols: 120,
      rows: 32,
    });
    await sendSelfTerminalInput(
      "00000000-0000-4000-8000-000000000092",
      "pwd\r",
    );
    await resizeSelfTerminal("00000000-0000-4000-8000-000000000092", 132, 40);
    await closeSelfTerminal("00000000-0000-4000-8000-000000000092");

    expect(requests).toEqual([
      {
        url: "/api/v1/self/container/terminal/sessions",
        method: "POST",
        body: {
          idempotency_key: "00000000-0000-4000-8000-000000000091",
          cols: 120,
          rows: 32,
        },
      },
      {
        url: "/api/v1/self/container/terminal/sessions/00000000-0000-4000-8000-000000000092/input",
        method: "POST",
        body: { data: "pwd\r" },
      },
      {
        url: "/api/v1/self/container/terminal/sessions/00000000-0000-4000-8000-000000000092/resize",
        method: "POST",
        body: { cols: 132, rows: 40 },
      },
      {
        url: "/api/v1/self/container/terminal/sessions/00000000-0000-4000-8000-000000000092",
        method: "DELETE",
        body: null,
      },
    ]);
    for (const request of requests) {
      expect(request.url).not.toMatch(/containers\/[^/]+/u);
      expect(request.body).not.toEqual(
        expect.objectContaining({ command: expect.anything() }),
      );
    }
  });

  it("binds Lease recovery to CSRF and the typed Lease ID without browser secrets", async () => {
    document.cookie = "h100_csrf=test-csrf-value; Path=/";
    const leaseId = "54314628-7b46-4986-bfbd-895f97e0e70f";
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        expect(String(input)).toBe(
          `/api/v1/admin/compute-leases/${leaseId}/recycle-retry`,
        );
        expect(init?.method).toBe("POST");
        expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe(
          "test-csrf-value",
        );
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        expect(body.confirmation).toBe(leaseId);
        expect(body.safe_reason).toBe("approved fixture recovery");
        expect(body.idempotency_key).toMatch(
          /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u,
        );
        expect(body).not.toHaveProperty("password");
        expect(body).not.toHaveProperty("session");
        expect(body).not.toHaveProperty("csrf");
        return new Response(
          JSON.stringify({
            status: "SUCCEEDED",
            operation_id: "30000000-0000-4000-8000-000000000001",
            lease_id: leaseId,
            idempotent_replay: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    await retryLeaseRecycle(leaseId, {
      confirmation: leaseId,
      safe_reason: "approved fixture recovery",
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });
});
