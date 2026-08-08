import {
  chmodSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";

import { describe, expect, it } from "vitest";

import {
  containsPrivateKeyMaterial,
  generateOpenSshEd25519,
  parseOpenSshPublicKey,
} from "../lib/ssh-key";

describe("browser SSH key lifecycle", () => {
  it("generates an OpenSSH-compatible ED25519 key entirely in memory", () => {
    const generated = generateOpenSshEd25519(
      "origin-pilot",
      "Browser test key",
    );
    const directory = mkdtempSync(path.join(tmpdir(), "portal-ssh-key-test-"));
    const privatePath = path.join(directory, "id_ed25519");
    try {
      writeFileSync(privatePath, generated.privateKey, {
        encoding: "utf8",
        mode: 0o600,
      });
      chmodSync(privatePath, 0o600);
      const derived = execFileSync(
        "/usr/bin/ssh-keygen",
        ["-y", "-f", privatePath],
        {
          encoding: "utf8",
          stdio: ["ignore", "pipe", "ignore"],
        },
      ).trim();
      expect(derived.split(/\s+/u).slice(0, 2)).toEqual(
        generated.publicKey.split(/\s+/u).slice(0, 2),
      );
      expect(generated.privateFileName).toBe("h100_origin-pilot_ed25519");
      expect(generated.publicFileName).toBe("h100_origin-pilot_ed25519.pub");
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it("computes the same SHA-256 fingerprint when the public key is parsed", () => {
    const generated = generateOpenSshEd25519(
      "origin-pilot",
      "Fingerprint test",
    );
    const parsed = parseOpenSshPublicKey(`${generated.publicKey}\n`);
    expect(parsed.keyType).toBe("ssh-ed25519");
    expect(parsed.fingerprintSha256).toBe(generated.fingerprintSha256);
    expect(parsed.publicKey).toBe(generated.publicKey);
  });

  it("rejects pasted OpenSSH private-key armor", () => {
    const generated = generateOpenSshEd25519(
      "origin-pilot",
      "Rejected private key",
    );
    expect(containsPrivateKeyMaterial(generated.privateKey)).toBe(true);
    expect(() => parseOpenSshPublicKey(generated.privateKey)).toThrow(
      "检测到私钥内容",
    );
  });

  it("rejects generic encrypted private-key armor", () => {
    expect(
      containsPrivateKeyMaterial(
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nforbidden\n-----END ENCRYPTED PRIVATE KEY-----",
      ),
    ).toBe(true);
  });

  it("does not reference persistent browser storage or telemetry", () => {
    const source = readFileSync(
      path.resolve(process.cwd(), "lib/ssh-key.ts"),
      "utf8",
    );
    for (const forbidden of [
      "localStorage",
      "sessionStorage",
      "indexedDB",
      "serviceWorker",
      "analytics",
      "Sentry",
      "fetch(",
    ]) {
      expect(source).not.toContain(forbidden);
    }
  });
});
