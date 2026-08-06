import { existsSync, mkdirSync, readFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Page, type Route } from "@playwright/test";

type Snapshot = {
  source: string;
  api: Record<string, unknown>;
  primary_user_id: string;
  primary_container: string;
};

const snapshotPath = process.env.PORTAL2_REAL_SNAPSHOT ?? "";
const snapshot: Snapshot | null =
  snapshotPath && existsSync(snapshotPath)
    ? (JSON.parse(readFileSync(snapshotPath, "utf8")) as Snapshot)
    : null;
const outputDirectory =
  process.env.PORTAL2_SCREENSHOT_DIR ??
  path.resolve("test-results", "portal2-visual-acceptance");

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function installSnapshot(page: Page): Promise<void> {
  if (!snapshot) return;
  await page.route("**/api/v1/**", async (route) => {
    const requestPath = new URL(route.request().url()).pathname.replace(
      /^\/api\/v1/,
      "",
    );
    if (requestPath === "/auth/csrf") {
      await json(route, { csrf_token: "visual-acceptance-csrf" }, 200);
      return;
    }
    if (requestPath === "/audit/page-access") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    const payload = snapshot.api[requestPath];
    if (payload === undefined) {
      await json(
        route,
        { detail: { code: "SNAPSHOT_ROUTE_MISSING", message: requestPath } },
        404,
      );
      return;
    }
    await json(route, payload);
  });
}

async function assertVisualBoundary(page: Page): Promise<void> {
  await expect(page.getByText(/AI 助手|智能助手|AI 洞察/)).toHaveCount(0);
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

const visualPages = [
  { name: "login", url: "/login", heading: "H100 管理平台" },
  { name: "overview", url: "/", heading: "总览" },
  { name: "gpu", url: "/gpus", heading: "GPU" },
  { name: "slurm", url: "/slurm", heading: "Slurm" },
  {
    name: "user-detail",
    url: () => `/users/${snapshot?.primary_user_id ?? "missing"}`,
    heading: "Origin-al",
  },
  {
    name: "container",
    url: () =>
      `/containers/${encodeURIComponent(snapshot?.primary_container ?? "missing")}`,
    heading: snapshot?.primary_container ?? "容器详情",
  },
  { name: "storage", url: "/storage", heading: "存储和配额" },
  { name: "monitoring", url: "/monitoring", heading: "监控和告警" },
  { name: "operations", url: "/operations", heading: "审批任务" },
  { name: "audit", url: "/audit", heading: "审计日志" },
] as const;

for (const viewport of [
  { width: 1366, height: 768, label: "1366x768" },
  { width: 1920, height: 1080, label: "1920x1080" },
]) {
  for (const item of visualPages) {
    test(`Portal-2 real snapshot ${item.name} ${viewport.label}`, async ({
      page,
    }) => {
      test.skip(!snapshot, "PORTAL2_REAL_SNAPSHOT is required");
      mkdirSync(outputDirectory, { recursive: true });
      await page.setViewportSize(viewport);
      await installSnapshot(page);
      const url = typeof item.url === "function" ? item.url() : item.url;
      await page.goto(url);
      await expect(
        page.getByRole("heading", { name: item.heading }).first(),
      ).toBeVisible();
      await assertVisualBoundary(page);
      await page.screenshot({
        path: path.join(outputDirectory, `${viewport.label}-${item.name}.png`),
        fullPage: false,
        animations: "disabled",
      });
    });
  }
}
