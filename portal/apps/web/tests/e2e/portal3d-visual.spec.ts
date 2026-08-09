import { createHash, generateKeyPairSync } from "node:crypto";
import { mkdirSync } from "node:fs";
import path from "node:path";

import {
  expect,
  test,
  type Download,
  type Page,
  type Route,
} from "@playwright/test";

type Scope = "HOST" | "CONTAINER" | "BOTH";

type MockKey = {
  id: string;
  managed_user_id: string;
  key_type: string;
  fingerprint_sha256: string;
  comment: string;
  scope: Scope;
  state: "VALIDATED" | "INSTALLED";
  generation_method: "BROWSER_GENERATED" | "IMPORTED";
  created_at: string;
  created_by: string;
  validated_at: string;
  installed_at: string | null;
  revoked_at: null;
};

type Portal3dState = {
  authenticated: boolean;
  keys: MockKey[];
  enrollmentBodies: Array<Record<string, unknown>>;
  activateBodies: Array<Record<string, unknown>>;
  startBodies?: Array<Record<string, unknown>>;
  computeState?: "STAGED" | "ACTIVE";
  containerState?: "STOPPED" | "RUNNING";
  clientValidation?: "PENDING" | "PASS";
  pilotAcceptance?: "PASSED";
};

const USER_ID = "00000000-0000-4000-8000-000000000001";
const MANAGED_USER_ID = "00000000-0000-4000-8000-000000000030";
const OUTPUT_DIRECTORY = path.resolve(
  "test-results",
  "portal3d-r-visual-acceptance",
);
const PRIVATE_ARMOR =
  "-----BEGIN OPENSSH PRIVATE KEY-----\nforbidden-test-material\n-----END OPENSSH PRIVATE KEY-----";

function sshString(value: Buffer): Buffer {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(value.length);
  return Buffer.concat([length, value]);
}

function temporaryPublicKey(comment = "Portal Playwright import"): string {
  const pair = generateKeyPairSync("ed25519");
  const der = pair.publicKey.export({ type: "spki", format: "der" });
  const raw = Buffer.from(der).subarray(-32);
  const blob = Buffer.concat([
    sshString(Buffer.from("ssh-ed25519", "ascii")),
    sshString(raw),
  ]);
  return `ssh-ed25519 ${blob.toString("base64")} ${comment}`;
}

function fingerprint(publicKey: string): string {
  const blob = Buffer.from(publicKey.split(/\s+/u)[1], "base64");
  return `SHA256:${createHash("sha256").update(blob).digest("base64").replace(/=+$/u, "")}`;
}

function enrollment(state: Portal3dState) {
  const computeState = state.computeState ?? "STAGED";
  return {
    required: computeState === "STAGED" && state.keys.length === 0,
    managed_user_id: MANAGED_USER_ID,
    compute_identity: "origin-pilot",
    compute_state: computeState,
    validated_key_count: state.keys.length,
    ssh_key_state:
      computeState === "ACTIVE" && state.keys.length
        ? "INSTALLED"
        : state.keys.length
          ? "VALIDATED"
          : "REQUIRED_BEFORE_ACTIVATION",
    setup_path: `/users/${USER_ID}?tab=ssh`,
  };
}

