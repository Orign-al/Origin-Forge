type BrowserCrypto = {
  getRandomValues(values: Uint8Array): Uint8Array;
  randomUUID?: () => string;
};

export function randomUuid(source?: BrowserCrypto): string {
  const provider = source ?? globalThis.crypto;
  if (!provider || typeof provider.getRandomValues !== "function") {
    throw new Error("Browser cryptographic random source is unavailable");
  }
  if (typeof provider.randomUUID === "function") {
    return provider.randomUUID();
  }

  // randomUUID() is unavailable on non-local HTTP origins, while the Web Crypto
  // getRandomValues() primitive remains available and is suitable for UUID v4.
  const bytes = provider.getRandomValues(new Uint8Array(16));
  const versionByte = bytes.at(6);
  const variantByte = bytes.at(8);
  if (versionByte === undefined || variantByte === undefined) {
    throw new Error(
      "Browser cryptographic random source returned invalid data",
    );
  }
  bytes[6] = (versionByte & 0x0f) | 0x40;
  bytes[8] = (variantByte & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}
