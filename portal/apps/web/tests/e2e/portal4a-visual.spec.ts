import { mkdirSync } from "node:fs";
import path from "node:path";

import { expect, test, type Page, type Route } from "@playwright/test";

type LeaseMode = "normal" | "warning" | "expired";
type RecycleMode = "empty" | "expired" | "restore-pending" | "failed";

type Portal4aState = {
  authenticated: boolean;
  passwordState: "RESET_REQUIRED" | "SET";
  leaseMode: LeaseMode;
  remainingSeconds: number;
  renewalPending: boolean;
  recycleMode: RecycleMode;
  jobs: Array<Record<string, unknown>>;
  jobBodies: Array<Record<string, unknown>>;
};

const USER_ID = "00000000-0000-4000-8000-000000000041";
const MANAGED_USER_ID = "00000000-0000-4000-8000-000000000030";
const LEASE_ID = "00000000-0000-4000-8000-000000000042";
const RECYCLE_ID = "00000000-0000-4000-8000-000000000043";
const OUTPUT_DIRECTORY =
  process.env.PORTAL4A_SCREENSHOT_DIR ??
  path.resolve("test-results", "portal4a-r-visual-acceptance");

function user(state: Portal4aState) {
  return {
    id: USER_ID,
    login_name: "origin-pilot",
    normalized_login: "origin-pilot",
    display_name: "Origin Pilot",
    unix_username: "origin-pilot",
    account_state: "ACTIVE",
    password_state: state.passwordState,
    resource_onboarding_state:
      state.leaseMode === "expired" ? "RECYCLED" : "ACTIVE",
    roles: [{ name: "user", description: "普通用户" }],
  };
}

const enrollment = {
  required: false,
  managed_user_id: MANAGED_USER_ID,
  compute_identity: "origin-pilot",
  compute_state: "ACTIVE",
  validated_key_count: 1,
  ssh_key_state: "INSTALLED",
  setup_path: null,
};

function lease(state: Portal4aState) {
  const active = state.leaseMode !== "expired";
  const expiresAt =
    state.leaseMode === "expired"
      ? "2026-08-09T20:00:00Z"
      : state.leaseMode === "warning"
        ? "2026-08-10T19:00:00Z"
        : "2026-08-13T20:00:00Z";
  return {
    id: LEASE_ID,
    state: active
      ? state.renewalPending
        ? "RENEWAL_PENDING"
        : state.leaseMode === "warning"
          ? "RENEWAL_WINDOW"
          : "ACTIVE"
      : "EXPIRED",
    active,
    starts_at: "2026-08-09T20:00:00Z",
    expires_at: expiresAt,
    remaining_seconds: active ? state.remainingSeconds : 0,
    renewal_available: active && state.leaseMode === "warning",
    renewal_available_from: "2026-08-09T19:00:00Z",
    gpu_count: 1,
    max_duration_seconds: 345600,
    renewal_window_seconds: 86400,
    auto_renew: false,
    restore_required: !active,
    pending_renewal_id: state.renewalPending
      ? "00000000-0000-4000-8000-000000000044"
      : null,
  };
}

function container(state: Portal4aState) {
  const active = state.leaseMode !== "expired";
  return {
    id: "00000000-0000-4000-8000-000000000045",
    name: "gpu-dev-origin-pilot",
    state: active ? "RUNNING" : "STOPPED",
    connection_state: active ? "AVAILABLE" : "DISABLED",
    profile: "STANDARD_8CPU_32GB",
    gpu: 0,
    gpu_allocation_state: "NONE",
    cpus: 8,
    memory_gb: 32,
    pids_limit: 4096,
  };
}

function environment(state: Portal4aState) {
  const active = state.leaseMode !== "expired";
  return {
    state: active ? "ACTIVE" : "RECYCLED",
    gpu_max: 1,
    host_access: "DISABLED",
    job_submission: active ? "AVAILABLE" : "DISABLED",
    lease: lease(state),
    container: container(state),
    storage: {
      quota_bytes: 300 * 1024 ** 3,
      state: "ACTIVE",
      workspace: "/storage/users/20001",
      container_mount: "/workspace",
      default_job_workdir: "/storage/users/20001/projects",
    },
  };
}

const key = {
  id: "00000000-0000-4000-8000-000000000046",
  managed_user_id: MANAGED_USER_ID,
  key_type: "ssh-ed25519",
  fingerprint_sha256: "SHA256:nek6vyEb3GT+UJAcY5y/8PgY4achF2ouNy+8C2JqUVc",
  comment: "Origin laptop",
  scope: "CONTAINER",
  state: "INSTALLED",
  generation_method: "BROWSER_GENERATED",
  created_at: "2026-08-08T08:00:00Z",
  created_by: USER_ID,
  validated_at: "2026-08-08T08:00:00Z",
  installed_at: "2026-08-09T16:00:00Z",
  revoked_at: null,
};