function stagedUser(state: Portal3dState) {
  const computeState = state.computeState ?? "STAGED";
  const containerState = state.containerState ?? "STOPPED";
  return {
    id: USER_ID,
    login_name: "Origin-al",
    normalized_login: "origin-al",
    display_name: "Origin-al",
    unix_username: "origin-al",
    account_state: "ACTIVE",
    password_state: "SET",
    resource_onboarding_state: computeState,
    created_at: "2026-08-06T00:00:00Z",
    activated_at: "2026-08-06T00:05:00Z",
    last_login_at: "2026-08-08T00:00:00Z",
    roles: [{ name: "platform_owner", description: "网页平台所有者" }],
    linux_identity: {
      managed_user_id: MANAGED_USER_ID,
      unix_username: "origin-pilot",
      uid: 20001,
      gid: 20001,
      shell: computeState === "ACTIVE" ? "/bin/bash" : "/usr/sbin/nologin",
      onboarding_state: computeState,
      host_access_state: computeState === "ACTIVE" ? "ENABLED" : "DISABLED",
      password_state: "LOCKED",
      authorized_keys_state: computeState === "ACTIVE" ? "INSTALLED" : "ABSENT",
      ssh_key_count: state.keys.length,
      ssh_key_state:
        computeState === "ACTIVE" && state.keys.length
          ? "INSTALLED"
          : state.keys.length
            ? "VALIDATED"
            : "REQUIRED_BEFORE_ACTIVATION",
      gpu_isolation_state: "VERIFIED",
      gpu_open_state: "DENIED",
      cuda_context_state: "DENIED",
      guard_state: "PASSING",
      slurm_account: "company",
      slurm_qos: "general",
      max_gpus: 1,
      project_id: 30001,
      quota_bytes: 300 * 1024 ** 3,
      container_name: "gpu-dev-origin-pilot",
      container_port: 22023,
      container_state: containerState,
      container_gpu: "NONE",
      container_cpus: 8,
      container_memory_gb: 32,
      container_pids_limit: 4096,
      container_image_digest: `sha256:${"a".repeat(64)}`,
      approved_host: "10.82.36.1",
      host_ssh_port: 22,
      host_ssh_server:
        computeState === "ACTIVE"
          ? "READY_FOR_CLIENT_VALIDATION"
          : "NOT_READY",
      container_ssh_server:
        computeState === "ACTIVE"
          ? "READY_FOR_CLIENT_VALIDATION"
          : "NOT_READY",
      host_server_fingerprint:
        computeState === "ACTIVE" ? "SHA256:test-host-server" : null,
      container_server_fingerprint:
        computeState === "ACTIVE" ? "SHA256:test-container-server" : null,
      host_ssh_client_validation:
        computeState === "ACTIVE"
          ? (state.clientValidation ?? "PENDING")
          : "NOT_STARTED",
      container_ssh_client_validation:
        computeState === "ACTIVE"
          ? (state.clientValidation ?? "PENDING")
          : "NOT_STARTED",
      host_ssh_policy: {
        status: "PASSING",
        pubkey_authentication: true,
        password_authentication: false,
        keyboard_interactive_authentication: false,
        authentication_methods: ["publickey"],
      },
      slurm_node_state: "DRAIN",
      slurm_queue: "EMPTY",
      gpu_scheduling_available: false,
      pilot_acceptance_status: state.pilotAcceptance,
      pilot_cpu_job_id: state.pilotAcceptance ? 201 : null,
      pilot_gpu_job_id: state.pilotAcceptance ? 202 : null,
      pilot_allocated_gpu_uuid: state.pilotAcceptance
        ? "GPU-11111111-2222-3333-4444-555555555555"
        : null,
      pilot_final_node_state: state.pilotAcceptance ? "DRAIN" : null,
    },
    compute_onboarding: {
      status: computeState,
      compute_username: "origin-pilot",
      draft_state: computeState,
      operation_id: "88e26514-91f4-46d3-999a-9a5f7c2cda6d",
      operation_status: "SUCCEEDED",
      ssh_key_status: state.keys.length
        ? "VALIDATED"
        : "REQUIRED_BEFORE_ACTIVATION",
      plan: null,
      activate_dry_run: null,
    },
  };
}

function activatePlan(keys: MockKey[]) {
  return {
    status: "DRY_RUN",
    handler: "user.activate",
    activate_status: "READY",
    execution_enabled: false,
    expected_state: "STAGED",
    managed_user_id: MANAGED_USER_ID,
    approved_ssh_keys: keys.map((key) => ({
      record_id: key.id,
      key_type: key.key_type,
      fingerprint_sha256: key.fingerprint_sha256,
      scope: key.scope,
      managed_user_id: MANAGED_USER_ID,
    })),
    public_key_validation: "PASSED",
    host_authorized_keys_install: "PLANNED",
    container_authorized_keys_install: "PLANNED",
    host_authorized_keys_current: "ABSENT",
    container_authorized_keys_current: "ABSENT",
    shell_current: "/usr/sbin/nologin",
    password_current: "LOCKED",
    gpu_isolation: "PASS",
    host_ssh_policy: {
      status: "PASSING",
      pubkey_authentication: true,
      password_authentication: false,
      keyboard_interactive_authentication: false,
      authentication_methods: ["publickey"],
    },
    guard: { timer: "ENABLED_ACTIVE", status: "PASSING" },
    quota: { project_id: 30001, hard_limit_gb: 300 },
    slurm: {
      account: "company",
      qos: "general",
      max_gpus: 1,
      node_state: "DRAIN",
    },
    container: { name: "gpu-dev-origin-pilot", state: "STOPPED", gpu: "NONE" },
  };
}

