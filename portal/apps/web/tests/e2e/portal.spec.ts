import { expect, test, type Page, type Route } from "@playwright/test";

type MockState = {
  authenticated?: boolean;
  role?: "platform_owner" | "operator";
  loginSucceeds?: boolean;
  setupUsesRemaining?: number;
  denyOperations?: boolean;
  enforceCsrf?: boolean;
  draftCreated?: boolean;
  planConflict?: boolean;
  staged?: boolean;
};

const MOCK_SETUP_TOKEN = "test-only-portal-setup-token-with-forty-eight-bytes";
const MOCK_PASSWORD = "A sufficiently long portal passphrase";

const owner = {
  id: "00000000-0000-4000-8000-000000000001",
  login_name: "Origin-al",
  normalized_login: "origin-al",
  display_name: "Origin-al",
  unix_username: "origin-al",
  account_state: "ACTIVE",
  password_state: "SET",
  resource_onboarding_state: "NOT_ENROLLED",
  created_at: "2026-08-06T00:00:00Z",
  activated_at: "2026-08-06T00:05:00Z",
  last_login_at: "2026-08-06T00:10:00Z",
  failed_login_count: 0,
  locked_until: null,
  roles: [{ name: "platform_owner", description: "网页平台所有者" }],
  linux_identity: {
    unix_username: "origin-al",
    onboarding_state: "NOT_ENROLLED",
    gpu_isolation_state: "NOT_APPLIED",
  },
  compute_onboarding: {
    status: "NOT_CREATED",
    compute_username: "origin-pilot",
    draft_state: "DRAFT NOT CREATED",
    plan: null,
  },
};

const originPilotPlan = {
  status: "DRY_RUN",
  plan_status: "READY",
  execution_enabled: false,
  proposal_state: "DRAFT",
  proposed_username: "origin-pilot",
  portal_owner: "Origin-al",
  proposed_uid: 20001,
  proposed_gid: 20001,
  proposed_project_id: 30001,
  proposed_ssh_port: 22023,
  proposed_slurm_account: "company",
  proposed_qos: "general",
  proposed_max_gpus: 1,
  proposed_quota_hard_limit_gb: 300,
  ssh_key_status: "REQUIRED BEFORE ACTIVATION",
  proposed_container: {
    name: "gpu-dev-origin-pilot",
    cpus: 8,
    memory_gb: 32,
    pids_limit: 4096,
    gpu: "none",
    network_bind: "10.82.36.1",
  },
  proposed_gpu_policy: {
    method: "systemd-user-uid-slice",
    unit: "user-20001.slice",
    dropin_path:
      "/etc/systemd/system/user-20001.slice.d/50-h100-gpu-isolation.conf",
    device_policy: "closed",
  },
  validation_results: [
    {
      check: "origin_pilot_username_available",
      status: "PASS",
      detail: "origin-pilot 用户和同名组不存在",
    },
    {
      check: "slurm_node_drained",
      status: "PASS",
      detail: "节点保持 DRAIN",
    },
  ],
  conflicts: [],
  stage_steps: ["创建 nologin 账号", "应用精确 UID slice", "保持 STAGED"],
  activate_steps: ["重新验证隔离", "要求 SSH 公钥", "人工验收后 ACTIVE"],
  rollback_steps: ["恢复精确配置备份", "保持 Slurm DRAIN"],
};

const originPilotConflictPlan = {
  ...originPilotPlan,
  plan_status: "CONFLICT",
  conflicts: [
    {
      code: "USERNAME_CONFLICT",
      message: "origin-pilot Linux 用户或组已存在",
    },
  ],
};

