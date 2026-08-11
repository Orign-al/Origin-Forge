import { expect, test, type Page, type Route } from "@playwright/test";

type Role = "user" | "platform_admin";
type State = {
  role: Role;
  requestStatus: string | null;
  reviewNote?: string | null;
  planState?: string | null;
  mutationCsrf: string[];
  submittedBody?: Record<string, unknown>;
  provisionCalls: number;
};

const REQUEST_ID = "10000000-0000-4000-8000-000000000001";
const ACCOUNT_ID = "10000000-0000-4000-8000-000000000002";
const PLAN_ID = "10000000-0000-4000-8000-000000000003";

const ordinaryUser = {
  id: ACCOUNT_ID,
  login_name: "origin-pilot2",
  normalized_login: "origin-pilot2",
  display_name: "Origin Pilot 2",
  unix_username: null,
  account_state: "ACTIVE",
  password_state: "SET",
  resource_onboarding_state: "NOT_ENROLLED",
  created_at: "2026-08-11T10:00:00Z",
  activated_at: "2026-08-11T10:10:00Z",
  last_login_at: "2026-08-11T10:20:00Z",
  roles: [{ name: "user", description: "普通用户" }],
};

const adminUser = {
  ...ordinaryUser,
  id: "10000000-0000-4000-8000-000000000010",
  login_name: "fixture-admin",
  normalized_login: "fixture-admin",
  display_name: "Fixture Admin",
  roles: [{ name: "platform_admin", description: "平台管理员" }],
};

function plan(state: State) {
  if (!state.planState) return null;
  const checks = [
    { check: "linux_username_available", status: "PASS", detail: "read only" },
    {
      check: "xfs_project_quota_capability",
      status: "PASS",
      detail: "read only",
    },
    {
      check: "gpu_bypass_guard_readiness",
      status: "PASS",
      detail: "timer only",
    },
  ];
  return {
    id: PLAN_ID,
    state: state.planState,
    username: "origin-pilot2",
    uid: 20002,
    gid: 20002,
    project_id: 30002,
    container_name: "gpu-dev-origin-pilot2",
    container_ssh_port: 22024,
    storage_bytes: 300 * 1024 ** 3,
    container_profile: "STANDARD_8CPU_32GB",
    container_cpus: 8,
    container_memory_gb: 32,
    container_pids_limit: 4096,
    container_gpu: 0,
    slurm_account: "company",
    slurm_qos: "general",
    gpu_max: 1,
    lease_seconds: 345600,
    lease_state: "NOT_STARTED",
    host_ssh: "DISABLED",
    shell: "/usr/sbin/nologin",
    password_state: "LOCKED",
    execution_enabled: false,
    reservation_expires_at: "2026-08-12T12:00:00Z",
    dry_run_at:
      state.planState === "READY_FOR_PROVISION" ? "2026-08-11T12:10:00Z" : null,
    allocator_result: { validation_results: checks },
    dry_run_result:
      state.planState === "READY_FOR_PROVISION"
        ? { validation_results: checks, infrastructure_side_effects: "NONE" }
        : null,
  };
}