async function json(
  route: Route,
  body: unknown,
  status = 200,
  headers = {},
): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    headers,
    body: JSON.stringify(body),
  });
}

async function installPortal3dApi(
  page: Page,
  state: Portal3dState,
): Promise<void> {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const requestPath = new URL(request.url()).pathname.replace(
      /^\/api\/v1/u,
      "",
    );
    if (requestPath === "/auth/csrf") {
      await json(route, { csrf_token: "portal3d-test-csrf" }, 200, {
        "set-cookie": "h100_csrf=portal3d-test-csrf; Path=/; SameSite=Strict",
      });
      return;
    }
    if (requestPath === "/auth/login") {
      state.authenticated = true;
      await json(
        route,
        {
          user: stagedUser(state),
          csrf_token: "portal3d-session-csrf",
          ssh_enrollment: enrollment(state),
        },
        200,
        {
          "set-cookie":
            "h100_csrf=portal3d-session-csrf; Path=/; SameSite=Strict",
        },
      );
      return;
    }
    if (requestPath === "/auth/me") {
      if (!state.authenticated) {
        await json(
          route,
          { detail: { code: "AUTH_REQUIRED", message: "请先登录" } },
          401,
        );
        return;
      }
      await json(route, {
        user: stagedUser(state),
        role: "platform_owner",
        ssh_enrollment: enrollment(state),
      });
      return;
    }
    if (requestPath === "/audit/page-access") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (requestPath === `/users/${USER_ID}`) {
      await json(route, { status: "OK", user: stagedUser(state) });
      return;
    }
    if (requestPath === `/users/${USER_ID}/ssh-keys`) {
      if (request.method() === "GET") {
        await json(route, {
          status: "OK",
          keys: state.keys,
          count: state.keys.length,
          maximum_active_keys: 5,
          enrollment: enrollment(state),
        });
        return;
      }
      const body = request.postDataJSON() as Record<string, unknown>;
      state.enrollmentBodies.push(body);
      for (const forbidden of [
        "private_key",
        "private_key_password",
        "private_key_path",
      ]) {
        expect(body).not.toHaveProperty(forbidden);
      }
      expect(String(body.public_key ?? "")).not.toContain("PRIVATE KEY");
      const id = `00000000-0000-4000-8000-${String(state.keys.length + 40).padStart(12, "0")}`;
      const record: MockKey = {
        id,
        managed_user_id: MANAGED_USER_ID,
        key_type: String(body.key_type),
        fingerprint_sha256: String(body.client_fingerprint_sha256),
        comment: String(body.comment ?? ""),
        scope: body.scope as Scope,
        state: "VALIDATED",
        generation_method:
          body.generation_method as MockKey["generation_method"],
        created_at: "2026-08-08T00:00:00Z",
        created_by: USER_ID,
        validated_at: "2026-08-08T00:00:00Z",
        installed_at: null,
        revoked_at: null,
      };
      state.keys.push(record);
      await json(
        route,
        {
          status: "VALIDATED",
          key: record,
          private_key_received: false,
          authorized_keys_installed: false,
        },
        201,
      );
      return;
    }
    if (requestPath === "/containers/gpu-dev-origin-pilot/start") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.startBodies ??= [];
      state.startBodies.push(body);
      expect(body).toMatchObject({
        expected_compute_state: "ACTIVE",
        expected_container_state: "STOPPED",
        expected_ssh_key_state: "INSTALLED",
      });
      expect(String(body.idempotency_key)).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u,
      );
      expect(body).not.toHaveProperty("command");
      expect(body).not.toHaveProperty("path");
      expect(body).not.toHaveProperty("argv");
      state.containerState = "RUNNING";
      await json(route, {
        status: "SUCCEEDED",
        operation_id: "00000000-0000-4000-8000-000000000091",
        container: {
          name: "gpu-dev-origin-pilot",
          state: "RUNNING",
          gpu: "NONE",
        },
      });
      return;
    }
    if (requestPath === "/operations") {
      if (request.method() === "POST") {
        const body = request.postDataJSON() as Record<string, unknown>;
        state.activateBodies.push(body);
        expect(body).toMatchObject({
          operation_type: "user.activate",
          target_id: "origin-pilot",
          payload: {
            managed_user_id: MANAGED_USER_ID,
            approved_ssh_key_record_ids: state.keys.map((key) => key.id),
            expected_state: "STAGED",
          },
        });
        await json(route, {
          id: "00000000-0000-4000-8000-000000000090",
          operation_type: "user.activate",
          target_type: "compute_identity",
          target_id: "origin-pilot",
          status: "DRAFT",
          dry_run_result: activatePlan(state.keys),
        });
        return;
      }
      await json(route, { status: "OK", operations: [] });
      return;
    }
    await json(
      route,
      { detail: { code: "PORTAL3D_TEST_ROUTE_MISSING", message: requestPath } },
      404,
    );
  });
}