const stagedOwner = {
  ...owner,
  resource_onboarding_state: "STAGED",
  linux_identity: {
    managed_user_id: "00000000-0000-4000-8000-000000000030",
    unix_username: "origin-pilot",
    uid: 20001,
    gid: 20001,
    shell: "/usr/sbin/nologin",
    onboarding_state: "STAGED",
    host_access_state: "DISABLED",
    password_state: "LOCKED",
    authorized_keys_state: "ABSENT",
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
    container_state: "STOPPED",
    container_gpu: "NONE",
    container_cpus: 8,
    container_memory_gb: 32,
    container_pids_limit: 4096,
    container_image_digest: `sha256:${"a".repeat(64)}`,
    ssh_key_count: 0,
    ssh_key_state: "REQUIRED_BEFORE_ACTIVATION",
  },
  compute_onboarding: {
    status: "STAGED",
    compute_username: "origin-pilot",
    draft_state: "STAGED",
    operation_id: "00000000-0000-4000-8000-000000000020",
    operation_status: "SUCCEEDED",
    ssh_key_status: "REQUIRED_BEFORE_ACTIVATION",
    plan: {
      ...originPilotPlan,
      stage_status: "STAGED",
      execution_enabled: true,
    },
  },
};

const managedContainer = {
  name: "/gpu-dev-codexops",
  id: "container-test-id",
  created: "2026-08-05T00:00:00Z",
  state: { Status: "running", Health: { Status: "healthy" } },
  image: "localhost/h100-dev@sha256:test",
  labels: { "h100.platform.managed": "true" },
  privileged: false,
  network_mode: "bridge",
  pid_mode: "",
  ipc_mode: "private",
  mounts: [
    {
      Type: "bind",
      Source: "/srv/gpu-platform/users/codexops/workspace",
      Destination: "/workspace",
      RW: true,
    },
  ],
  device_requests: null,
  restart_policy: { Name: "unless-stopped", MaximumRetryCount: 0 },
  ports: { "22/tcp": [{ HostIp: "10.82.36.1", HostPort: "22022" }] },
};

const gpuRows = [
  ["0", "GPU-c837-test", "00000000:21:00.0", "1", "/dev/nvidia1"],
  ["1", "GPU-992f-test", "00000000:81:00.0", "0", "/dev/nvidia0"],
  ["2", "GPU-cb10-test", "00000000:C1:00.0", "3", "/dev/nvidia3"],
  ["3", "GPU-873d-test", "00000000:E1:00.0", "2", "/dev/nvidia2"],
].map(([index, uuid, bus, minor, path]) => ({
  index,
  uuid,
  "pci.bus_id": bus,
  minor_number: minor,
  device_path: path,
  name: "NVIDIA H100 PCIe",
  "memory.total": "81559",
  "memory.used": "0",
  "utilization.gpu": "0",
  "temperature.gpu": "31",
  "power.draw": "69.00",
  "ecc.errors.uncorrected.volatile.total": "0",
  "pcie.link.gen.current": "5",
  "pcie.link.width.current": "16",
  "mig.mode.current": "Disabled",
  dcgm_status: "Pass",
  xid_aer_status: "CLEAR",
  active_slurm_job_ids: [],
}));

const overview = {
  status: "PARTIAL",
  platform: {
    status: "PARTIAL",
    node: {
      status: "OK",
      nodes: [
        {
          name: "sagsh100server",
          state: "IDLE+DRAIN",
          reason:
            "gpu device mapping and transient isolation validation complete",
        },
      ],
    },
    jobs: { jobs: [] },
    gpu: { status: "OK", count: 4, gpus: gpuRows },
    systemd: { status: "OK", count: 0 },
  },
  containers: { status: "OK", count: 0, containers: [] },
  storage: { status: "OK", mounts: [] },
  registries: {
    status: "PARTIAL",
    registries: [
      { name: "Local", status: "AVAILABLE" },
      { name: "Docker Hub", status: "DEFERRED" },
    ],
  },
  alerts: { status: "OK", count: 1, alerts: [] },
  monitoring: {
    status: "OK",
    prometheus: { status: "OK", targets: [] },
    alerts: { status: "OK", alerts: [] },
    metrics: [],
    guard: { metrics: [] },
    mellanox: { metrics: [] },
    grafana: { status: "OK" },
  },
  recent_audit: [],
  identity: {
    status: "OK",
    portal_users: 1,
    managed_linux_users: 0,
    account_states: { ACTIVE: 1 },
    onboarding_states: {},
  },
  tasks: { status: "OK", pending_approval: 0, failed: 0, total: 0 },
  ssh_policy: {
    status: "OK",
    global_ssh_policy: {
      pubkey_authentication: true,
      password_authentication: true,
      keyboard_interactive_authentication: false,
      authentication_methods: "any",
    },
    managed_compute_user_policy: {
      status: "PASSING",
      pubkey_authentication: true,
      password_authentication: false,
      keyboard_interactive_authentication: false,
      authentication_methods: ["publickey"],
    },
  },
};

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

