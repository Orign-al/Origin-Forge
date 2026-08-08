import * as ed25519 from "@noble/ed25519";
import { sha256, sha512 } from "@noble/hashes/sha2.js";

ed25519.hashes.sha512 = sha512;

const textEncoder = new TextEncoder();
const PRIVATE_MARKERS = ["PRIVATE KEY"] as const;
const ALLOWED_TYPES = new Set([
  "ssh-ed25519",
  "ecdsa-sha2-nistp256",
  "sk-ssh-ed25519@openssh.com",
]);

export type GeneratedSshKey = {
  keyType: "ssh-ed25519";
  publicKey: string;
  privateKey: string;
  fingerprintSha256: string;
  privateFileName: string;
  publicFileName: string;
};

export type ParsedSshPublicKey = {
  keyType: string;
  publicKey: string;
  comment: string;
  fingerprintSha256: string;
};

function concatBytes(...values: Uint8Array[]): Uint8Array {
  const size = values.reduce((total, value) => total + value.length, 0);
  const result = new Uint8Array(size);
  let offset = 0;
  for (const value of values) {
    result.set(value, offset);
    offset += value.length;
  }
  return result;
}

function uint32(value: number): Uint8Array {
  const result = new Uint8Array(4);
  new DataView(result.buffer).setUint32(0, value, false);
  return result;
}

function sshString(value: Uint8Array | string): Uint8Array {
  const bytes = typeof value === "string" ? textEncoder.encode(value) : value;
  return concatBytes(uint32(bytes.length), bytes);
}

function bytesToBase64(value: Uint8Array): string {
  let binary = "";
  const chunkSize = 0x8000;
  for (let offset = 0; offset < value.length; offset += chunkSize) {
    binary += String.fromCharCode(
      ...value.subarray(offset, offset + chunkSize),
    );
  }
  return btoa(binary);
}

function base64ToBytes(value: string): Uint8Array {
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(value)) {
    throw new Error("SSH 公钥 Base64 格式无效");
  }
  try {
    const decoded = atob(value);
    return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
  } catch {
    throw new Error("SSH 公钥 Base64 格式无效");
  }
}

function readSshString(
  bytes: Uint8Array,
  initialOffset: number,
): [Uint8Array, number] {
  if (initialOffset + 4 > bytes.length) throw new Error("SSH 公钥数据不完整");
  const length = new DataView(
    bytes.buffer,
    bytes.byteOffset + initialOffset,
    4,
  ).getUint32(0, false);
  const start = initialOffset + 4;
  const end = start + length;
  if (length > 16 * 1024 || end > bytes.length)
    throw new Error("SSH 公钥字段长度无效");
  return [bytes.slice(start, end), end];
}

function decodeAscii(value: Uint8Array): string {
  if (value.some((byte) => byte > 0x7f)) throw new Error("SSH Key 类型无效");
  return String.fromCharCode(...value);
}

function safeComment(value: string): string {
  const comment = value.trim();
  if (comment.length > 128 || /[\u0000-\u001f\u007f-\u009f]/u.test(comment)) {
    throw new Error("SSH Key 注释包含不允许的字符");
  }
  return comment;
}

function validateBlob(keyType: string, blob: Uint8Array): void {
  const [embedded, afterType] = readSshString(blob, 0);
  if (decodeAscii(embedded) !== keyType)
    throw new Error("SSH Key 类型与内容不一致");
  let offset = afterType;
  if (keyType === "ssh-ed25519") {
    const [publicBytes, next] = readSshString(blob, offset);
    if (publicBytes.length !== 32) throw new Error("ED25519 公钥长度无效");
    offset = next;
  } else if (keyType === "ecdsa-sha2-nistp256") {
    const [curve, afterCurve] = readSshString(blob, offset);
    const [point, next] = readSshString(blob, afterCurve);
    if (
      decodeAscii(curve) !== "nistp256" ||
      point.length !== 65 ||
      point[0] !== 4
    ) {
      throw new Error("ECDSA P-256 公钥格式无效");
    }
    offset = next;
  } else if (keyType === "sk-ssh-ed25519@openssh.com") {
    const [publicBytes, afterPublic] = readSshString(blob, offset);
    const [application, next] = readSshString(blob, afterPublic);
    if (publicBytes.length !== 32 || application.length === 0)
      throw new Error("安全密钥 ED25519 公钥格式无效");
    offset = next;
  }
  if (offset !== blob.length) throw new Error("SSH 公钥包含多余数据");
}