async function browserStorageSnapshot(page: Page) {
  return page.evaluate(async () => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
    indexedDb: "databases" in indexedDB ? await indexedDB.databases() : [],
    caches: "caches" in globalThis ? await caches.keys() : [],
    serviceWorkers:
      "serviceWorker" in navigator
        ? (await navigator.serviceWorker.getRegistrations()).map(
            (item) => item.scope,
          )
        : [],
  }));
}

async function assertNoBrowserPrivateStorage(
  page: Page,
  baseline: Awaited<ReturnType<typeof browserStorageSnapshot>>,
): Promise<void> {
  const storage = await browserStorageSnapshot(page);
  expect(storage.local).toEqual([]);
  expect(storage.session).toEqual([]);
  expect(storage.indexedDb).toEqual(baseline.indexedDb);
  expect(storage.caches).toEqual([]);
  expect(storage.serviceWorkers).toEqual([]);
}

async function discardDownload(download: Download): Promise<void> {
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(Buffer.from(chunk));
  const content = Buffer.concat(chunks).toString("utf8");
  expect(content).toMatch(/^-----BEGIN OPENSSH PRIVATE KEY-----/u);
  await download.delete();
}

async function openSshTab(page: Page): Promise<void> {
  await page.goto(`/users/${USER_ID}?tab=ssh`);
  await expect(
    page.getByRole("heading", { name: "SSH 密钥设置" }),
  ).toBeVisible();
}

async function assertVisualBoundary(page: Page): Promise<void> {
  await expect(page.getByText(/AI 助手|智能助手|AI 洞察/u)).toHaveCount(0);
  const result = await page.evaluate(() => ({
    horizontalOverflow:
      document.documentElement.scrollWidth > window.innerWidth,
    overlapping: [
      ...document.querySelectorAll("button, a, input, textarea"),
    ].some((element) => {
      const box = element.getBoundingClientRect();
      return (
        box.width > 0 &&
        box.height > 0 &&
        (box.right > window.innerWidth + 1 || box.left < -1)
      );
    }),
    fingerprintOverflow: [
      ...document.querySelectorAll(".ssh-fingerprint-value"),
    ].some((element) => {
      const row = element.closest(".kv");
      if (!row) return true;
      const box = element.getBoundingClientRect();
      const rowBox = row.getBoundingClientRect();
      return box.left < rowBox.left - 1 || box.right > rowBox.right + 1;
    }),
  }));
  expect(result.horizontalOverflow).toBe(false);
  expect(result.overlapping).toBe(false);
  expect(result.fingerprintOverflow).toBe(false);
}