function computeRequest(state: State, internal = false) {
  if (!state.requestStatus) return null;
  return {
    id: REQUEST_ID,
    portal_account_id: ACCOUNT_ID,
    ...(internal
      ? {
          requested_by: ACCOUNT_ID,
          managed_user_id: null,
          reviewed_by: null,
          portal_user: {
            login_name: "origin-pilot2",
            display_name: "Origin Pilot 2",
            role: "user",
            account_state: "ACTIVE",
            password_state: "SET",
            compute_state: "NOT_ENROLLED",
          },
        }
      : {}),
    username: "origin-pilot2",
    status: state.requestStatus,
    requested_gpu_max: 1,
    requested_storage_bytes: 300 * 1024 ** 3,
    requested_container_profile: "STANDARD_8CPU_32GB",
    requested_lease_seconds: 345600,
    purpose: '<img src=x onerror="alert(1)"> 多用户平台验收',
    user_note: null,
    submitted_at: "2026-08-11T12:00:00Z",
    review_note: state.reviewNote ?? null,
    reviewed_at:
      state.requestStatus === "REQUESTED" ? null : "2026-08-11T12:05:00Z",
    approved_at:
      state.requestStatus === "REQUESTED" ? null : "2026-08-11T12:05:00Z",
    rejected_at:
      state.requestStatus === "REJECTED" ? "2026-08-11T12:05:00Z" : null,
    cancelled_at: null,
    created_at: "2026-08-11T12:00:00Z",
    updated_at: "2026-08-11T12:05:00Z",
    plan: plan(state),
  };
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function installApi(page: Page, state: State): Promise<void> {
  await page.context().addCookies([
    {
      name: "h100_csrf",
      value: "portal5a1a-csrf",
      domain: "127.0.0.1",
      path: "/",
    },
  ]);
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/, "");
    const mutation = !["GET", "HEAD", "OPTIONS"].includes(request.method());
    if (mutation && path !== "/audit/page-access") {
      const csrf = request.headers()["x-csrf-token"] ?? "";
      state.mutationCsrf.push(csrf);
      if (csrf !== "portal5a1a-csrf") {
        await json(
          route,
          { detail: { code: "CSRF_REJECTED", message: "CSRF 校验失败" } },
          403,
        );
        return;
      }
    }
    if (path === "/auth/me") {
      const user = state.role === "user" ? ordinaryUser : adminUser;
      await json(route, {
        user,
        role: state.role,
        ssh_enrollment: {
          required: false,
          managed_user_id: null,
          compute_identity: null,
          compute_state: "NOT_ENROLLED",
          validated_key_count: 0,
          ssh_key_state: "NOT_APPLICABLE",
          setup_path: null,
        },
      });
      return;
    }
    if (path === "/audit/page-access") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (path === "/platform/alerts") {
      await json(route, { status: "OK", count: 0, alerts: [] });
      return;
    }
    if (path === "/self/compute-request") {
      if (state.role !== "user") {
        await json(
          route,
          { detail: { code: "FORBIDDEN", message: "当前账号无权执行此操作" } },
          403,
        );
        return;
      }
      if (request.method() === "GET") {
        await json(route, {
          status: "OK",
          compute_identity: "NOT_PROVISIONED",
          request: computeRequest(state),
        });
        return;
      }
      if (
        state.requestStatus &&
        !["REJECTED", "CANCELLED"].includes(state.requestStatus)
      ) {
        await json(
          route,
          {
            detail: {
              code: "ACTIVE_COMPUTE_REQUEST_EXISTS",
              message: "已有进行中的计算资源申请",
            },
          },
          409,
        );
        return;
      }
      const body = request.postDataJSON() as Record<string, unknown>;
      state.submittedBody = body;
      state.requestStatus = "REQUESTED";
      await json(
        route,
        { status: "REQUESTED", request: computeRequest(state) },
        201,
      );
      return;
    }
    if (path === `/self/compute-request/${REQUEST_ID}/cancel`) {
      if (state.role !== "user") {
        await json(
          route,
          { detail: { code: "COMPUTE_REQUEST_NOT_FOUND", message: "不存在" } },
          404,
        );
        return;
      }
      state.requestStatus = "CANCELLED";
      await json(route, {
        status: "CANCELLED",
        request: computeRequest(state),
      });
      return;
    }
    if (
      path.startsWith("/admin/compute-resource-requests") &&
      state.role !== "platform_admin"
    ) {
      await json(
        route,
        { detail: { code: "FORBIDDEN", message: "当前账号无权执行此操作" } },
        403,
      );
      return;
    }
    if (path === "/admin/compute-resource-requests") {
      const item = computeRequest(state, true);
      await json(route, {
        status: "OK",
        requests: item ? [item] : [],
        count: item ? 1 : 0,
      });
      return;
    }
    if (path === `/admin/compute-resource-requests/${REQUEST_ID}`) {
      await json(route, { status: "OK", request: computeRequest(state, true) });
      return;
    }
    if (path === `/admin/compute-resource-requests/${REQUEST_ID}/review`) {
      const body = request.postDataJSON() as {
        decision: string;
        review_note: string | null;
      };
      state.requestStatus =
        body.decision === "APPROVE" ? "APPROVED" : "REJECTED";
      state.reviewNote = body.review_note;
      await json(route, {
        status: state.requestStatus,
        request: computeRequest(state, true),
      });
      return;
    }
    if (path === `/admin/compute-resource-requests/${REQUEST_ID}/plan`) {
      state.requestStatus = "PROVISION_PLAN_READY";
      state.planState = "RESERVED";
      await json(route, { status: "RESERVED", plan: plan(state) });
      return;
    }
    if (path === `/admin/compute-resource-requests/${REQUEST_ID}/dry-run`) {
      state.planState = "READY_FOR_PROVISION";
      await json(route, { status: "READY_FOR_PROVISION", plan: plan(state) });
      return;
    }
    if (path === `/admin/compute-resource-requests/${REQUEST_ID}/provision`) {
      state.provisionCalls += 1;
      await json(
        route,
        {
          detail: {
            code: "PROVISION_EXECUTION_DISABLED_NEXT_GATE",
            message: "正式创建计算环境需要下一阶段管理员确认",
          },
        },
        409,
      );
      return;
    }
    await json(
      route,
      { detail: { code: "NOT_FOUND", message: "fixture route not found" } },
      404,
    );
  });
}

