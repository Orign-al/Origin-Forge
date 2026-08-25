import { defineConfig, devices } from "@playwright/test";

const requestedPortText = process.env.PORTAL_E2E_PORT ?? "18080";
const requestedPort = Number(requestedPortText);
if (
  !Number.isInteger(requestedPort) ||
  requestedPort < 1024 ||
  requestedPort > 65535
) {
  throw new Error("PORTAL_E2E_PORT must be an unprivileged TCP port");
}
const testOrigin = `http://127.0.0.1:${requestedPort}`;
const useProductionBuild = process.env.PORTAL_E2E_USE_PRODUCTION_BUILD === "1";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: testOrigin,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command: useProductionBuild
      ? `./node_modules/.bin/next start -H 127.0.0.1 -p ${requestedPort}`
      : `./node_modules/.bin/next dev --webpack -H 127.0.0.1 -p ${requestedPort}`,
    url: `${testOrigin}/login`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