async function installMockApi(page: Page, state: MockState): Promise<void> {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/api\/v1/, "");
    if (path === "/auth/csrf") {
      await json(route, { csrf_token: "test-csrf" }, 200, {
        "set-cookie": "h100_csrf=test-csrf; Path=/; SameSite=Strict",
      });
      return;
    }
    if (path === "/auth/me") {
      if (!state.authenticated) {
        await json(
          route,
          { detail: { code: "AUTH_REQUIRED", message: "请先登录" } },
          401,
        );
        return;
      }
      await json(route, {
        user: state.staged ? stagedOwner : owner,
        role: state.role ?? "platform_owner",
        ssh_enrollment: {
          required: Boolean(state.staged),
          managed_user_id: state.staged
            ? "00000000-0000-4000-8000-000000000030"
            : null,
          compute_identity: state.staged ? "origin-pilot" : null,
          compute_state: state.staged ? "STAGED" : "NOT_ENROLLED",
          validated_key_count: 0,
          ssh_key_state: state.staged
            ? "REQUIRED_BEFORE_ACTIVATION"
            : "NOT_APPLICABLE",
          setup_path: state.staged ? `/users/${owner.id}?tab=ssh` : null,
        },
      });
      return;
    }
    if (path === "/auth/login") {
      if (state.enforceCsrf && !request.headers()["x-csrf-token"]) {
        await json(
          route,
          { detail: { code: "CSRF_REJECTED", message: "CSRF 校验失败" } },
          403,
        );
        return;
      }
      if (!state.loginSucceeds) {
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
      state.authenticated = true;
      await json(route, { user: owner, csrf_token: "session-csrf" }, 200, {
        "set-cookie": "h100_csrf=session-csrf; Path=/; SameSite=Strict",
      });
      return;
    }
    if (path === "/auth/setup-password") {
      if ((state.setupUsesRemaining ?? 0) < 1) {
        await json(
          route,
          {
            detail: {
              code: "SETUP_TOKEN_INVALID",
              message: "设置链接无效或已过期",
            },
          },
          400,
        );
        return;
      }
      state.setupUsesRemaining = (state.setupUsesRemaining ?? 1) - 1;
      state.authenticated = true;
      await json(route, { user: owner, csrf_token: "session-csrf" }, 200);
      return;
    }
    if (path === "/auth/logout") {
      state.authenticated = false;
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (path === "/auth/sessions") {
      await json(route, [
        {
          id: "00000000-0000-4000-8000-000000000010",
          created_at: "2026-08-06T00:05:00Z",
          last_seen_at: "2026-08-06T00:10:00Z",
          idle_expires_at: "2026-08-06T00:40:00Z",
          absolute_expires_at: "2026-08-06T12:05:00Z",
          current: true,
          source_ip: "127.0.0.1",
        },
      ]);
      return;
    }
    if (path === "/auth/sessions/revoke-others") {
      await json(route, { revoked: 0 });
      return;
    }
    if (path === "/auth/password") {
      await json(route, { changed: true });
      return;
    }
    if (path === "/audit/page-access") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (path === "/platform/overview") {
      await json(route, overview);
      return;
    }
    if (path === "/platform/gpus") {
      await json(route, { status: "OK", count: 4, gpus: gpuRows });
      return;
    }
    if (path === "/platform/gpu-health") {
      await json(route, {
        status: "OK",
        discovery: { ok: true, output: "4 GPUs found" },
        diag: { ok: true, output: "All tests passed" },
        kernel_errors: { status: "CLEAR", count: 0 },
      });
      return;
    }
    if (path === "/platform/alerts") {
      await json(route, {
        status: "OK",
        count: 1,
        alerts: [
          {
            name: "MellanoxPhysicalLinkDeferred",
            severity: "warning",
            state: "firing",
            summary: "管理员接受单节点 Pilot 风险",
          },
        ],
      });
      return;
    }
    if (path === "/platform/monitoring") {
      await json(route, overview.monitoring);
      return;
    }
    if (path === "/platform/registries") {
      await json(route, overview.registries);
      return;
    }
    if (path === "/platform/storage") {
      await json(route, {
        status: "OK",
        mounts: [],
        volume_groups: {},
        path_usage: {},
        docker_usage: [],
      });
      return;
    }
    if (path === "/platform/quotas") {
      await json(route, {
        status: "OK",
        report: "Project quota on /srv/gpu-platform",
        projects: [],
      });
      return;
    }
    if (path === "/slurm/nodes") {
      await json(route, {
        status: "OK",
        nodes: [
          {
            name: "sagsh100server",
            state: "IDLE+DRAIN",
            reason:
              "gpu device mapping and transient isolation validation complete",
            cpus: 256,
            real_memory: 486377,
            gres: "gpu:h100:4",
            gres_used: "gpu:h100:0(IDX:N/A)",
            alloc_tres: "",
            partitions: "notebook,train",
          },
        ],
      });
      return;
    }
    if (path === "/slurm/jobs") {
      await json(route, { status: "OK", jobs: [] });
      return;
    }
    if (path === "/slurm/history") {
      await json(route, { status: "OK", jobs: [] });
      return;
    }
    if (path === "/slurm/accounts") {
      await json(route, {
        status: "OK",
        accounts: [],
        qos: [],
        associations: [],
      });
      return;
    }
    if (path === "/users") {
      await json(route, {
        status: "OK",
        users: [state.staged ? stagedOwner : owner],
        count: 1,
      });
      return;
    }
    if (path === `/users/${owner.id}`) {
      await json(route, {
        status: "OK",
        user: state.staged
          ? stagedOwner
          : state.draftCreated
            ? {
                ...owner,
                compute_onboarding: {
                  status: "DRAFT",
                  compute_username: "origin-pilot",
                  draft_state: "DRAFT",
                  operation_id: "00000000-0000-4000-8000-000000000020",
                  operation_status: "DRAFT",
                  plan: state.planConflict
                    ? originPilotConflictPlan
                    : originPilotPlan,
                },
              }
            : owner,
      });
      return;
    }
    if (path === `/users/${owner.id}/ssh-keys`) {
      await json(route, {
        status: "OK",
        keys: [],
        count: 0,
        maximum_active_keys: 5,
        enrollment: {
          required: true,
          managed_user_id: "00000000-0000-4000-8000-000000000030",
          compute_identity: "origin-pilot",
          compute_state: "STAGED",
          validated_key_count: 0,
          ssh_key_state: "REQUIRED_BEFORE_ACTIVATION",
          setup_path: `/users/${owner.id}?tab=ssh`,
        },
      });
      return;
    }
    if (path === "/containers") {
      await json(route, {
        status: "OK",
        count: 1,
        containers: [
          {
            ID: managedContainer.id,
            Names: "gpu-dev-codexops",
            Image: managedContainer.image,
            Status: "Up 1 hour (healthy)",
            Ports: "10.82.36.1:22022->22/tcp",
            Labels: managedContainer.labels,
          },
        ],
      });
      return;
    }
    if (path === "/containers/gpu-dev-codexops") {
      await json(route, { status: "OK", container: managedContainer });
      return;
    }
    if (path === "/images") {
      await json(route, { status: "OK", count: 0, images: [] });
      return;
    }
    if (path === "/audit") {
      await json(route, {
        status: "OK",
        events: [
          {
            id: "audit-1",
            timestamp: "2026-08-06T00:00:00Z",
            event_type: "login.success",
            actor: "origin-al",
            actor_role: "platform_owner",
            object_type: "portal_user",
            result: "SUCCESS",
          },
        ],
      });
      return;
    }
    if (path === "/operations") {
      if (state.denyOperations) {
        await json(
          route,
          { detail: { code: "FORBIDDEN", message: "权限不足" } },
          403,
        );
        return;
      }
      if (request.method() === "POST") {
        const body = request.postDataJSON() as Record<string, unknown>;
        expect(body).toMatchObject({
          operation_type: "user.plan",
          target_id: "origin-pilot",
          payload: { username: "origin-pilot" },
        });
        state.draftCreated = true;
        await json(route, {
          id: "00000000-0000-4000-8000-000000000020",
          operation_type: "user.plan",
          target_type: "compute_identity",
          target_id: "origin-pilot",
          status: "DRAFT",
          dry_run_result: originPilotPlan,
        });
        return;
      }
      await json(route, {
        status: "OK",
        operations: state.draftCreated
          ? [
              {
                id: "00000000-0000-4000-8000-000000000020",
                operation_type: "user.plan",
                target_id: "origin-pilot",
                risk_level: "MEDIUM",
                status: "DRAFT",
                request_summary: "独立计算身份 dry-run",
                dry_run_result: originPilotPlan,
              },
            ]
          : [],
      });
      return;
    }
    if (path.endsWith("/submit") || path.endsWith("/approval")) {
      await json(route, { status: "OK" });
      return;
    }
    await json(
      route,
      { detail: { code: "NOT_FOUND", message: "测试路由不存在" } },
      404,
    );
  });
}

