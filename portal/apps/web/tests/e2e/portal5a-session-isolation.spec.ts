import {
  expect,
  test,
  type BrowserContext,
  type Page,
  type Request,
  type Route,
} from "@playwright/test";

const e2ePort = process.env.PORTAL_E2E_PORT ?? "18080";
const ORIGIN = `http://127.0.0.1:${e2ePort}`;

type FixtureAccount = {
  username: string;
  password: string;
  displayName: string;
  userId: string;
  managedId: string;
  containerId: string;
  leaseId: string;
  jobId: string;
  keyId: string;
};

const accountA: FixtureAccount = {
  username: "fixture-user-a",
  password: "fixture password a only",
  displayName: "Fixture User A",
  userId: "60000000-0000-4000-8000-000000000001",
  managedId: "61000000-0000-4000-8000-000000000001",
  containerId: "62000000-0000-4000-8000-000000000001",
  leaseId: "63000000-0000-4000-8000-000000000001",
  jobId: "64000000-0000-4000-8000-000000000001",
  keyId: "65000000-0000-4000-8000-000000000001",
};

const accountB: FixtureAccount = {
  username: "fixture-user-b",
  password: "fixture password b only",
  displayName: "Fixture User B",
  userId: "60000000-0000-4000-8000-000000000002",
  managedId: "61000000-0000-4000-8000-000000000002",
  containerId: "62000000-0000-4000-8000-000000000002",
  leaseId: "63000000-0000-4000-8000-000000000002",
  jobId: "64000000-0000-4000-8000-000000000002",
  keyId: "65000000-0000-4000-8000-000000000002",
};

const accounts = new Map(
  [accountA, accountB].map((account) => [account.username, account]),
);

function portalUser(account: FixtureAccount) {
  return {
    id: account.userId,
    login_name: account.username,
    normalized_login: account.username,
    display_name: account.displayName,
    unix_username: account.username,
    account_state: "ACTIVE",
    password_state: "SET",
    resource_onboarding_state: "ACTIVE",
    created_at: "2026-08-17T00:00:00Z",
    activated_at: "2026-08-17T00:05:00Z",
    last_login_at: "2026-08-17T00:10:00Z",
    roles: [{ name: "user", description: "普通用户" }],
  };
}

function sshEnrollment(account: FixtureAccount) {
  return {
    required: false,
    managed_user_id: account.managedId,
    compute_identity: account.username,
    compute_state: "ACTIVE",
    validated_key_count: 1,
    ssh_key_state: "SSH_READY",
    setup_path: null,
  };
}

function lease(account: FixtureAccount) {
  return {
    id: account.leaseId,
    state: "ACTIVE",
    active: true,
    starts_at: "2026-08-17T12:21:08Z",
    expires_at: "2026-08-21T12:21:08Z",
    remaining_seconds: 300_000,
    renewal_available: false,
    renewal_available_from: "2026-08-20T12:21:08Z",
    gpu_count: 1,
    max_duration_seconds: 345_600,
    renewal_window_seconds: 86_400,
    auto_renew: false,
    restore_required: false,
    pending_renewal_id: null,
  };
}

function container(account: FixtureAccount) {
  return {
    id: account.containerId,
    name: `gpu-dev-${account.username}`,
    state: "RUNNING",
    connection_state: "READY",
    gpu: "NONE",
    cpus: 8,
    memory_gb: 32,
    pids_limit: 4096,
  };
}

function job(account: FixtureAccount) {
  return {
    id: account.jobId,
    slurm_job_id: account === accountA ? 101 : 102,
    name: `${account.username}-private-job`,
    state: "COMPLETED",
    cpus: 2,
    memory_mb: 4096,
    gpu_count: 1,
    time_limit_seconds: 600,
    script_path: `workspace/.portal/job-scripts/${account.jobId}.sh`,
    workdir: "workspace",
    stdout_path: `workspace/.portal/jobs/${account.jobId}.out`,
    stderr_path: `workspace/.portal/jobs/${account.jobId}.err`,
    lease_deadline_at: "2026-08-21T12:21:08Z",
    created_at: "2026-08-17T12:30:00Z",
    submitted_at: "2026-08-17T12:30:01Z",
    finished_at: "2026-08-17T12:31:00Z",
    exit_code: "0:0",
  };
}

