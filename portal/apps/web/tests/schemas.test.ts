import { describe, expect, it } from "vitest";

import {
  loginSchema,
  passwordActionExchangeSchema,
  setupPasswordSchema,
} from "@h100-portal/schemas";

describe("browser validation schemas", () => {
  it("accepts a long Unicode passphrase without trimming it", () => {
    const password = "这是一个足够长的网页平台口令  保留空格";
    const parsed = setupPasswordSchema.parse({
      password,
      confirmation: password,
    });
    expect(parsed.password).toBe(password);
  });

  it("rejects mismatched password confirmation", () => {
    expect(() =>
      setupPasswordSchema.parse({
        password: "A sufficiently long portal passphrase",
        confirmation: "A different sufficiently long passphrase",
      }),
    ).toThrow();
  });

  it("validates the one-time bearer separately from password input", () => {
    expect(
      passwordActionExchangeSchema.safeParse({
        token: "test-only-token-with-more-than-thirty-two-characters",
      }).success,
    ).toBe(true);
    expect(
      "token" in
        setupPasswordSchema.parse({
          password: "A sufficiently long portal passphrase",
          confirmation: "A sufficiently long portal passphrase",
        }),
    ).toBe(false);
  });

  it("requires both login fields", () => {
    expect(loginSchema.safeParse({ username: "", password: "" }).success).toBe(
      false,
    );
  });
});