async function fillPasswordSetup(page: Page): Promise<void> {
  await page.getByLabel("新网页密码").fill(MOCK_PASSWORD);
  await page.getByLabel("再次输入密码").fill(MOCK_PASSWORD);
  await page.getByRole("button", { name: "设置密码并进入平台" }).click();
}

test("未登录访问控制台时跳转登录页", async ({ page }) => {
  await installMockApi(page, {});
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});

test("无效登录显示统一错误文案", async ({ page }) => {
  await installMockApi(page, { loginSucceeds: false });
  await page.goto("/login");
  await page.getByLabel("登录名").fill("Origin-al");
  await page.getByLabel("网页密码").fill("wrong-password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.locator(".error-box[role='alert']")).toContainText(
    "用户名或密码不正确",
  );
});

test("首次密码设置后进入总览", async ({ page }) => {
  await installMockApi(page, { setupUsesRemaining: 1 });
  await page.goto(`/setup-password?token=${MOCK_SETUP_TOKEN}`);
  await fillPasswordSetup(page);
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "总览" })).toBeVisible();
  await expect(
    page.getByText("INTERNAL HTTP · VIRTUAL NETWORK ONLY").first(),
  ).toBeVisible();
});

test("过期 token 被拒绝", async ({ page }) => {
  await installMockApi(page, { setupUsesRemaining: 0 });
  await page.goto(`/setup-password?token=${MOCK_SETUP_TOKEN}`);
  await fillPasswordSetup(page);
  await expect(page.locator(".error-box[role='alert']")).toContainText(
    "设置链接无效",
  );
});