test("首次登录无 Key 时进入统一 SSH 密钥设置流程", async ({ page }) => {
  const state: Portal3dState = {
    authenticated: false,
    keys: [],
    enrollmentBodies: [],
    activateBodies: [],
  };
  await installPortal3dApi(page, state);
  await page.goto("/login");
  await page.getByLabel("登录名").fill("Origin-al");
  await page.getByLabel("网页密码").fill("A sufficiently long Portal password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(new RegExp(`/users/${USER_ID}\\?tab=ssh$`, "u"));
  await expect(
    page.getByText("完成 SSH 密钥设置后即可启用计算环境。"),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "生成新密钥" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "导入已有公钥" }),
  ).toBeVisible();
});

test("浏览器生成私钥只下载到本地且请求只包含公钥", async ({ page }) => {
  const state: Portal3dState = {
    authenticated: true,
    keys: [],
    enrollmentBodies: [],
    activateBodies: [],
  };
  await installPortal3dApi(page, state);
  await openSshTab(page);
  const storageBeforeGeneration = await browserStorageSnapshot(page);
  await page.getByRole("button", { name: "生成新密钥" }).click();
  await page.getByRole("button", { name: "生成 ED25519 密钥" }).click();
  await expect(page.getByText(/^SHA256:/u)).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载私钥" }).click();
  await discardDownload(await downloadPromise);
  await page.getByLabel("我已经保存私钥").check();
  await page.getByRole("button", { name: "继续" }).click();
  await expect(page.getByText(/私钥未发送到服务器/u)).toBeVisible();
  expect(state.enrollmentBodies).toHaveLength(1);
  expect(state.enrollmentBodies[0]).toMatchObject({
    key_type: "ssh-ed25519",
    scope: "BOTH",
    generation_method: "BROWSER_GENERATED",
    confirmed_private_key_saved: true,
  });
  expect(state.keys).toHaveLength(1);
  await assertNoBrowserPrivateStorage(page, storageBeforeGeneration);
});

test("导入公钥成功并在浏览器和服务器入口拒绝私钥", async ({ page }) => {
  const state: Portal3dState = {
    authenticated: true,
    keys: [],
    enrollmentBodies: [],
    activateBodies: [],
  };
  await installPortal3dApi(page, state);
  await openSshTab(page);
  await page.getByRole("button", { name: "导入已有公钥" }).click();
  await page.getByLabel("或粘贴 .pub 内容").fill(PRIVATE_ARMOR);
  await expect(page.locator(".error-box[role='alert']")).toContainText(
    "检测到私钥内容",
  );
  await page.getByLabel("上传 .pub 文件").setInputFiles({
    name: "forbidden.pub",
    mimeType: "text/plain",
    buffer: Buffer.from(PRIVATE_ARMOR),
  });
  await expect(page.locator(".error-box[role='alert']")).toContainText(
    "检测到私钥内容",
  );
  expect(state.enrollmentBodies).toHaveLength(0);

  await page
    .getByLabel("或粘贴 .pub 内容")
    .fill(`${temporaryPublicKey("Oversized import")}${" ".repeat(17 * 1024)}`);
  await expect(page.locator(".error-box[role='alert']")).toContainText(
    "超过 16KB 限制",
  );
  await expect(page.getByRole("button", { name: "校验公钥" })).toBeDisabled();
  expect(state.enrollmentBodies).toHaveLength(0);

  const publicKey = temporaryPublicKey();
  await page.getByLabel("或粘贴 .pub 内容").fill(publicKey);
  await page.getByRole("button", { name: "校验公钥" }).click();
  await expect(
    page.getByText(fingerprint(publicKey), { exact: true }),
  ).toBeVisible();
  await page.getByLabel("我确认这是与我持有的私钥匹配的 SSH 公钥").check();
  await page.getByRole("button", { name: "继续" }).click();
  await expect(page.getByText(/平台没有接收私钥/u)).toBeVisible();
  expect(state.keys).toHaveLength(1);
  expect(state.keys[0].generation_method).toBe("IMPORTED");
});