function cookie(request: Request, name: string): string | undefined {
  const header = request.headers()["cookie"] ?? "";
  for (const item of header.split(";")) {
    const [key, ...value] = item.trim().split("=");
    if (key === name) return decodeURIComponent(value.join("="));
  }
  return undefined;
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

class PortalSessionFixture {
  readonly sessions = new Map<string, FixtureAccount>();
  private sequence = 0;

  async install(page: Page, context: BrowserContext): Promise<void> {
    await page.route("**/api/v1/**", (route) => this.handle(route, context));
  }

  private authenticated(request: Request): FixtureAccount | undefined {
    const token = cookie(request, "h100_session");
    return token ? this.sessions.get(token) : undefined;
  }

  private async handle(route: Route, context: BrowserContext): Promise<void> {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/u, "");

    if (path === "/auth/csrf") {
      const csrf = `fixture-csrf-${++this.sequence}`;
      await context.addCookies([
        {
          name: "h100_csrf",
          value: csrf,
          domain: "127.0.0.1",
          path: "/",
          sameSite: "Strict",
        },
      ]);
      await json(route, { csrf_token: csrf });
      return;
    }

    if (path === "/auth/login") {
      const body = request.postDataJSON() as {
        username?: string;
        password?: string;
      };
      const csrf = cookie(request, "h100_csrf");
      if (!csrf || request.headers()["x-csrf-token"] !== csrf) {
        await json(
          route,
          { detail: { code: "CSRF_REJECTED", message: "CSRF 校验失败" } },
          403,
        );
        return;
      }
      const account = accounts.get(body.username ?? "");
      if (!account || body.password !== account.password) {
        await json(
          route,
          {
            detail: {
              code: "AUTHENTICATION_FAILED",
              message: "用户名或密码不正确",
            },
          },
          401,
        );
        return;
      }
      const oldToken = cookie(request, "h100_session");
      if (oldToken) this.sessions.delete(oldToken);
      const token = `fixture-session-${++this.sequence}-${account.username}`;
      const sessionCsrf = `fixture-session-csrf-${this.sequence}`;
      this.sessions.set(token, account);
      await context.addCookies([
        {
          name: "h100_session",
          value: token,
          domain: "127.0.0.1",
          path: "/",
          httpOnly: true,
          sameSite: "Strict",
        },
        {
          name: "h100_csrf",
          value: sessionCsrf,
          domain: "127.0.0.1",
          path: "/",
          sameSite: "Strict",
        },
      ]);
      await json(route, {
        user: portalUser(account),
        ssh_enrollment: sshEnrollment(account),
      });
      return;
    }

    const account = this.authenticated(request);
    if (!account) {
      await json(
        route,
        { detail: { code: "AUTH_REQUIRED", message: "请先登录" } },
        401,
      );
      return;
    }

    if (path === "/auth/logout") {
      const token = cookie(request, "h100_session");
      if (token) this.sessions.delete(token);
      await context.clearCookies();
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (path === "/auth/me") {
      await json(route, {
        user: portalUser(account),
        role: "user",
        ssh_enrollment: sshEnrollment(account),
        recent_auth_valid: false,
        recent_auth_valid_until: null,
      });
      return;
    }
    if (path === "/audit/page-access") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (path === "/self/environment") {
      await json(route, {
        status: "OK",
        environment: {
          state: "ACTIVE",
          gpu_max: 1,
          host_access: "DISABLED",
          job_submission: "AVAILABLE",
          lease: lease(account),
          container: container(account),
          storage: { quota_bytes: 300 * 1024 ** 3, state: "ACTIVE" },
        },
      });
      return;
    }
    if (path === "/self/storage") {
      await json(route, {
        status: "OK",
        storage: {
          root: `/srv/gpu-platform/users/${account.username}`,
          quota_bytes: 300 * 1024 ** 3,
          used_bytes: 1024,
          available_bytes: 300 * 1024 ** 3 - 1024,
          state: "ACTIVE",
          private: true,
        },
      });
      return;
    }
    if (path === "/self/lease") {
      await json(route, { status: "OK", lease: lease(account) });
      return;
    }
    if (path === "/self/container") {
      await json(route, { status: "OK", container: container(account) });
      return;
    }
    if (path === "/self/jobs") {
      await json(route, { status: "OK", jobs: [job(account)], count: 1 });
      return;
    }
    const logMatch = path.match(/^\/self\/jobs\/([^/]+)\/logs$/u);
    if (logMatch) {
      if (logMatch[1] !== account.jobId) {
        await json(
          route,
          { detail: { code: "JOB_NOT_FOUND", message: "作业不存在" } },
          404,
        );
        return;
      }
      await json(route, {
        status: "OK",
        stdout: `${account.username}-private-stdout`,
        stderr: "",
      });
      return;
    }
    const keyMatch = path.match(/^\/users\/([^/]+)\/ssh-keys$/u);
    if (keyMatch) {
      if (keyMatch[1] !== account.userId) {
        await json(
          route,
          {
            detail: {
              code: "FORBIDDEN",
              message: "当前账号无权管理该 SSH Key",
            },
          },
          403,
        );
        return;
      }
      await json(route, {
        status: "OK",
        keys: [
          {
            id: account.keyId,
            managed_user_id: account.managedId,
            key_type: "ssh-ed25519",
            fingerprint_sha256: `SHA256:${account.username}`,
            comment: `${account.username} fixture`,
            scope: "CONTAINER",
            state: "INSTALLED",
            generation_method: "IMPORTED",
            created_at: "2026-08-17T00:00:00Z",
            created_by: account.userId,
            validated_at: "2026-08-17T00:00:00Z",
            installed_at: "2026-08-17T00:00:00Z",
            revoked_at: null,
          },
        ],
        count: 1,
        maximum_active_keys: 5,
        enrollment: sshEnrollment(account),
      });
      return;
    }
    await json(
      route,
      { detail: { code: "NOT_FOUND", message: `fixture missing: ${path}` } },
      404,
    );
  }
}

async function loginAs(page: Page, account: FixtureAccount): Promise<void> {
  await page.goto("/login");
  await expect(page.getByRole("button", { name: "登录" })).toBeEnabled();
  await page.getByLabel("登录名").fill(account.username);
  await page.getByLabel("网页密码").fill(account.password);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.locator(".user-chip")).toContainText(account.displayName);
}

