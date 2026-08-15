import { expect, test, type Page, type Route } from "@playwright/test";

type Role = "platform_owner" | "user";
type FixtureState = {
  role: Role;
  incidentMode: "FAILED" | "NONE";
  reauthCalls: number;
  recoveryCalls: number;
  mutationCsrf: string[];
  recoveryBody: Record<string, unknown> | null;
};

const USER_ID = "10000000-0000-4000-8000-000000000001";
const USER2_ID = "10000000-0000-4000-8000-000000000002";
const LEASE_ID = "54314628-7b46-4986-bfbd-895f97e0e70f";
const EXPIRY_OPERATION_ID = "90964d2b-c1e0-4c12-a036-2b49c11d8558";
const RECOVERY_OPERATION_ID = "30000000-0000-4000-8000-000000000001";

const owner = {
  id: "00000000-0000-4000-8000-000000000001",
  login_name: "origin-al",
  normalized_login: "origin-al",
  display_name: "Origin-al",
  unix_username: "origin-al",
  account_state: "ACTIVE",
  password_state: "SET",
  resource_onboarding_state: "NOT_ENROLLED",
  roles: [{ name: "platform_owner", description: "平台所有者" }],
};

const lifecycle = {
  has_lease: true,
  lease_id: LEASE_ID,
  owner: "origin-pilot",
  starts_at: "2026-08-10T05:24:55.083442Z",
  expires_at: "2026-08-14T05:24:55.083442Z",
  time_expired: true,
  lease_state: "ACTIVE",
};

const originPilot = {
  id: USER_ID,
  login_name: "origin-pilot",
  normalized_login: "origin-pilot",
  display_name: "Origin Pilot",
  unix_username: "origin-pilot",
  account_state: "ACTIVE",
  password_state: "SET",
  resource_onboarding_state: "ACTIVE",
  created_at: "2026-08-10T05:00:00Z",
  activated_at: "2026-08-10T05:10:00Z",
  last_login_at: "2026-08-10T05:20:00Z",
  roles: [{ name: "user", description: "普通用户" }],
  compute_request: null,
  compute_lifecycle: lifecycle,
  compute_onboarding: {
    status: "ACTIVE",
    compute_username: "origin-pilot",
    draft_state: "ACTIVE",
    ssh_key_status: "SSH_READY",
    plan: null,
  },
  linux_identity: {
    managed_user_id: "20000000-0000-4000-8000-000000000001",
    unix_username: "origin-pilot",
    uid: 20001,
    gid: 20001,
    shell: "/usr/sbin/nologin",
    host_access_state: "DISABLED_BY_PLATFORM_POLICY",
    compute_environment_state: "ACTIVE",
    onboarding_state: "ACTIVE",
    gpu_isolation_state: "VERIFIED",
    ssh_key_state: "SSH_READY",
    ssh_key_count: 1,
    slurm_account: "company",
    slurm_qos: "general",
    project_id: 30001,
    quota_bytes: 322122547200,
    container_name: "gpu-dev-origin-pilot",
    container_state: "RUNNING",
    container_gpu: "NONE",
  },
};

const originPilot2 = {
  id: USER2_ID,
  login_name: "origin-pilot2",
  normalized_login: "origin-pilot2",
  display_name: "Origin Pilot 2",
  unix_username: null,
  account_state: "ACTIVE",
  password_state: "SET",
  resource_onboarding_state: "NOT_ENROLLED",
  created_at: "2026-08-11T05:00:00Z",
  activated_at: "2026-08-11T05:10:00Z",
  last_login_at: null,
  roles: [{ name: "user", description: "普通用户" }],
  compute_request: {
    id: "25aafaf9-b4f8-4cb7-beb0-127ed9923d83",
    status: "FAILED",
    gpu_max: 1,
    submitted_at: "2026-08-11T05:00:00Z",
  },
  compute_lifecycle: { has_lease: false },
  compute_onboarding: {
    status: "NOT_APPLICABLE",
    compute_username: null,
    draft_state: "NOT_APPLICABLE",
    ssh_key_status: "NOT_APPLICABLE",
    plan: null,
  },
  linux_identity: {
    unix_username: null,
    onboarding_state: "NOT_ENROLLED",
    gpu_isolation_state: "NOT_APPLIED",
  },
};

