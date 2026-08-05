import { describe, expect, it } from "vitest";

import { loginSchema, setupPasswordSchema } from "@h100-portal/schemas";

describe("browser validation schemas", () => {
  it("accepts a long Unicode passphrase without trimming it", () => {
    const password = "这是一个足够长的网页平台口令  保留空格";
    const parsed = setupPasswordSchema.parse({
      token: "test-only-token-with-more-than-thirty-two-characters",
      password,
      confirmation: password,
    });
    expect(parsed.password).toBe(password);
  });

  it("rejects mismatched password confirmation", () => {
    expect(() =>
      setupPasswordSchema.parse({
        token: "test-only-token-with-more-than-thirty-two-characters",
        password: "A sufficiently long portal passphrase",
        confirmation: "A different sufficiently long passphrase",
      }),
    ).toThrow();
  });

  it("requires both login fields", () => {
    expect(loginSchema.safeParse({ username: "", password: "" }).success).toBe(
      false,
    );
  });
});