type ApiResult = { status: number; body: unknown };

async function api(page: Page, path: string): Promise<ApiResult> {
  return page.evaluate(async (requestedPath) => {
    const response = await fetch(`/api/v1${requestedPath}`);
    const body = await response.json().catch(() => null);
    return { status: response.status, body };
  }, path);
}

async function expectOwnerResources(
  page: Page,
  own: FixtureAccount,
  other: FixtureAccount,
): Promise<void> {
  const [me, jobs, currentLease, currentContainer, keys, otherKeys, otherLogs] =
    await Promise.all([
      api(page, "/auth/me"),
      api(page, "/self/jobs"),
      api(page, "/self/lease"),
      api(page, "/self/container"),
      api(page, `/users/${own.userId}/ssh-keys`),
      api(page, `/users/${other.userId}/ssh-keys`),
      api(page, `/self/jobs/${other.jobId}/logs`),
    ]);

  expect(me.status).toBe(200);
  expect((me.body as { user: { id: string } }).user.id).toBe(own.userId);
  expect(
    (jobs.body as { jobs: Array<{ id: string; name: string }> }).jobs,
  ).toEqual([
    expect.objectContaining({
      id: own.jobId,
      name: `${own.username}-private-job`,
    }),
  ]);
  expect((currentLease.body as { lease: { id: string } }).lease.id).toBe(
    own.leaseId,
  );
  expect(
    (currentContainer.body as { container: { id: string } }).container.id,
  ).toBe(own.containerId);
  expect((keys.body as { keys: Array<{ id: string }> }).keys).toEqual([
    expect.objectContaining({ id: own.keyId }),
  ]);
  expect(otherKeys.status).toBe(403);
  expect(otherLogs.status).toBe(404);
  expect(
    JSON.stringify({ me, jobs, currentLease, currentContainer, keys }),
  ).not.toContain(other.username);
}

