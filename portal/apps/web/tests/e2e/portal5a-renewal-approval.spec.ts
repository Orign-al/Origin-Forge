import { expect, test, type Page, type Route } from "@playwright/test";

type Role = "platform_owner" | "platform_admin" | "operator" | "auditor";
type RenewalState = {
  role: Role;
  recentAuthValid: boolean;
  requests: RenewalRequest[];
  reauthenticateCalls: number;
  decisions: Array<Record<string, unknown>>;
};

type RenewalRequest = {
  id: string;
  owner_managed_user_id: string;
  username: string;
  display_name: string;
  lease_id: string;
  lease_state: string;
  lease_expires_at: string;
  state: "REQUESTED" | "APPROVED" | "REJECTED" | "CANCELLED";
  approval_required: boolean;
  duration_seconds: number;
  requested_at: string;
  decided_at: string | null;
  decision_comment: string | null;
  resulting_lease_id: string | null;
  actionable: boolean;
  closed_reason: string | null;
};

const REQUEST_ID = "b7378cf1-e567-4529-adef-7bc927fb4104";

function renewal(overrides: Partial<RenewalRequest> = {}): RenewalRequest {
  return {
    id: REQUEST_ID,
    owner_managed_user_id: "20000000-0000-4000-8000-000000000001",
    username: "luchenxi",
    display_name: "陆晨曦",
    lease_id: "30000000-0000-4000-8000-000000000001",
    lease_state: "RENEWAL_PENDING",
    lease_expires_at: "2026-09-06T14:50:00Z",
    state: "REQUESTED",
    approval_required: true,
    duration_seconds: 96 * 3600,
    requested_at: "2026-09-06T03:02:58Z",
    decided_at: null,
    decision_comment: null,
    resulting_lease_id: null,
    actionable: true,
    closed_reason: null,
    ...overrides,
  };
}

