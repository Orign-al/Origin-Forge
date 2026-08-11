import { describe, expect, it, vi } from "vitest";

import { randomUuid } from "../lib/random-uuid";

describe("randomUuid", () => {
  it("uses the native implementation when the browser exposes it", () => {
    const nativeUuid = "00000000-0000-4000-8000-000000000001";
    const randomUUID = vi.fn(() => nativeUuid);
    const getRandomValues = vi.fn((values: Uint8Array) => values);

    expect(randomUuid({ randomUUID, getRandomValues })).toBe(nativeUuid);
    expect(randomUUID).toHaveBeenCalledOnce();
    expect(getRandomValues).not.toHaveBeenCalled();
  });

  it("builds an RFC 4122 UUID v4 when randomUUID is unavailable on HTTP", () => {
    const getRandomValues = vi.fn((values: Uint8Array) => {
      values.set(Array.from({ length: 16 }, (_unused, index) => index));
      return values;
    });

    expect(randomUuid({ getRandomValues })).toBe(
      "00010203-0405-4607-8809-0a0b0c0d0e0f",
    );
    expect(getRandomValues).toHaveBeenCalledOnce();
  });
});