function publicBlob(publicKey: Uint8Array): Uint8Array {
  return concatBytes(sshString("ssh-ed25519"), sshString(publicKey));
}

function fingerprint(blob: Uint8Array): string {
  return `SHA256:${bytesToBase64(sha256(blob)).replace(/=+$/u, "")}`;
}

function privateKeyFile(
  seed: Uint8Array,
  publicKey: Uint8Array,
  comment: string,
): string {
  const checkBytes = new Uint8Array(4);
  globalThis.crypto.getRandomValues(checkBytes);
  const check = new DataView(checkBytes.buffer).getUint32(0, false);
  const unpadded = concatBytes(
    uint32(check),
    uint32(check),
    sshString("ssh-ed25519"),
    sshString(publicKey),
    sshString(concatBytes(seed, publicKey)),
    sshString(comment),
  );
  const paddingLength = 8 - (unpadded.length % 8);
  const padding = Uint8Array.from(
    { length: paddingLength },
    (_unused, index) => index + 1,
  );
  const body = concatBytes(
    textEncoder.encode("openssh-key-v1\0"),
    sshString("none"),
    sshString("none"),
    sshString(new Uint8Array()),
    uint32(1),
    sshString(publicBlob(publicKey)),
    sshString(concatBytes(unpadded, padding)),
  );
  const encoded = bytesToBase64(body);
  const wrapped = encoded.match(/.{1,70}/gu)?.join("\n") ?? encoded;
  return `-----BEGIN OPENSSH PRIVATE KEY-----\n${wrapped}\n-----END OPENSSH PRIVATE KEY-----\n`;
}

export function containsPrivateKeyMaterial(value: string): boolean {
  const upper = value.toUpperCase();
  return PRIVATE_MARKERS.some((marker) => upper.includes(marker));
}

export function parseOpenSshPublicKey(value: string): ParsedSshPublicKey {
  if (containsPrivateKeyMaterial(value))
    throw new Error("检测到私钥内容，已阻止导入");
  if (textEncoder.encode(value).length > 16 * 1024)
    throw new Error("SSH 公钥文件超过大小限制");
  const lines = value
    .split(/\r?\n/u)
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length !== 1) throw new Error("每次只能导入一把 SSH 公钥");
  const match = /^(\S+)\s+(\S+)(?:\s+(.+))?$/u.exec(lines[0]);
  if (!match || !ALLOWED_TYPES.has(match[1]))
    throw new Error("SSH Key 类型不受支持");
  const blob = base64ToBytes(match[2]);
  validateBlob(match[1], blob);
  const comment = safeComment(match[3] ?? "");
  const canonical = `${match[1]} ${bytesToBase64(blob)}${comment ? ` ${comment}` : ""}`;
  return {
    keyType: match[1],
    publicKey: canonical,
    comment,
    fingerprintSha256: fingerprint(blob),
  };
}

export function generateOpenSshEd25519(
  username: string,
  requestedComment: string,
): GeneratedSshKey {
  if (!globalThis.crypto?.getRandomValues) {
    throw new Error("当前浏览器没有可用的安全随机源，无法在本地生成 SSH Key");
  }
  const comment = safeComment(requestedComment || `${username} Portal key`);
  const { secretKey, publicKey } = ed25519.keygen();
  const blob = publicBlob(publicKey);
  const publicLine = `ssh-ed25519 ${bytesToBase64(blob)} ${comment}`;
  const baseName = `h100_${username.replace(/[^a-z0-9_-]/giu, "_")}_ed25519`;
  const privateKey = privateKeyFile(secretKey, publicKey, comment);
  secretKey.fill(0);
  return {
    keyType: "ssh-ed25519",
    publicKey: publicLine,
    privateKey,
    fingerprintSha256: fingerprint(blob),
    privateFileName: baseName,
    publicFileName: `${baseName}.pub`,
  };
}

export function downloadTextFile(
  fileName: string,
  content: string,
  mediaType = "application/octet-stream",
): void {
  const url = URL.createObjectURL(new Blob([content], { type: mediaType }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.rel = "noopener";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement("textarea");
  input.value = value;
  input.setAttribute("readonly", "true");
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.append(input);
  input.select();
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error("浏览器未允许复制");
}