test("普通用户提交固定标准申请并进入审批中状态", async ({ page }) => {
  const state: State = {
    role: "user",
    requestStatus: null,
    mutationCsrf: [],
    provisionCalls: 0,
  };
  await installApi(page, state);
  await page.goto("/");
  await expect(page.getByRole("link", { name: "申请计算资源" })).toBeVisible();
  await expect(page.getByRole("link", { name: "作业" })).toHaveCount(0);
  await page.getByRole("link", { name: "申请计算资源" }).click();
  await expect(page.getByText("标准开发环境", { exact: true })).toBeVisible();
  await expect(page.getByText("Container GPU", { exact: true })).toBeVisible();
  await expect(page.getByText("NONE", { exact: true })).toBeVisible();
  await page.getByLabel("GPU 最大数量").selectOption("1");
  await page.getByLabel("用途 / Project Description").fill("多用户平台验收");
  await page.getByRole("button", { name: "提交申请" }).click();
  await expect(
    page.getByRole("heading", { name: "等待管理员审批" }),
  ).toBeVisible();
  await expect(page.getByText(`申请 ID：${REQUEST_ID}`)).toBeVisible();
  expect(state.submittedBody).toMatchObject({
    requested_gpu_max: 1,
    requested_storage_bytes: 300 * 1024 ** 3,
    requested_container_profile: "STANDARD_8CPU_32GB",
    requested_lease_seconds: 345600,
    purpose: "多用户平台验收",
  });
  expect(state.submittedBody).not.toHaveProperty("uid");
  expect(state.submittedBody).not.toHaveProperty("ssh_port");
  expect(state.mutationCsrf).toContain("portal5a1a-csrf");
});

test("进行中申请禁止重复且普通用户不能访问管理员审批对象", async ({ page }) => {
  const state: State = {
    role: "user",
    requestStatus: "REQUESTED",
    mutationCsrf: [],
    provisionCalls: 0,
  };
  await installApi(page, state);
  await page.goto("/compute-request");
  await expect(
    page.getByRole("heading", { name: "等待管理员审批" }),
  ).toBeVisible();
  const abuse = await page.evaluate(async (requestId) => {
    const csrf =
      document.cookie
        .split("; ")
        .find((item) => item.startsWith("h100_csrf="))
        ?.split("=")[1] ?? "";
    const duplicate = await fetch("/api/v1/self/compute-request", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({
        requested_gpu_max: 1,
        requested_storage_bytes: 322122547200,
        requested_container_profile: "STANDARD_8CPU_32GB",
        requested_lease_seconds: 345600,
        purpose: "duplicate",
        user_note: null,
        idempotency_key: "10000000-0000-4000-8000-000000000099",
      }),
    });
    const admin = await fetch(
      `/api/v1/admin/compute-resource-requests/${requestId}`,
    );
    return { duplicate: duplicate.status, admin: admin.status };
  }, REQUEST_ID);
  expect(abuse).toEqual({ duplicate: 409, admin: 403 });
  await page.goto(`/compute-requests/${REQUEST_ID}`);
  await expect(page.getByText(/无权查看/)).toBeVisible();
});

test("管理员批准、reservation 和 dry-run 停在下一 Human Gate", async ({
  page,
}) => {
  const state: State = {
    role: "platform_admin",
    requestStatus: "REQUESTED",
    mutationCsrf: [],
    provisionCalls: 0,
  };
  await installApi(page, state);
  await page.goto(`/compute-requests/${REQUEST_ID}`);
  await expect(
    page.getByText("origin-pilot2", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.locator("img")).toHaveCount(0);
  await page.getByRole("button", { name: "批准" }).click();
  await expect(
    page.getByRole("button", { name: "生成 Provision Plan" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "生成 Provision Plan" }).click();
  await expect(page.getByText("20002 / 20002")).toBeVisible();
  await expect(page.getByText("30002", { exact: true })).toBeVisible();
  await expect(
    page.getByText("gpu-dev-origin-pilot2", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("NOT STARTED", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "执行 Dry-run" }).click();
  await expect(
    page.getByText("READY_FOR_PROVISION", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "正式创建计算环境" }),
  ).toBeDisabled();
  await expect(page.getByText(/PROVISION EXECUTION: DISABLED/)).toBeVisible();
  expect(state.provisionCalls).toBe(0);
  expect(state.mutationCsrf.every((value) => value === "portal5a1a-csrf")).toBe(
    true,
  );
});

test("拒绝需要备注并允许用户看到安全文本原因", async ({ page }) => {
  const state: State = {
    role: "platform_admin",
    requestStatus: "REQUESTED",
    mutationCsrf: [],
    provisionCalls: 0,
  };
  await installApi(page, state);
  await page.goto(`/compute-requests/${REQUEST_ID}`);
  await expect(
    page.getByRole("button", { name: "拒绝（需备注）" }),
  ).toBeDisabled();
  await page.getByLabel("审批备注").fill("资源暂不可用 <b>unsafe</b>");
  await page.getByRole("button", { name: "拒绝（需备注）" }).click();
  await expect(page.getByText(/申请已拒绝/)).toBeVisible();
  await expect(page.locator("b")).toHaveCount(0);
  expect(state.requestStatus).toBe("REJECTED");
});
