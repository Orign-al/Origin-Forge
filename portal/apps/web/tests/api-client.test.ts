import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "@h100-portal/api-client";
import {
  closeSelfTerminal,
  enrollSshKey,
  resizeSelfTerminal,
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
});
