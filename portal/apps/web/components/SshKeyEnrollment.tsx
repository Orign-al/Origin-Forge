"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { Button, Input, StatusBadge } from "@h100-portal/ui";
import {
  ApiError,
  createOperation,
  enrollSshKey,
  sshKeys,
  type SshKeyRecord,
} from "../lib/api";
import {
  containsPrivateKeyMaterial,
  copyText,
  downloadTextFile,
  generateOpenSshEd25519,
  parseOpenSshPublicKey,
  type GeneratedSshKey,
  type ParsedSshPublicKey,
} from "../lib/ssh-key";

type Scope = "HOST" | "CONTAINER" | "BOTH";
type EnrollmentMode = "idle" | "generate" | "import";

const SCOPE_LABELS: Record<Scope, string> = {
  HOST: "宿主机",
  CONTAINER: "开发容器",
  BOTH: "宿主机 + 容器",
};

function keyStateLabel(key: SshKeyRecord): string {
  if (key.state === "VALIDATED") return "VALIDATED — NOT INSTALLED";
  return key.state;
}

function ScopeControl({
  value,
  onChange,
  containerOnly = false,
}: {
  value: Scope;
  onChange: (scope: Scope) => void;
  containerOnly?: boolean;
}) {
  const scopes: Scope[] = containerOnly
    ? ["CONTAINER"]
    : (Object.keys(SCOPE_LABELS) as Scope[]);
  return (
    <div
      className="segmented-control"
      role="radiogroup"
      aria-label="SSH Key 用途"
    >
      {scopes.map((scope) => (
        <label
          className={value === scope ? "segment segment-active" : "segment"}
          key={scope}
        >
          <input
            type="radio"
            name="ssh-key-scope"
            value={scope}
            checked={value === scope}
            onChange={() => onChange(scope)}
          />
          {SCOPE_LABELS[scope]}
        </label>
      ))}
    </div>
  );
}