function user(role: Role) {
  return {
    id: "10000000-0000-4000-8000-000000000001",
    login_name: role,
    normalized_login: role,
    display_name: role,
    unix_username: role,
    account_state: "ACTIVE",
    password_state: "SET",
    resource_onboarding_state: "NOT_ENROLLED",
    roles: [{ name: role, description: role }],
  };
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function installApi(page: Page, state: RenewalState): Promise<void> {
  await page.context().addCookies([
    {
      name: "h100_csrf",
      value: "renewal-approval-fixture-csrf",
      domain: "127.0.0.1",
      path: "/",
    },
  ]);
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/u, "");

    if (path === "/auth/me") {
      await json(route, {
        user: user(state.role),
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
        recent_auth_valid: state.recentAuthValid,
        recent_auth_valid_until: null,
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
    if (path === "/admin/lease-renewals" && request.method() === "GET") {
      if (state.role === "operator") {
        await json(
          route,
          {
            detail: { code: "PERMISSION_DENIED", message: "当前账号无权访问" },
          },
          403,
        );
        return;
      }
      await json(route, {
        status: "OK",
        requests: state.requests,
        count: state.requests.length,
        pending_count: state.requests.filter((row) => row.actionable).length,
      });
      return;
    }
    if (path === "/auth/reauthenticate" && request.method() === "POST") {
      expect(request.headers()["x-csrf-token"]).toBe(
        "renewal-approval-fixture-csrf",
      );
      state.reauthenticateCalls += 1;
      state.recentAuthValid = true;
      await json(route, { reauthenticated: true });
      return;
    }
    if (
      path === `/admin/lease-renewals/${REQUEST_ID}/decision` &&
      request.method() === "POST"
    ) {
      expect(request.headers()["x-csrf-token"]).toBe(
        "renewal-approval-fixture-csrf",
      );
      if (!["platform_owner", "platform_admin"].includes(state.role)) {
        await json(
          route,
          {
            detail: { code: "PERMISSION_DENIED", message: "当前账号无权审批" },
          },
          403,
        );
        return;
      }
      const body = request.postDataJSON() as Record<string, unknown>;
      state.decisions.push(body);
      state.requests = state.requests.map((row) =>
        row.id === REQUEST_ID
          ? {
              ...row,
              state: "APPROVED",
              actionable: false,
              decided_at: "2026-09-06T04:00:00Z",
              decision_comment:
                typeof body.comment === "string" ? body.comment : null,
              resulting_lease_id: "40000000-0000-4000-8000-000000000001",
            }
          : row,
      );
      await json(route, {
        status: "APPROVED",
        renewal_request_id: REQUEST_ID,
        resulting_lease_id: "40000000-0000-4000-8000-000000000001",
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

for (const reviewerRole of ["platform_owner", "platform_admin"] as const) {
  test(`${reviewerRole} sees and approves a pre-expiry renewal after reauthentication`, async ({
    page,
  }) => {
    const state: RenewalState = {
      role: reviewerRole,
      recentAuthValid: false,
      requests: [renewal()],
      reauthenticateCalls: 0,
      decisions: [],
    };
    await installApi(page, state);
    await page.goto("/");

    await page.getByRole("link", { name: "续期审批", exact: true }).click();
    await expect(page).toHaveURL(/\/lease-renewals$/u);
    await expect(page.getByRole("heading", { name: "续期审批" })).toBeVisible();
    await expect(page.getByText("luchenxi · 陆晨曦")).toBeVisible();
    await expect(page.getByText("申请 96 小时")).toBeVisible();

    const approve = page.getByRole("button", { name: "批准续期" });
    await expect(approve).toBeDisabled();
    await page.getByLabel("管理员密码").fill("fixture password only");
    await page.getByLabel("审批备注（可选）").fill("需求已核验");
    await expect(approve).toBeEnabled();
    await approve.click();

    await expect(page.getByRole("cell", { name: "APPROVED" })).toBeVisible();
    expect(state.reauthenticateCalls).toBe(1);
    expect(state.decisions).toEqual([
      { decision: "APPROVE", comment: "需求已核验" },
    ]);
  });
}

test("auditor can inspect renewal requests but cannot review them", async ({
  page,
}) => {
  const state: RenewalState = {
    role: "auditor",
    recentAuthValid: false,
    requests: [renewal()],
    reauthenticateCalls: 0,
    decisions: [],
  };
  await installApi(page, state);
  await page.goto("/lease-renewals");

  await expect(page.getByText("luchenxi · 陆晨曦")).toBeVisible();
  await expect(
    page.getByText("当前角色仅可查看，不能处理续期申请。"),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "批准续期" })).toHaveCount(0);
  await expect(page.getByLabel("管理员密码")).toHaveCount(0);
});

test("operator has neither renewal navigation nor API-backed page access", async ({
  page,
}) => {
  const state: RenewalState = {
    role: "operator",
    recentAuthValid: false,
    requests: [renewal()],
    reauthenticateCalls: 0,
    decisions: [],
  };
  await installApi(page, state);
  await page.goto("/");

  await expect(page.getByRole("link", { name: "续期审批" })).toHaveCount(0);
  await page.goto("/lease-renewals");
  await expect(
    page.getByText("续期审批列表不可用，未执行任何操作。"),
  ).toBeVisible();
  await expect(page.getByText("luchenxi · 陆晨曦")).toHaveCount(0);
});

test("expired renewal is shown as non-actionable history", async ({ page }) => {
  const state: RenewalState = {
    role: "platform_admin",
    recentAuthValid: true,
    requests: [
      renewal({
        username: "huangyixing",
        display_name: "黄义星",
        lease_state: "RECYCLE_BIN",
        lease_expires_at: "2026-09-01T02:00:00Z",
        actionable: false,
        closed_reason: "LEASE_EXPIRED_RESTORE_REQUIRED",
      }),
    ],
    reauthenticateCalls: 0,
    decisions: [],
  };
  await installApi(page, state);
  await page.goto("/lease-renewals");

  await expect(page.getByRole("cell", { name: "huangyixing" })).toBeVisible();
  await expect(page.getByText("已失效", { exact: true })).toBeVisible();
  await expect(page.getByText("原 Lease 已到期，请使用恢复流程")).toBeVisible();
  await expect(page.getByRole("button", { name: "批准续期" })).toHaveCount(0);
});