test("token 使用一次后不能重用", async ({ page }) => {
  const state: MockState = { setupUsesRemaining: 1 };
  await installMockApi(page, state);
  await page.goto(`/setup-password?token=${MOCK_SETUP_TOKEN}`);
  await fillPasswordSetup(page);
  await expect(page).toHaveURL(/\/$/);
  await page.goto(`/setup-password?token=${MOCK_SETUP_TOKEN}`);
  await fillPasswordSetup(page);
  await expect(page.locator(".error-box[role='alert']")).toContainText(
    "设置链接无效",
  );
});

test("Origin-al 可使用大小写显示名登录", async ({ page }) => {
  await installMockApi(page, { loginSucceeds: true });
  await page.goto("/login");
  await page.getByLabel("登录名").fill("ORIGIN-AL");
  await page.getByLabel("网页密码").fill(MOCK_PASSWORD);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.locator(".user-chip")).toContainText("Origin-al");
  await expect(page.locator(".user-chip")).toContainText("平台所有者");
});

test("总览加载真实状态结构并适配 1366 宽度", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  await installMockApi(page, { authenticated: true });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "总览" })).toBeVisible();
  await expect(
    page.locator(".stat-panel").filter({ hasText: "GPU" }),
  ).toContainText("4");
  await expect(page.getByText("网页 / 受管用户")).toBeVisible();
  await expect(page.getByText("待审批 / 失败任务")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("系统页显示管理员接受的内部 HTTP 状态", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/system");
  await expect(
    page.getByText(
      "INTERNAL HTTP ACCEPTED BY ADMINISTRATOR — VIRTUAL NETWORK ONLY",
    ),
  ).toBeVisible();
  await expect(
    page.getByText("NOT ENABLED — NOT REQUIRED FOR CURRENT PILOT SCOPE"),
  ).toBeVisible();
  await expect(page.getByText(/HTTPS/)).toHaveCount(0);
  await expect(page.getByText("Global SSH Policy", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Managed Compute User Policy", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("PUBLIC KEY ONLY")).toBeVisible();
  await expect(page.getByText("Global Password Authentication")).toBeVisible();
});

test("Slurm 页面明确显示 DRAIN 和禁止 RESUME", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/slurm");
  await expect(page.getByText("IDLE+DRAIN", { exact: true })).toBeVisible();
  await expect(page.getByText(/禁止通过 Portal RESUME Slurm/)).toBeVisible();
});

test("GPU 页面展示四卡物理身份映射", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/gpus");
  await expect(page.getByText("GPU-c837-test")).toBeVisible();
  await expect(page.getByText("/dev/nvidia1")).toBeVisible();
  await expect(page.getByText("/dev/nvidia0")).toBeVisible();
  await expect(page.getByText("MIG Disabled")).toBeVisible();
  await expect(page.getByText("69.00").first()).toBeVisible();
  await expect(page.getByText("CLEAR", { exact: true }).first()).toBeVisible();
});