test("连接入口无 Key 时引导设置，有 VALIDATED Key 时仍阻止 STAGED 连接", async ({
  page,
}) => {
  const state: Portal3dState = {
    authenticated: true,
    keys: [],
    enrollmentBodies: [],
    activateBodies: [],
  };
  await installPortal3dApi(page, state);
  await page.goto("/access");
  await page.getByRole("button", { name: "连接开发容器" }).click();
  await expect(
    page.getByRole("dialog", { name: "SSH 密钥设置" }),
  ).toBeVisible();
  await expect(page.getByText("连接前需要配置 SSH 密钥。")).toBeVisible();
  await page.getByRole("button", { name: "关闭 SSH 密钥设置" }).click();

  const publicKey = temporaryPublicKey("Connection gate");
  state.keys.push({
    id: "00000000-0000-4000-8000-000000000040",
    managed_user_id: MANAGED_USER_ID,
    key_type: "ssh-ed25519",
    fingerprint_sha256: fingerprint(publicKey),
    comment: "Connection gate",
    scope: "BOTH",
    state: "VALIDATED",
    generation_method: "IMPORTED",
    created_at: "2026-08-08T00:00:00Z",
    created_by: USER_ID,
    validated_at: "2026-08-08T00:00:00Z",
    installed_at: null,
    revoked_at: null,
  });
  await page.reload();
  await expect(page.getByRole("button", { name: "连接宿主机" })).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "连接开发容器" }),
  ).toBeDisabled();
  await expect(page.locator(".connection-command")).toHaveCount(0);
  await expect(page.getByText(/authorized_keys 仍为 ABSENT/u)).toBeVisible();
});

test("HOST-only Key 不会被容器连接入口误用", async ({ page }) => {
  const publicKey = temporaryPublicKey("Host scope only");
  const state: Portal3dState = {
    authenticated: true,
    enrollmentBodies: [],
    activateBodies: [],
    keys: [
      {
        id: "00000000-0000-4000-8000-000000000041",
        managed_user_id: MANAGED_USER_ID,
        key_type: "ssh-ed25519",
        fingerprint_sha256: fingerprint(publicKey),
        comment: "Host scope only",
        scope: "HOST",
        state: "VALIDATED",
        generation_method: "IMPORTED",
        created_at: "2026-08-08T00:00:00Z",
        created_by: USER_ID,
        validated_at: "2026-08-08T00:00:00Z",
        installed_at: null,
        revoked_at: null,
      },
    ],
  };
  await installPortal3dApi(page, state);
  await page.goto("/access");
  await expect(page.getByRole("button", { name: "连接宿主机" })).toBeDisabled();
  await page.getByRole("button", { name: "连接开发容器" }).click();
  await expect(
    page.getByRole("dialog", { name: "SSH 密钥设置" }),
  ).toBeVisible();
  await expect(page.getByText("连接前需要配置 SSH 密钥。")).toBeVisible();
});