function KeyTable({ keys }: { keys: SshKeyRecord[] }) {
  if (!keys.length) return null;
  return (
    <div className="ui-table-wrap ssh-key-table">
      <table className="ui-table">
        <thead>
          <tr>
            <th>类型</th>
            <th>Fingerprint</th>
            <th>注释</th>
            <th>用途</th>
            <th>状态</th>
            <th>添加时间</th>
          </tr>
        </thead>
        <tbody>
          {keys.map((key) => (
            <tr key={key.id}>
              <td>{key.key_type.replace("ssh-", "").toUpperCase()}</td>
              <td className="mono">{key.fingerprint_sha256}</td>
              <td>{key.comment || "—"}</td>
              <td>{SCOPE_LABELS[key.scope]}</td>
              <td>
                <StatusBadge value={keyStateLabel(key)} />
              </td>
              <td>{new Date(key.created_at).toLocaleString("zh-CN")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SshKeyEnrollment({
  userId,
  username,
  computeState,
  managedUserId,
  activateDryRun,
  onClose,
  containerOnly = false,
}: {
  userId: string;
  username: string;
  computeState: string;
  managedUserId?: string | null;
  activateDryRun?: Record<string, unknown> | null;
  onClose?: () => void;
  containerOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [mode, setMode] = useState<EnrollmentMode>("idle");
  const [scope, setScope] = useState<Scope>(
    containerOnly ? "CONTAINER" : "BOTH",
  );
  const [comment, setComment] = useState("Origin laptop");
  const [generated, setGenerated] = useState<GeneratedSshKey | null>(null);
  const [privateDownloaded, setPrivateDownloaded] = useState(false);
  const [privateSaved, setPrivateSaved] = useState(false);
  const [importText, setImportText] = useState("");
  const [imported, setImported] = useState<ParsedSshPublicKey | null>(null);
  const [importConfirmed, setImportConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [localActivatePlan, setLocalActivatePlan] = useState<Record<
    string,
    unknown
  > | null>(null);
  const keysQuery = useQuery({
    queryKey: ["ssh-keys", userId],
    queryFn: () => sshKeys(userId),
    retry: false,
  });
  const keys = keysQuery.data?.keys ?? [];
  const validKeys = keys.filter(
    (key) => key.state === "VALIDATED" || key.state === "INSTALLED",
  );
  const effectiveManagedUserId =
    managedUserId ?? keysQuery.data?.enrollment.managed_user_id;
  const plan = localActivatePlan ?? activateDryRun;
  const hostSshPolicy =
    plan?.host_ssh_policy &&
    typeof plan.host_ssh_policy === "object" &&
    !Array.isArray(plan.host_ssh_policy)
      ? (plan.host_ssh_policy as Record<string, unknown>)
      : null;

  function resetFlow(nextMode: EnrollmentMode) {
    setMode(nextMode);
    setGenerated(null);
    setPrivateDownloaded(false);
    setPrivateSaved(false);
    setImportText("");
    setImported(null);
    setImportConfirmed(false);
    setError(null);
    setMessage(null);
  }

  function generateKey() {
    setError(null);
    try {
      const next = generateOpenSshEd25519(username, comment);
      setGenerated(next);
      setPrivateDownloaded(false);
      setPrivateSaved(false);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "浏览器无法生成 SSH Key",
      );
    }
  }

  function downloadPrivateKey() {
    if (!generated) return;
    downloadTextFile(generated.privateFileName, generated.privateKey);
    setPrivateDownloaded(true);
  }

  function acceptImportText(value: string) {
    setError(null);
    setImported(null);
    setImportConfirmed(false);
    if (containsPrivateKeyMaterial(value)) {
      setImportText("");
      setError("检测到私钥内容，已阻止导入。Portal 只接受 .pub 公钥。");
      return;
    }
    if (new TextEncoder().encode(value).length > 16 * 1024) {
      setImportText("");
      setError("SSH 公钥内容超过 16KB 限制，已阻止导入。");
      return;
    }
    setImportText(value);
  }

  async function uploadPublicFile(file: File | undefined) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pub")) {
      setError("仅支持 .pub 公钥文件；不要选择私钥文件。");
      if (fileInput.current) fileInput.current.value = "";
      return;
    }
    if (file.size <= 0 || file.size > 16 * 1024) {
      setError("公钥文件为空或超过 16KB 限制。");
      return;
    }
    acceptImportText(await file.text());
  }

  function previewImport() {
    setError(null);
    try {
      setImported(parseOpenSshPublicKey(importText));
    } catch (caught) {
      setImported(null);
      setError(caught instanceof Error ? caught.message : "SSH 公钥格式无效");
    }
  }

  async function refreshAfterEnrollment() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["ssh-keys", userId] }),
      queryClient.invalidateQueries({ queryKey: ["user", userId] }),
      queryClient.invalidateQueries({ queryKey: ["me"] }),
      queryClient.invalidateQueries({ queryKey: ["users"] }),
    ]);
  }

  async function enrollGenerated() {
    if (!generated || !privateDownloaded || !privateSaved) return;
    setBusy(true);
    setError(null);
    try {
      await enrollSshKey(userId, {
        key_type: generated.keyType,
        public_key: generated.publicKey,
        comment,
        scope,
        generation_method: "BROWSER_GENERATED",
        client_fingerprint_sha256: generated.fingerprintSha256,
        confirmed_private_key_saved: true,
      });
      setGenerated(null);
      setPrivateSaved(false);
      setPrivateDownloaded(false);
      setMode("idle");
      setMessage(
        "SSH 公钥已验证；私钥未发送到服务器，authorized_keys 尚未安装。",
      );
      await refreshAfterEnrollment();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? `${caught.code}：${caught.message}`
          : "SSH 公钥登记失败",
      );
    } finally {
      setBusy(false);
    }
  }

  async function enrollImported() {
    if (!imported || !importConfirmed) return;
    setBusy(true);
    setError(null);
    try {
      await enrollSshKey(userId, {
        key_type: imported.keyType as
          "ssh-ed25519" | "ecdsa-sha2-nistp256" | "sk-ssh-ed25519@openssh.com",
        public_key: imported.publicKey,
        comment: comment || imported.comment,
        scope,
        generation_method: "IMPORTED",
        client_fingerprint_sha256: imported.fingerprintSha256,
        confirmed_public_key: true,
      });
      setImportText("");
      setImported(null);
      setImportConfirmed(false);
      setMode("idle");
      setMessage(
        "SSH 公钥已验证；平台没有接收私钥，authorized_keys 尚未安装。",
      );
      await refreshAfterEnrollment();
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? `${caught.code}：${caught.message}`
          : "SSH 公钥登记失败",
      );
    } finally {
      setBusy(false);
    }
  }

  async function createActivateDryRun() {
    if (!effectiveManagedUserId || !validKeys.length) return;
    setBusy(true);
    setError(null);
    try {
      const operation = await createOperation({
        operation_type: "user.activate",
        target_type: "compute_identity",
        target_id: username,
        request_summary: `${username} Activate 计划（仅 dry-run）`,
        payload: {
          managed_user_id: effectiveManagedUserId,
          approved_ssh_key_record_ids: validKeys.map((key) => key.id),
          expected_state: "STAGED",
          approval_reference: "portal3d-r-self-service-v1",
        },
        idempotency_key: `portal3d-r-activate:${Date.now()}`,
      });
      const dryRun = operation.dry_run_result;
      setLocalActivatePlan(
        dryRun && typeof dryRun === "object"
          ? (dryRun as Record<string, unknown>)
          : null,
      );
      setMessage("Activate dry-run 已完成；真实 Activate 仍需管理员明确审批。");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["user", userId] }),
        queryClient.invalidateQueries({ queryKey: ["operations"] }),
      ]);
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? `${caught.code}：${caught.message}`
          : "Activate dry-run 失败",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="ssh-enrollment" data-testid="ssh-key-enrollment">
      <div className="detail-section-heading">
        <div>
          <h2>SSH 密钥设置</h2>
          <p className="muted">
            服务器保存你的公钥。连接 SSH 时，请使用与该公钥匹配的私钥。
          </p>
        </div>
        {onClose ? (
          <Button
            type="button"
            onClick={onClose}
            aria-label="关闭 SSH 密钥设置"
          >
            取消
          </Button>
        ) : null}
      </div>

      {containerOnly ? (
        <div className="security-strip">
          <span>用途：仅开发容器</span>
          <span>宿主 SSH：未启用</span>
          <span>认证：SSH Public Key</span>
        </div>
      ) : (
        <ol className="setup-stepper" aria-label="计算环境启用步骤">
          <li className={validKeys.length ? "step-complete" : "step-current"}>
            1. SSH 密钥
          </li>
          <li>2. 确认资源</li>
          <li>3. 等待激活</li>
          <li>4. 连接环境</li>
        </ol>
      )}

      {computeState === "STAGED" ? (
        <div className="notice staged-key-boundary">
          当前 authorized_keys 为 ABSENT，Shell 为 /usr/sbin/nologin，开发容器为
          STOPPED。 登记公钥不会自动 Activate。
        </div>
      ) : null}

      {message ? <div className="notice success-notice">{message}</div> : null}
      {error ? (
        <div className="error-box" role="alert">
          {error}
        </div>
      ) : null}

      {keysQuery.isPending ? (
        <div className="muted">正在读取 SSH Key 记录…</div>
      ) : null}
      {keysQuery.isError ? (
        <div className="error-box">SSH Key 记录暂时不可用。</div>
      ) : null}
      <KeyTable keys={keys} />

      {mode === "idle" ? (
        <div className="ssh-enrollment-empty">
          {!validKeys.length ? (
            <div>
              <strong>使用 GPU 平台前，需要配置 SSH 密钥。</strong>
              <div className="muted">
                你可以生成新的密钥，也可以导入已有公钥。
              </div>
            </div>
          ) : (
            <div>
              <strong>
                {containerOnly && computeState === "ACTIVE"
                  ? `${validKeys.length} 把公钥可用于开发容器`
                  : `${validKeys.length} 把公钥已验证`}
              </strong>
              <div className="muted">
                {containerOnly && computeState === "ACTIVE"
                  ? "新增密钥仍只授权自己的开发容器，不会启用宿主访问。"
                  : "尚未安装；计算身份仍需 Activate 审批。"}
              </div>
            </div>
          )}
          <div className="button-row">
            <Button
              tone="primary"
              type="button"
              onClick={() => resetFlow("generate")}
            >
              生成新密钥
            </Button>
            <Button type="button" onClick={() => resetFlow("import")}>
              导入已有公钥
            </Button>
          </div>
        </div>
      ) : null}

      {mode === "generate" ? (
        <section className="enrollment-form" aria-label="生成新密钥">
          <h3>生成新密钥</h3>
          <div className="form-field">
            <label htmlFor="generated-key-comment">注释</label>
            <Input
              id="generated-key-comment"
              value={comment}
              maxLength={128}
              onChange={(event) => setComment(event.target.value)}
            />
          </div>
          <div className="form-field">
            <span className="field-label">用途</span>
            <ScopeControl
              value={scope}
              onChange={setScope}
              containerOnly={containerOnly}
            />
          </div>
          {!generated ? (
            <div className="button-row">
              <Button tone="primary" type="button" onClick={generateKey}>
                生成 ED25519 密钥
              </Button>
              <Button type="button" onClick={() => resetFlow("idle")}>
                取消
              </Button>
            </div>
          ) : (
            <div className="generated-key-confirmation">
              <dl className="kv-grid">
                <div className="kv">
                  <dt>类型</dt>
                  <dd>ED25519</dd>
                </div>
                <div className="kv">
                  <dt>Fingerprint</dt>
                  <dd className="mono ssh-fingerprint-value">
                    {generated.fingerprintSha256}
                  </dd>
                </div>
                <div className="kv">
                  <dt>用途</dt>
                  <dd>{SCOPE_LABELS[scope]}</dd>
                </div>
                <div className="kv">
                  <dt>私钥存储</dt>
                  <dd>仅浏览器内存</dd>
                </div>
              </dl>
              <div className="notice">
                私钥只在本次设置流程中提供下载。平台不会保存你的私钥；如果丢失，需要重新添加新的
                SSH Key。
              </div>
              <div className="button-row">
                <Button
                  tone="primary"
                  type="button"
                  onClick={downloadPrivateKey}
                >
                  下载私钥
                </Button>
                <Button
                  type="button"
                  onClick={() =>
                    downloadTextFile(
                      generated.publicFileName,
                      `${generated.publicKey}\n`,
                      "text/plain",
                    )
                  }
                >
                  下载公钥
                </Button>
                <Button
                  type="button"
                  onClick={() => void copyText(generated.publicKey)}
                >
                  复制公钥
                </Button>
                <Button
                  type="button"
                  onClick={() => void copyText(generated.fingerprintSha256)}
                >
                  复制 Fingerprint
                </Button>
              </div>
              <label className="confirmation-check">
                <input
                  type="checkbox"
                  checked={privateSaved}
                  disabled={!privateDownloaded}
                  onChange={(event) => setPrivateSaved(event.target.checked)}
                />
                我已经保存私钥
              </label>
              {!privateDownloaded ? (
                <div className="muted">请先下载私钥，之后才能继续。</div>
              ) : null}
              <div className="notice local-passphrase-note">
                当前版本生成标准 OpenSSH 未加密私钥。保存后可在本机运行
                ssh-keygen -p -f
                {` ${generated.privateFileName}`}{" "}
                添加本地保护密码；密码不会发送到 Portal。
              </div>
              <div className="button-row">
                <Button
                  tone="primary"
                  type="button"
                  disabled={!privateSaved || busy}
                  onClick={() => void enrollGenerated()}
                >
                  继续
                </Button>
                <Button
                  type="button"
                  disabled={busy}
                  onClick={() => resetFlow("idle")}
                >
                  取消
                </Button>
              </div>
            </div>
          )}
        </section>
      ) : null}

      {mode === "import" ? (
        <section className="enrollment-form" aria-label="导入已有公钥">
          <h3>导入已有公钥</h3>
          <div className="notice">
            仅支持 SSH 公钥（.pub）。不要上传没有 .pub 后缀的私钥文件。
          </div>
          <div className="form-field">
            <label htmlFor="public-key-upload">上传 .pub 文件</label>
            <input
              ref={fileInput}
              id="public-key-upload"
              className="file-input"
              type="file"
              accept=".pub,text/plain"
              onChange={(event) =>
                void uploadPublicFile(event.target.files?.[0])
              }
            />
          </div>
          <div className="form-field">
            <label htmlFor="public-key-paste">或粘贴 .pub 内容</label>
            <textarea
              id="public-key-paste"
              className="ui-textarea mono"
              rows={5}
              value={importText}
              onChange={(event) => acceptImportText(event.target.value)}
              placeholder="ssh-ed25519 AAAA... comment"
              spellCheck={false}
            />
          </div>
          <div className="form-field">
            <label htmlFor="imported-key-comment">注释</label>
            <Input
              id="imported-key-comment"
              value={comment}
              maxLength={128}
              onChange={(event) => setComment(event.target.value)}
            />
          </div>
          <div className="form-field">
            <span className="field-label">用途</span>
            <ScopeControl
              value={scope}
              onChange={setScope}
              containerOnly={containerOnly}
            />
          </div>
          {!imported ? (
            <div className="button-row">
              <Button
                tone="primary"
                type="button"
                onClick={previewImport}
                disabled={!importText}
              >
                校验公钥
              </Button>
              <Button type="button" onClick={() => resetFlow("idle")}>
                取消
              </Button>
            </div>
          ) : (
            <div className="generated-key-confirmation">
              <dl className="kv-grid">
                <div className="kv">
                  <dt>类型</dt>
                  <dd>{imported.keyType}</dd>
                </div>
                <div className="kv">
                  <dt>Fingerprint</dt>
                  <dd className="mono ssh-fingerprint-value">
                    {imported.fingerprintSha256}
                  </dd>
                </div>
                <div className="kv">
                  <dt>用途</dt>
                  <dd>{SCOPE_LABELS[scope]}</dd>
                </div>
                <div className="kv">
                  <dt>私钥</dt>
                  <dd>未接收</dd>
                </div>
              </dl>
              <label className="confirmation-check">
                <input
                  type="checkbox"
                  checked={importConfirmed}
                  onChange={(event) => setImportConfirmed(event.target.checked)}
                />
                我确认这是与我持有的私钥匹配的 SSH 公钥
              </label>
              <div className="button-row">
                <Button
                  tone="primary"
                  type="button"
                  disabled={!importConfirmed || busy}
                  onClick={() => void enrollImported()}
                >
                  继续
                </Button>
                <Button
                  type="button"
                  disabled={busy}
                  onClick={() => resetFlow("idle")}
                >
                  取消
                </Button>
              </div>
            </div>
          )}
        </section>
      ) : null}

      {validKeys.length > 0 && computeState === "STAGED" ? (
        <section className="activate-dry-run">
          <div>
            <h3>Activate Dry-Run</h3>
            <p className="muted">
              重新验证 STAGED 资源和 Key Scope；不会安装 authorized_keys、修改
              Shell 或启动容器。
            </p>
          </div>
          <Button
            tone="primary"
            type="button"
            disabled={busy}
            onClick={() => void createActivateDryRun()}
          >
            生成 Activate Dry-Run
          </Button>
        </section>
      ) : null}

      {plan ? (
        <section className="activate-plan" data-testid="activate-dry-run-plan">
          <div className="detail-section-heading">
            <h3>Activate Dry-Run</h3>
            <StatusBadge
              value={String(plan.activate_status ?? plan.status ?? "UNKNOWN")}
            />
          </div>
          <dl className="kv-grid">
            <div className="kv">
              <dt>Host authorized_keys</dt>
              <dd>{String(plan.host_authorized_keys_install ?? "UNKNOWN")}</dd>
            </div>
            <div className="kv">
              <dt>Container authorized_keys</dt>
              <dd>
                {String(plan.container_authorized_keys_install ?? "UNKNOWN")}
              </dd>
            </div>
            <div className="kv">
              <dt>执行</dt>
              <dd>DISABLED — ADMINISTRATOR APPROVAL REQUIRED</dd>
            </div>
            <div className="kv">
              <dt>当前 Shell</dt>
              <dd>{String(plan.shell_current ?? "/usr/sbin/nologin")}</dd>
            </div>
            <div className="kv">
              <dt>宿主 SSH 策略</dt>
              <dd>{String(hostSshPolicy?.status ?? "UNKNOWN")}</dd>
            </div>
            <div className="kv">
              <dt>Public Key Authentication</dt>
              <dd>
                {hostSshPolicy?.pubkey_authentication === true
                  ? "ENABLED"
                  : "DISABLED"}
              </dd>
            </div>
            <div className="kv">
              <dt>Password Authentication</dt>
              <dd>
                {hostSshPolicy?.password_authentication === false
                  ? "DISABLED"
                  : "ENABLED"}
              </dd>
            </div>
            <div className="kv">
              <dt>Keyboard Interactive</dt>
              <dd>
                {hostSshPolicy?.keyboard_interactive_authentication === false
                  ? "DISABLED"
                  : "ENABLED"}
              </dd>
            </div>
            <div className="kv">
              <dt>Required Authentication</dt>
              <dd>
                {Array.isArray(hostSshPolicy?.authentication_methods)
                  ? hostSshPolicy.authentication_methods
                      .map((item) => String(item).toUpperCase())
                      .join(", ")
                  : "UNKNOWN"}
              </dd>
            </div>
          </dl>
        </section>
      ) : null}
    </div>
  );
}