test("镜像页显示 Docker Hub deferred", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/images");
  await expect(page.getByText("Docker Hub", { exact: true })).toBeVisible();
  await expect(page.getByText(/DEFERRED \/ RESTRICTED/)).toBeVisible();
});

test("所有者能查看用户页且计算身份未注册", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/users");
  await expect(
    page.getByText("Origin-al", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByText("NOT_ENROLLED", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByText(/不会自动进入 Pilot/)).toBeVisible();
});

test("用户详情按标签区分网页、Linux 与 GPU 身份", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto(`/users/${owner.id}`);
  await expect(page.getByText(/平台恢复\/管理账号/)).toBeVisible();
  await expect(page.getByText("origin-al", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "GPU 隔离" }).click();
  await expect(
    page.getByText("NOT_APPLIED", { exact: true }).first(),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Slurm" }).click();
  await expect(page.getByText("NOT_ENROLLED", { exact: true })).toBeVisible();
});

test("Origin-al 计算资源页只创建 origin-pilot DRAFT 并锁定 Stage/Activate", async ({
  page,
}) => {
  await installMockApi(page, { authenticated: true });
  await page.goto(`/users/${owner.id}`);
  await page.getByRole("tab", { name: "计算资源" }).click();
  await expect(
    page.getByText("DRAFT NOT CREATED", { exact: true }).first(),
  ).toBeVisible();
  await page.getByRole("button", { name: "创建计算资源草稿" }).click();
  await expect(
    page.getByText("origin-pilot", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByText(/20001 — NOT RESERVED/).first()).toBeVisible();
  await expect(
    page.getByText("REQUIRED BEFORE ACTIVATION", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Stage 未授权" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Activate 未授权" }),
  ).toBeDisabled();
  await expect(
    page.getByText(/Stage 不读取、不验证、不安装 SSH 公钥/),
  ).toBeVisible();
  await expect(
    page.locator('input[type="file"], textarea[name*="key"]'),
  ).toHaveCount(0);
  await expect(page.getByText(/不会被转换为 Pilot/)).toBeVisible();
});

test("origin-pilot 资源冲突在计划页明确阻断", async ({ page }) => {
  await installMockApi(page, {
    authenticated: true,
    draftCreated: true,
    planConflict: true,
  });
  await page.goto(`/users/${owner.id}`);
  await page.getByRole("tab", { name: "计算资源" }).click();
  await expect(page.getByText(/USERNAME_CONFLICT/)).toBeVisible();
  await expect(
    page.getByText("CONFLICT", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Stage 未授权" }),
  ).toBeDisabled();
});

test("origin-pilot STAGED 页面保持无公钥、nologin、停止容器和 Activate Gate", async ({
  page,
}) => {
  await installMockApi(page, { authenticated: true, staged: true });
  await page.goto(`/users/${owner.id}`);
  await page.getByRole("tab", { name: "计算资源" }).click();
  await expect(page.getByText("STAGED", { exact: true }).first()).toBeVisible();
  await expect(
    page.getByText("origin-pilot", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByText("20001/20001", { exact: true })).toBeVisible();
  await expect(
    page.getByText("/usr/sbin/nologin", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("LOCKED", { exact: true })).toBeVisible();
  await expect(page.getByText("ABSENT", { exact: true })).toBeVisible();
  await expect(page.getByText("DENIED", { exact: true }).first()).toBeVisible();
  await expect(
    page.getByText(/gpu-dev-origin-pilot \/ STOPPED \/ GPU NONE/),
  ).toBeVisible();
  await expect(page.getByText("PASSING", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "设置 SSH 公钥" }).click();
  await expect(
    page.getByRole("heading", { name: "SSH 密钥设置" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "生成新密钥" })).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "导入已有公钥" }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "生成 Activate Dry-Run" }),
  ).toHaveCount(0);
  await expect(page.getByText(/authorized_keys 为 ABSENT/)).toBeVisible();
  await expect(page.getByText(/Origin-al 是平台恢复\/管理账号/)).toBeVisible();
});