test("ACTIVE 且 Key 已安装后才显示 SSH 与 VS Code 配置", async ({ page }) => {
  const publicKey = temporaryPublicKey("Installed connection key");
  const state: Portal3dState = {
    authenticated: true,
    computeState: "ACTIVE",
    containerState: "RUNNING",
    enrollmentBodies: [],
    activateBodies: [],
    keys: [
      {
        id: "00000000-0000-4000-8000-000000000042",
        managed_user_id: MANAGED_USER_ID,
        key_type: "ssh-ed25519",
        fingerprint_sha256: fingerprint(publicKey),
        comment: "Installed connection key",
        scope: "BOTH",
        state: "INSTALLED",
        generation_method: "IMPORTED",
        created_at: "2026-08-08T00:00:00Z",
        created_by: USER_ID,
        validated_at: "2026-08-08T00:00:00Z",
        installed_at: "2026-08-08T00:05:00Z",
        revoked_at: null,
      },
    ],
  };
  await installPortal3dApi(page, state);
  await page.goto("/access");
  await page.getByRole("button", { name: "连接宿主机" }).click();
  await expect(page.locator(".connection-command")).toContainText(
    "ssh -i <你的私钥路径> origin-pilot@10.82.36.1",
  );
  await page.getByRole("button", { name: "关闭" }).click();
  await page.getByRole("button", { name: "VS Code Remote SSH" }).click();
  await expect(page.locator(".connection-command")).toContainText(
    "IdentityFile <你的私钥路径>",
  );
  await expect(page.locator(".connection-command")).toContainText("Port 22023");
  await expect(
    page.getByText("USER AUTHENTICATION KEY FINGERPRINT").first(),
  ).toBeVisible();
  await expect(page.getByText("HOST SSH SERVER FINGERPRINT")).toBeVisible();
  await expect(page.getByText("CONTAINER SSH SERVER FINGERPRINT")).toBeVisible();
  await expect(page.getByText("SHA256:test-host-server")).toBeVisible();
  await expect(page.getByText("SHA256:test-container-server")).toBeVisible();
  await expect(page.getByText("PENDING", { exact: true })).toHaveCount(2);
  await expect(page.getByText("OUTSIDE SLURM DENIED")).toBeVisible();
  await expect(page.getByText("sbatch / srun / squeue / sacct / 文件管理")).toBeVisible();
  await expect(
    page.getByText("VS Code Remote SSH / Shell / 开发 / 编译 / 数据准备"),
  ).toBeVisible();
  await expect(page.getByText("NONE", { exact: true })).toBeVisible();
  await expect(page.getByText(/当前调度节点保持 DRAIN/u)).toBeVisible();
});