const incident = {
  lease_id: LEASE_ID,
  portal_user_id: USER_ID,
  username: "origin-pilot",
  owner: "origin-pilot",
  starts_at: lifecycle.starts_at,
  expires_at: lifecycle.expires_at,
  current_time: "2026-08-15T00:00:00Z",
  time_expired: true,
  lease_state: "ACTIVE",
  recycle_state: "FAILED",
  last_transition_at: "2026-08-14T05:25:00Z",
  compute_environment_state: "ACTIVE",
  container_name: "gpu-dev-origin-pilot",
  container_desired_state: "RUNNING",
  container_observed_state: "RUNNING",
  connection_authorization_state: "DENIED_EXPIRED_LEASE",
  container_ssh_authorization_state: "KEY_INSTALLED",
  operation_id: EXPIRY_OPERATION_ID,
  operation_type: "lease.expire",
  operation_status: "FAILED",
  operation_started_at: "2026-08-14T05:24:55Z",
  operation_finished_at: "2026-08-14T05:25:00Z",
  error_code: "CONTAINER_STOP_FAILED",
  safe_error_message: "到期回收未完成；需要平台所有者人工确认后重试。",
  attempt_count: null,
  next_retry_at: null,
  manual_review_required: true,
  recovery_available: true,
  data_delete_allowed: false,
};

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function installApi(page: Page, state: FixtureState): Promise<void> {
  await page.context().addCookies([
    {
      name: "h100_csrf",
      value: "lx2-ui-fixture-csrf",
      domain: "127.0.0.1",
      path: "/",
    },
  ]);
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/u, "");
    const mutation = !["GET", "HEAD", "OPTIONS"].includes(request.method());
    if (mutation && path !== "/audit/page-access") {
      const csrf = request.headers()["x-csrf-token"] ?? "";
      state.mutationCsrf.push(csrf);
      if (csrf !== "lx2-ui-fixture-csrf") {
        await json(
          route,
          { detail: { code: "CSRF_REJECTED", message: "CSRF 校验失败" } },
          403,
        );
        return;
      }
    }
    if (path === "/auth/me") {
      await json(route, {
        user: state.role === "platform_owner" ? owner : originPilot,
        role: state.role,
        ssh_enrollment: {
          required: false,
          managed_user_id: null,
          compute_identity: null,
          compute_state: "ACTIVE",
          validated_key_count: 1,
          ssh_key_state: "SSH_READY",
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
      await json(route, { status: "OK", count: 1, alerts: [] });
      return;
    }
    if (path === "/users") {
      await json(route, {
        status: "OK",
        users: [originPilot, originPilot2],
        count: 2,
      });
      return;
    }
    if (path === `/users/${USER_ID}`) {
      await json(route, { status: "OK", user: originPilot });
      return;
    }
    if (path === "/admin/lease-recovery-incidents") {
      if (state.role !== "platform_owner") {
        await json(
          route,
          { detail: { code: "FORBIDDEN", message: "当前账号无权执行此操作" } },
          403,
        );
        return;
      }
      const incidents = state.incidentMode === "FAILED" ? [incident] : [];
      await json(route, { status: "OK", incidents, count: incidents.length });
      return;
    }
    if (path === "/auth/reauthenticate") {
      state.reauthCalls += 1;
      await json(route, { reauthenticated: true });
      return;
    }
    if (path === `/admin/compute-leases/${LEASE_ID}/recycle-retry`) {
      state.recoveryCalls += 1;
      state.recoveryBody = JSON.parse(request.postData() ?? "{}") as Record<
        string,
        unknown
      >;
      await json(route, {
        status: "SUCCEEDED",
        operation_id: RECOVERY_OPERATION_ID,
        lease_id: LEASE_ID,
        idempotent_replay: false,
      });
      return;
    }
    await json(
      route,
      { detail: { code: "NOT_FOUND", message: `fixture missing: ${path}` } },
      404,
    );
  });
}