function recycleItems(state: Portal4aState) {
  if (state.recycleMode === "empty") return [];
  return [
    {
      id: RECYCLE_ID,
      resource_name: "gpu-dev-origin-pilot",
      state:
        state.recycleMode === "restore-pending"
          ? "RESTORE_PENDING"
          : state.recycleMode === "failed"
            ? "FAILED"
            : "RECYCLE_BIN",
      expires_at: "2026-08-09T20:00:00Z",
      recycled_at: "2026-08-09T20:01:00Z",
      data_preserved: true,
      auto_permanent_delete: false,
      container: "STOPPED",
    },
  ];
}

function submittedJob(body: Record<string, unknown>) {
  return {
    id: "00000000-0000-4000-8000-000000000047",
    slurm_job_id: 35,
    name: body.name,
    state: "COMPLETED",
    cpus: body.cpus,
    memory_mb: body.memory_mb,
    gpu_count: body.gpu_count,
    time_limit_seconds: body.time_limit_seconds,
    script_path: ".portal/job-scripts/00000000-0000-4000-8000-000000000047.sh",
    workdir: "projects",
    stdout_path: "outputs/00000000-0000-4000-8000-000000000047.out",
    stderr_path: "outputs/00000000-0000-4000-8000-000000000047.err",
    lease_deadline_at: "2026-08-13T20:00:00Z",
    created_at: "2026-08-09T21:00:00Z",
    submitted_at: "2026-08-09T21:00:01Z",
    finished_at: "2026-08-09T21:00:08Z",
    exit_code: "0:0",
  };
}

async function json(
  route: Route,
  body: unknown,
  status = 200,
  headers: Record<string, string> = {},
): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    headers,
    body: JSON.stringify(body),
  });
}

async function installPortal4aApi(
  page: Page,
  state: Portal4aState,
): Promise<void> {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const requestPath = new URL(request.url()).pathname.replace(
      /^\/api\/v1/u,
      "",
    );
    if (requestPath === "/auth/csrf") {
      await json(route, { csrf_token: "portal4a-test-csrf" }, 200, {
        "set-cookie": "h100_csrf=portal4a-test-csrf; Path=/; SameSite=Strict",
      });
      return;
    }
    if (requestPath === "/auth/login") {
      state.authenticated = true;
      await json(route, { user: user(state), ssh_enrollment: enrollment });
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
        user: user(state),
        role: "user",
        ssh_enrollment: enrollment,
      });
      return;
    }
    if (requestPath === "/auth/password" && request.method() === "POST") {
      state.passwordState = "SET";
      await json(route, { changed: true });
      return;
    }
    if (requestPath === "/audit/page-access") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (requestPath === "/self/environment") {
      await json(route, { status: "OK", environment: environment(state) });
      return;
    }
    if (requestPath === "/self/lease/renewals") {
      state.renewalPending = true;
      await json(route, {
        status: "REQUESTED",
        renewal_request_id: "00000000-0000-4000-8000-000000000044",
      });
      return;
    }
    if (requestPath === "/self/container") {
      await json(route, { status: "OK", container: container(state) });
      return;
    }
    if (requestPath === "/self/container/connection") {
      const active = state.leaseMode !== "expired";
      await json(route, {
        status: "OK",
        connection: {
          available: active,
          host: "20.10.10.3",
          port: 22023,
          username: "origin-pilot",
          authentication: "SSH_PUBLIC_KEY",
          gpu: "NONE",
          key_fingerprint: key.fingerprint_sha256,
          command: active
            ? "ssh -i <你的私钥路径> -p 22023 origin-pilot@20.10.10.3"
            : null,
          vscode: active
            ? "Host h100-origin-pilot-dev\n    HostName 20.10.10.3\n    Port 22023\n    User origin-pilot\n    IdentityFile <你的私钥路径>"
            : null,
        },
      });
      return;
    }
    if (requestPath === "/self/jobs") {
      if (request.method() === "POST") {
        const body = request.postDataJSON() as Record<string, unknown>;
        state.jobBodies.push(body);
        const job = submittedJob(body);
        state.jobs = [job];
        await json(route, { status: "SUBMITTED", job });
        return;
      }
      await json(route, {
        status: "OK",
        jobs: state.jobs,
        count: state.jobs.length,
      });
      return;
    }
    if (/^\/self\/jobs\/[^/]+\/logs$/u.test(requestPath)) {
      await json(route, {
        status: "OK",
        stdout: "portal4a job completed\n",
        stderr: "",
      });
      return;
    }
    if (/^\/self\/jobs\/[^/]+\/cancel$/u.test(requestPath)) {
      await json(route, { status: "CANCELLED", job: state.jobs[0] });
      return;
    }
    if (requestPath === "/self/storage") {
      await json(route, {
        status: "OK",
        storage: {
          root: "/storage/users/20001",
          quota_bytes: 300 * 1024 ** 3,
          used_bytes: 42 * 1024 ** 3,
          available_bytes: 258 * 1024 ** 3,
          state: "ACTIVE",
          private: true,
        },
      });
      return;
    }
    if (requestPath === "/self/recycle-bin") {
      await json(route, {
        status: "OK",
        items: recycleItems(state),
        auto_permanent_delete: false,
      });
      return;
    }
    if (
      requestPath === `/self/recycle-bin/${RECYCLE_ID}/restore-requests` &&
      request.method() === "POST"
    ) {
      state.recycleMode = "empty";
      state.leaseMode = "normal";
      await json(route, {
        status: "RESTORED",
        restore_request_id: "00000000-0000-4000-8000-000000000048",
        lease_id: "00000000-0000-4000-8000-000000000049",
      });
      return;
    }
    if (requestPath === `/users/${USER_ID}/ssh-keys`) {
      await json(route, {
        status: "OK",
        keys: [key],
        count: 1,
        maximum_active_keys: 5,
        enrollment,
      });
      return;
    }
    if (
      requestPath === "/users" ||
      requestPath.startsWith("/platform/") ||
      requestPath.startsWith("/operations")
    ) {
      await json(
        route,
        { detail: { code: "PERMISSION_DENIED", message: "当前账号无权访问" } },
        403,
      );
      return;
    }
    await json(
      route,
      { detail: { code: "TEST_ROUTE_MISSING", message: requestPath } },
      404,
    );
  });
}

