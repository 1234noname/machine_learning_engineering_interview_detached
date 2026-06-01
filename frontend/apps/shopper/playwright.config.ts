import { defineConfig, devices } from "@playwright/test";
import type { PlaywrightTestConfig } from "@playwright/test";

/**
 * Playwright configuration for AVSA Shopper E2E tests.
 *
 * The shopper is built with `output: "standalone"` so the web server must be
 * started with `node .next/standalone/apps/shopper/server.js` rather than
 * `next start`. For a full run, bring the stack up with `just stack-up` (it
 * serves the shopper on :3000) — reuseExistingServer below reuses it.
 *
 * Set AVSA_API_URL=http://localhost:8080 so the /chat proxy route reaches the
 * Python API. Set AVSA_ORCHESTRATOR_STUB=1 on the API process to use stub mode.
 *
 * Run: pnpm exec playwright test
 */

// Port is configurable so the same config works in environments where :3000 is
// occupied (e.g. by a Colima Lima mux tunnel). Set PLAYWRIGHT_PORT to override.
const port = process.env["PLAYWRIGHT_PORT"] ?? "3000";
const baseURL = `http://localhost:${port}`;

// Build the config object explicitly so we can conditionally omit webServer
// without hitting exactOptionalPropertyTypes conflicts in strict mode.
const config: PlaywrightTestConfig = {
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env["CI"],
  retries: process.env["CI"] ? 2 : 0,
  workers: process.env["CI"] ? 1 : undefined,
  reporter: "html",
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
};

// Start the Next.js standalone server for local runs.
// Requires `pnpm run build` to have been run first so the standalone output
// and its static assets are present. CI starts the server as a separate step.
if (!process.env["CI"]) {
  config.webServer = {
    command: `PORT=${port} node .next/standalone/apps/shopper/server.js`,
    url: baseURL,
    reuseExistingServer: true,
    timeout: 120000,
  };
}

export default defineConfig(config);