test("platform_owner can inspect legacy lifecycle and use the fixture-bound Recovery UX", async ({
  page,
}) => {
  const state: FixtureState = {
    role: "platform_owner",
    incidentMode: "FAILED",
    reauthCalls: 0,
    recoveryCalls: 0,
    mutationCsrf: [],
    recoveryBody: null,
  };
  await installApi(page, state);
  await page.goto("/users");

  const pilotRow = page
    .getByRole("row")
    .filter({ hasText: "origin-pilot" })
    .first();
  await expect(pilotRow).toContainText("ACTIVE");
  const details = pilotRow.getByRole("link", { name: "查看详情" });
  await expect(details).toHaveAttribute("href", `/users/${USER_ID}?tab=lease`);
  const pilot2Row = page.getByRole("row").filter({ hasText: "origin-pilot2" });
  await expect(pilot2Row).toContainText("FAILED");
  await expect(pilot2Row.getByRole("link", { name: "查看详情" })).toHaveCount(
    0,
  );

  await details.click();
  await expect(page).toHaveURL(
    new RegExp(`/users/${USER_ID}\\?tab=lease$`, "u"),
  );
  await expect(page.getByText("Lease 生命周期异常")).toBeVisible();
  await expect(page.getByText(LEASE_ID)).toBeVisible();
  await expect(page.getByText("CONTAINER_STOP_FAILED")).toBeVisible();
  await expect(page.getByText("DENIED_EXPIRED_LEASE")).toBeVisible();
  await expect(page.getByRole("button", { name: "重试回收" })).toBeVisible();

  await page.getByRole("tab", { name: "计算资源" }).click();
  await expect(page.getByText("现有受管计算环境")).toBeVisible();
  await expect(page.getByText("计算身份尚未配置")).toHaveCount(0);
  await page.getByRole("tab", { name: "租约 / 生命周期" }).click();

  await page.getByRole("button", { name: "重试回收" }).click();
  const dialog = page.getByRole("dialog", { name: "确认重试回收" });
  await expect(dialog).toBeVisible();
  const confirm = dialog.getByRole("button", { name: "确认重试回收" });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel("输入完整 Lease ID 确认").fill(LEASE_ID);
  await dialog.getByLabel("Recovery 原因").fill("approved fixture recovery");
  await dialog
    .getByLabel("platform_owner 密码（recent re-authentication）")
    .fill("fixture password only");
  await expect(confirm).toBeEnabled();
  await confirm.click();

  await expect(
    page.getByText(RECOVERY_OPERATION_ID, { exact: true }),
  ).toBeVisible();
  expect(state.reauthCalls).toBe(1);
  expect(state.recoveryCalls).toBe(1);
  expect(state.mutationCsrf).toEqual([
    "lx2-ui-fixture-csrf",
    "lx2-ui-fixture-csrf",
  ]);
  expect(state.recoveryBody).toEqual(
    expect.objectContaining({
      confirmation: LEASE_ID,
      safe_reason: "approved fixture recovery",
      idempotency_key: expect.any(String),
    }),
  );
  expect(state.recoveryBody).not.toHaveProperty("password");
});

test("ordinary user never receives the administrator Recovery action", async ({
  page,
}) => {
  const state: FixtureState = {
    role: "user",
    incidentMode: "FAILED",
    reauthCalls: 0,
    recoveryCalls: 0,
    mutationCsrf: [],
    recoveryBody: null,
  };
  await installApi(page, state);
  await page.goto(`/users/${USER_ID}?tab=lease`);
  await expect(page.getByText(LEASE_ID)).toBeVisible();
  await expect(page.getByRole("button", { name: "重试回收" })).toHaveCount(0);
  expect(state.reauthCalls).toBe(0);
  expect(state.recoveryCalls).toBe(0);
});