async function assertVisualBoundary(page: Page): Promise<void> {
  await expect(page.getByText(/AI 助手|智能助手|AI 洞察/u)).toHaveCount(0);
  const result = await page.evaluate(() => {
    const cards = [...document.querySelectorAll(".ui-card")];
    return {
      horizontalOverflow:
        document.documentElement.scrollWidth > window.innerWidth,
      invalidCards: cards.filter((card) => {
        const style = window.getComputedStyle(card);
        return (
          !["", "none"].includes(style.backgroundImage) ||
          !["", "none"].includes(style.backdropFilter) ||
          Number.parseFloat(style.borderTopLeftRadius || "0") > 8
        );
      }).length,
    };
  });
  expect(result.horizontalOverflow).toBe(false);
  expect(result.invalidCards).toBe(0);
}

async function capture(
  page: Page,
  viewportLabel: string,
  name: string,
): Promise<void> {
  await page.addStyleTag({
    content: "nextjs-portal { display: none !important; }",
  });
  await assertVisualBoundary(page);
  await page.screenshot({
    path: path.join(OUTPUT_DIRECTORY, `${viewportLabel}-${name}.png`),
    fullPage: false,
    animations: "disabled",
  });
}

for (const viewport of [
  { width: 1366, height: 768, label: "1366x768" },
  { width: 1920, height: 1080, label: "1920x1080" },
]) {
  test(`Portal-4A-R ordinary user visual acceptance ${viewport.label}`, async ({
    page,
  }) => {
    mkdirSync(OUTPUT_DIRECTORY, { recursive: true });
    await page.setViewportSize(viewport);
    const state: Portal4aState = {
      authenticated: false,
      passwordState: "RESET_REQUIRED",
      leaseMode: "normal",
      remainingSeconds: 72 * 3600,
      renewalPending: false,
      recycleMode: "empty",
      jobs: [],
      jobBodies: [],
    };
    await installPortal4aApi(page, state);

    await page.goto("/login");
    await expect(
      page.getByRole("heading", { name: "Origin Forge" }),
    ).toBeVisible();
    await capture(page, viewport.label, "01-login");

    await page.getByLabel("登录名").fill("origin-pilot");
    await page.getByLabel("网页密码").fill("visual-fixture-password");
    await page.getByRole("button", { name: "登录" }).click();
    await expect(
      page.getByRole("heading", { name: "首次登录修改密码" }),
    ).toBeVisible();
    await capture(page, viewport.label, "02-first-password-change");

    await page.getByLabel("临时密码").fill("visual-fixture-password");
    await page.getByLabel("新Portal密码").fill("visual-fixture-new-password");
    await page.getByLabel("再次输入").fill("visual-fixture-new-password");
    await page.getByRole("button", { name: "保存并进入我的环境" }).click();
    await expect(page.getByRole("heading", { name: "我的环境" })).toBeVisible();
    await expect(page.getByText("Host SSH 已按普通用户策略关闭")).toBeVisible();
    await capture(page, viewport.label, "03-my-environment");

    state.remainingSeconds = 48 * 3600;
    await page.goto("/");
    await expect(
      page.getByText("剩余 2天 0小时 · 每次最多续期4天"),
    ).toBeVisible();
    await capture(page, viewport.label, "04-lease-over-24h");

    state.leaseMode = "warning";
    state.remainingSeconds = 23 * 3600 + 30 * 60;
    await page.goto("/");
    await expect(
      page.getByText("剩余 23小时 30分 · 每次最多续期4天"),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "申请续期" })).toBeEnabled();
    await capture(page, viewport.label, "05-lease-final-24h");

    await page.getByRole("button", { name: "申请续期" }).click();
    await expect(page.getByText("续期申请待审批")).toBeVisible();
    await capture(page, viewport.label, "06-renewal-requested");

    await page.goto("/access");
    await expect(page.getByRole("heading", { name: "连接" })).toBeVisible();
    await expect(page.getByText("22023", { exact: true })).toBeVisible();
    await expect(
      page.getByText(/宿主环境|Host port 22|Host VS Code/u),
    ).toHaveCount(0);
    await capture(page, viewport.label, "07-container-only-connection");

    await page.goto("/jobs");
    await expect(page.getByLabel("执行脚本")).toBeVisible();
    await page
      .getByLabel("执行脚本")
      .fill('set -eu\necho "portal script"\nwhoami\nid');
    await page.getByLabel("GPU").selectOption("1");
    await page.getByLabel("运行环境").selectOption("approved");
    await expect(page.getByRole("button", { name: "提交作业" })).toBeEnabled();
    await capture(page, viewport.label, "08-job-submit");

    await page.getByRole("button", { name: "提交作业" }).click();
    await expect(page.getByText("35", { exact: true })).toBeVisible();
    await expect(page.getByText("COMPLETED", { exact: true })).toBeVisible();
    await page.getByText("我的作业", { exact: true }).scrollIntoViewIfNeeded();
    await capture(page, viewport.label, "09-my-jobs");
    expect(state.jobBodies).toHaveLength(1);
    expect(state.jobBodies[0]?.gpu_count).toBe(1);
    expect(state.jobBodies[0]?.script).toContain("portal script");
    expect(state.jobBodies[0]).not.toHaveProperty("script_path");
    expect(state.jobBodies[0]).not.toHaveProperty("workdir");

    await page.goto("/containers");
    await expect(
      page.getByText("gpu-dev-origin-pilot", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("NONE", { exact: true })).toBeVisible();
    await expect(page.getByText(/Docker Socket|Privileged/u)).toHaveCount(0);
    await capture(page, viewport.label, "10-development-container");

    await page.goto("/terminal");
    await expect(page.getByRole("heading", { name: "网页终端" })).toBeVisible();
    await expect(page.getByText("容器 Shell，不是宿主 Shell")).toBeVisible();
    await expect(page.getByRole("button", { name: "打开终端" })).toBeEnabled();
    await expect(page.getByText(/宿主访问均不可用/u)).toBeVisible();
    await capture(page, viewport.label, "10b-web-terminal");

    await page.goto("/storage");
    await expect(page.getByText("300.0 GB", { exact: true })).toBeVisible();
    await capture(page, viewport.label, "11-storage");

    await page.goto("/ssh-keys");
    await expect(page.getByText("用途：仅开发容器")).toBeVisible();
    await expect(page.getByText("宿主 SSH：未启用")).toBeVisible();
    await capture(page, viewport.label, "12-ssh-keys");

    await page.goto("/recycle-bin");
    await expect(page.getByText("回收站为空")).toBeVisible();
    await capture(page, viewport.label, "13-recycle-bin-empty");

    state.leaseMode = "expired";
    state.recycleMode = "expired";
    await page.goto("/recycle-bin");
    await expect(page.getByText("已过期", { exact: true })).toBeVisible();
    await expect(page.getByText("已保留", { exact: true })).toBeVisible();
    await capture(page, viewport.label, "14-recycle-bin-expired");

    state.recycleMode = "failed";
    await page.goto("/recycle-bin");
    await expect(page.getByText("恢复失败", { exact: true })).toBeVisible();
    const failedRestore = page.getByRole("button", { name: "恢复容器" });
    await expect(failedRestore).toBeEnabled();
    await failedRestore.click();
    await expect(
      page.getByText("恢复完成，新的 96 小时 Lease 已创建。", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("回收站为空")).toBeVisible();
    await capture(page, viewport.label, "15-owner-self-restored");

    await page.goto("/users");
    await expect(page.getByText("当前账号无权查看此模块。")).toBeVisible();
    await expect(
      page.getByText(/用户管理|Approval Admin|Audit Global/u),
    ).toHaveCount(0);
    await capture(page, viewport.label, "16-unauthorized-admin-route");
  });
}
