import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "@h100-portal/api-client";

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
});