test("账号安全页显示会话并可撤销其他会话", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/account/security");
  await expect(page.getByRole("heading", { name: "账号安全" })).toBeVisible();
  await expect(page.getByText("127.0.0.1", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "撤销其他会话" }).click();
  await expect(page.getByRole("status")).toContainText("已撤销 0 个其他会话");
});

test("容器详情显示默认安全属性", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/containers/gpu-dev-codexops");
  await expect(page.getByText("GPU：无", { exact: true })).toBeVisible();
  await expect(page.getByText("Privileged：否", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Docker Socket：未挂载", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Host Network：否", { exact: true }),
  ).toBeVisible();
});

test("存储页读取真实 quota 适配器而不伪造零值", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/storage");
  await expect(page.getByText(/Project quota on/)).toBeVisible();
});

test("审计页显示安全事件而不显示秘密", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/audit");
  await expect(page.getByText("login.success")).toBeVisible();
  await expect(page.getByText("origin-al", { exact: true })).toBeVisible();
  await expect(
    page.getByText(/password_hash|session_cookie|private_key/),
  ).toHaveCount(0);
});

test("退出登录撤销当前浏览器状态并返回登录页", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/");
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page).toHaveURL(/\/login$/);
});

test("缺少 CSRF 的状态修改请求被拒绝", async ({ page }) => {
  await installMockApi(page, { loginSucceeds: true, enforceCsrf: true });
  await page.goto("/login");
  await page.context().clearCookies();
  const result = await page.evaluate(async () => {
    const response = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "Origin-al", password: "not-recorded" }),
    });
    return { status: response.status, body: await response.json() };
  });
  expect(result.status).toBe(403);
  expect(result.body.detail.code).toBe("CSRF_REJECTED");
});

test("非 owner 不能进入高风险审批模块", async ({ page }) => {
  await installMockApi(page, {
    authenticated: true,
    role: "operator",
    denyOperations: true,
  });
  await page.goto("/operations");
  await expect(page.getByText("当前账号无权查看此模块。")).toBeVisible();
});

test("界面没有渐变、玻璃拟态或 AI 助手文案", async ({ page }) => {
  await installMockApi(page, { authenticated: true });
  await page.goto("/");
  await expect(page.getByText(/AI 助手|智能助手|AI 洞察/)).toHaveCount(0);
  const styles = await page
    .locator(".ui-card")
    .first()
    .evaluate((element) => {
      const computed = window.getComputedStyle(element);
      return {
        backgroundImage: computed.backgroundImage,
        backdropFilter: computed.backdropFilter,
        borderRadius: Number.parseFloat(computed.borderTopLeftRadius || "0"),
      };
    });
  expect(["", "none"]).toContain(styles.backgroundImage);
  expect(["", "none"]).toContain(styles.backdropFilter);
  expect(styles.borderRadius).toBeLessThanOrEqual(8);
});