async function expectOwnerJobUi(
  page: Page,
  own: FixtureAccount,
  other: FixtureAccount,
): Promise<void> {
  await page.goto("/jobs");
  await expect(page.getByRole("heading", { name: "作业" })).toBeVisible();
  const ownRow = page
    .getByRole("row")
    .filter({ hasText: `${own.username}-private-job` });
  await expect(ownRow).toHaveCount(1);
  await expect(page.getByText(`${other.username}-private-job`)).toHaveCount(0);

  await ownRow.getByRole("button", { name: "日志" }).click();
  await expect(
    page.getByText(`${own.username}-private-job · 日志`, { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".job-log").first()).toContainText(
    `${own.username}-private-stdout`,
  );
  await expect(page.locator(".job-log").first()).not.toContainText(
    other.username,
  );
}

test("two browser.newContext cookie jars stay isolated across refresh, logout, and re-login", async ({
  browser,
}) => {
  const fixture = new PortalSessionFixture();
  const contextA = await browser.newContext({ baseURL: ORIGIN });
  const contextB = await browser.newContext({ baseURL: ORIGIN });
  try {
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();
    await fixture.install(pageA, contextA);
    await fixture.install(pageB, contextB);

    await Promise.all([loginAs(pageA, accountA), loginAs(pageB, accountB)]);
    for (let refresh = 0; refresh < 3; refresh += 1) {
      await Promise.all([pageA.reload(), pageB.reload()]);
      await expect(pageA.locator(".user-chip")).toContainText(
        accountA.displayName,
      );
      await expect(pageB.locator(".user-chip")).toContainText(
        accountB.displayName,
      );
      await Promise.all([
        expectOwnerResources(pageA, accountA, accountB),
        expectOwnerResources(pageB, accountB, accountA),
      ]);
    }

    const cookieB = (await contextB.cookies()).find(
      (item) => item.name === "h100_session",
    )?.value;
    await pageA.getByRole("button", { name: "退出" }).click();
    await expect(pageA).toHaveURL(/\/login$/u);
    expect((await api(pageA, "/auth/me")).status).toBe(401);

    await pageB.reload();
    await expect(pageB.locator(".user-chip")).toContainText(
      accountB.displayName,
    );
    expect(
      (await contextB.cookies()).find((item) => item.name === "h100_session")
        ?.value,
    ).toBe(cookieB);
    await expectOwnerResources(pageB, accountB, accountA);

    await loginAs(pageA, accountA);
    await expectOwnerResources(pageA, accountA, accountB);
    await expectOwnerResources(pageB, accountB, accountA);
  } finally {
    await Promise.all([contextA.close(), contextB.close()]);
  }
});

test("two pages in one BrowserContext intentionally share the latest login cookie", async ({
  browser,
}) => {
  const fixture = new PortalSessionFixture();
  const context = await browser.newContext({ baseURL: ORIGIN });
  try {
    const page1 = await context.newPage();
    const page2 = await context.newPage();
    await fixture.install(page1, context);
    await fixture.install(page2, context);

    await loginAs(page1, accountA);
    const firstToken = (await context.cookies()).find(
      (item) => item.name === "h100_session",
    )?.value;
    expect(firstToken).toBeTruthy();

    await page2.goto("/");
    await expect(page2.locator(".user-chip")).toContainText(
      accountA.displayName,
    );
    await expectOwnerResources(page2, accountA, accountB);

    await loginAs(page2, accountB);
    const secondToken = (await context.cookies()).find(
      (item) => item.name === "h100_session",
    )?.value;
    expect(secondToken).toBeTruthy();
    expect(secondToken).not.toBe(firstToken);
    expect(fixture.sessions.has(firstToken ?? "")).toBe(false);

    await page1.reload();
    await expect(page1.locator(".user-chip")).toContainText(
      accountB.displayName,
    );
    await Promise.all([
      expectOwnerResources(page1, accountB, accountA),
      expectOwnerResources(page2, accountB, accountA),
    ]);
  } finally {
    await context.close();
  }
});

test("two browser contexts render only their owner-bound Job UI and logs", async ({
  browser,
}) => {
  const fixture = new PortalSessionFixture();
  const contextA = await browser.newContext({ baseURL: ORIGIN });
  const contextB = await browser.newContext({ baseURL: ORIGIN });
  try {
    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();
    await fixture.install(pageA, contextA);
    await fixture.install(pageB, contextB);

    await Promise.all([loginAs(pageA, accountA), loginAs(pageB, accountB)]);
    await Promise.all([
      expectOwnerJobUi(pageA, accountA, accountB),
      expectOwnerJobUi(pageB, accountB, accountA),
    ]);

    expect((await api(pageA, `/self/jobs/${accountB.jobId}/logs`)).status).toBe(
      404,
    );
    expect((await api(pageB, `/self/jobs/${accountA.jobId}/logs`)).status).toBe(
      404,
    );
  } finally {
    await Promise.all([contextA.close(), contextB.close()]);
  }
});
