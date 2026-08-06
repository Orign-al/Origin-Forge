import { existsSync, mkdirSync, readFileSync } from "node:fs";
import path from "node:path";

import { expect, test, type Page, type Route } from "@playwright/test";

type Snapshot = {
  api: Record<string, unknown>;
  primary_user_id: string;
};

const snapshotPath = process.env.PORTAL3A_REAL_SNAPSHOT ?? "";
const snapshot: Snapshot | null =
  snapshotPath && existsSync(snapshotPath)
    ? (JSON.parse(readFileSync(snapshotPath, "utf8")) as Snapshot)
    : null;
const outputDirectory =
  process.env.PORTAL3A_SCREENSHOT_DIR ??
  path.resolve("test-results", "portal3a-visual-acceptance");

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
      await json(route, { csrf_token: "portal3a-visual-csrf" });
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

for (const viewport of [
  { width: 1366, height: 768, label: "1366x768" },
  { width: 1920, height: 1080, label: "1920x1080" },
]) {
  test(`Portal-3A compute plan ${viewport.label}`, async ({ page }) => {
    test.skip(!snapshot, "PORTAL3A_REAL_SNAPSHOT is required");
    mkdirSync(outputDirectory, { recursive: true });
    await page.setViewportSize(viewport);
    await installSnapshot(page);
    await page.goto(`/users/${snapshot?.primary_user_id ?? "missing"}`);
    await page.getByRole("tab", { name: "计算资源" }).click();
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
    await assertVisualBoundary(page);
    await page.screenshot({
      path: path.join(
        outputDirectory,
        `${viewport.label}-origin-pilot-plan.png`,
      ),
      fullPage: false,
      animations: "disabled",
    });
  });
}