test("Portal-3F 显示两项真实客户端 PASS 与首个 Pilot 验收结果", async ({ page }) => {
  const publicKey = temporaryPublicKey("Portal-3F installed key");
  const state: Portal3dState = {
    authenticated: true,
    computeState: "ACTIVE",
    containerState: "RUNNING",
    clientValidation: "PASS",
    pilotAcceptance: "PASSED",
    enrollmentBodies: [],
    activateBodies: [],
    keys: [
      {
        id: "00000000-0000-4000-8000-000000000052",
        managed_user_id: MANAGED_USER_ID,
        key_type: "ssh-ed25519",
        fingerprint_sha256: fingerprint(publicKey),
        comment: "Portal-3F installed key",
        scope: "BOTH",
        state: "INSTALLED",
        generation_method: "BROWSER_GENERATED",
        created_at: "2026-08-09T00:00:00Z",
        created_by: USER_ID,
        validated_at: "2026-08-09T00:00:00Z",
        installed_at: "2026-08-09T00:05:00Z",
        revoked_at: null,
      },
    ],
  };
  await installPortal3dApi(page, state);
  await page.goto(`/users/${USER_ID}`);
  await page.getByRole("tab", { name: "计算资源" }).click();
  await expect(
    page.getByText("Client Validation PASS", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("PASSED", { exact: true })).toBeVisible();
  await expect(page.getByText("201", { exact: true })).toBeVisible();
  await expect(page.getByText("202", { exact: true })).toBeVisible();
  await expect(
    page.getByText("GPU-11111111-2222-3333-4444-555555555555"),
  ).toBeVisible();
  await expect(page.getByText(/节点仍等待最终上线审批/u)).toBeVisible();
});

test("ACTIVE + STOPPED 仅显示受控容器启动入口并在成功后开放连接", async ({
  page,
}) => {
  const publicKey = temporaryPublicKey("Installed container start key");
  const state: Portal3dState = {
    authenticated: true,
    computeState: "ACTIVE",
    containerState: "STOPPED",
    enrollmentBodies: [],
    activateBodies: [],
    startBodies: [],
    keys: [
      {
        id: "00000000-0000-4000-8000-000000000043",
        managed_user_id: MANAGED_USER_ID,
        key_type: "ssh-ed25519",
        fingerprint_sha256: fingerprint(publicKey),
        comment: "Installed container start key",
        scope: "BOTH",
        state: "INSTALLED",
        generation_method: "IMPORTED",
        created_at: "2026-08-08T00:00:00Z",
        created_by: USER_ID,
        validated_at: "2026-08-08T00:00:00Z",
        installed_at: "2026-08-08T00:05:00Z",
        revoked_at: null,
      },
    ],
  };
  await installPortal3dApi(page, state);
  await page.goto("/access");
  await expect(
    page.getByRole("button", { name: "启动开发容器" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "连接开发容器" }).click();
  await expect(page.getByText(/请先提交容器启动操作/u)).toBeVisible();
  await page.getByRole("button", { name: "启动开发容器" }).click();
  await expect(page.getByText(/开发容器已安全启动/u)).toBeVisible();
  expect(state.startBodies).toHaveLength(1);
  await expect(page.getByRole("button", { name: "启动开发容器" })).toHaveCount(
    0,
  );
  await page.getByRole("button", { name: "连接开发容器" }).click();
  await expect(page.locator(".connection-command")).toContainText(
    "ssh -i <你的私钥路径> -p 22023 origin-pilot@10.82.36.1",
  );
});

for (const viewport of [
  { width: 1366, height: 768, label: "1366x768" },
  { width: 1920, height: 1080, label: "1920x1080" },
]) {
  test(`Portal-3D-R SSH enrollment visual acceptance ${viewport.label}`, async ({
    page,
  }) => {
    mkdirSync(OUTPUT_DIRECTORY, { recursive: true });
    await page.setViewportSize(viewport);
    const state: Portal3dState = {
      authenticated: true,
      keys: [],
      enrollmentBodies: [],
      activateBodies: [],
    };
    await installPortal3dApi(page, state);
    await openSshTab(page);
    await assertVisualBoundary(page);
    await page.screenshot({
      path: path.join(OUTPUT_DIRECTORY, `${viewport.label}-ssh-key-empty.png`),
    });

    await page.getByRole("button", { name: "生成新密钥" }).click();
    await assertVisualBoundary(page);
    await page.screenshot({
      path: path.join(OUTPUT_DIRECTORY, `${viewport.label}-generate-key.png`),
    });
    await page.getByRole("button", { name: "生成 ED25519 密钥" }).click();
    await expect(page.getByText(/^SHA256:/u)).toBeVisible();
    await assertVisualBoundary(page);
    await page.screenshot({
      path: path.join(
        OUTPUT_DIRECTORY,
        `${viewport.label}-fingerprint-confirm.png`,
      ),
    });
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "下载私钥" }).click();
    await discardDownload(await downloadPromise);
    await page.getByLabel("我已经保存私钥").check();
    await page.screenshot({
      path: path.join(
        OUTPUT_DIRECTORY,
        `${viewport.label}-private-saved-confirm.png`,
      ),
    });
    await page.getByRole("button", { name: "继续" }).click();
    await expect(
      page.getByText("VALIDATED — NOT INSTALLED", { exact: true }),
    ).toBeVisible();
    await assertVisualBoundary(page);
    await page.screenshot({
      path: path.join(OUTPUT_DIRECTORY, `${viewport.label}-key-list.png`),
    });

    await page.getByRole("button", { name: "导入已有公钥" }).click();
    await assertVisualBoundary(page);
    await page.screenshot({
      path: path.join(OUTPUT_DIRECTORY, `${viewport.label}-import-key.png`),
    });
    await page.getByRole("button", { name: "取消" }).click();

    await page.goto("/access");
    await expect(page.getByText("SSH Key 已验证但尚未安装。")).toBeVisible();
    await assertVisualBoundary(page);
    await page.screenshot({
      path: path.join(OUTPUT_DIRECTORY, `${viewport.label}-connect-staged.png`),
    });

    await openSshTab(page);
    await page.getByRole("button", { name: "生成 Activate Dry-Run" }).click();
    await expect(page.getByTestId("activate-dry-run-plan")).toContainText(
      "PLANNED",
    );
    await expect(page.getByTestId("activate-dry-run-plan")).toContainText(
      "DISABLED — ADMINISTRATOR APPROVAL REQUIRED",
    );
    await page.getByTestId("activate-dry-run-plan").scrollIntoViewIfNeeded();
    await assertVisualBoundary(page);
    await page.screenshot({
      path: path.join(
        OUTPUT_DIRECTORY,
        `${viewport.label}-activate-dry-run.png`,
      ),
    });
    expect(state.activateBodies).toHaveLength(1);
  });
}
