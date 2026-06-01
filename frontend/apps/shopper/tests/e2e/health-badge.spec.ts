/**
 * HealthBadge E2E — the only browser-level test that the badge actually flips
 * to `API: ok` against the live /api/health proxy (shopper :3000 → Python API
 * :8080 → live). Unit tests for HealthBadge don't exist (and a vitest jsdom
 * test couldn't reach the real proxy anyway), so this is the only guard that
 * the wiring works end-to-end.
 *
 * Run (with the stack up):
 *   just stack-up
 *   cd frontend/apps/shopper && pnpm exec playwright test tests/e2e/health-badge.spec.ts
 */

import { test, expect } from "@playwright/test";

test.describe("HealthBadge", () => {
  test("resolves to API: ok against the live /api/health proxy", async ({
    page,
  }) => {
    await page.goto("/");

    const badge = page.getByTestId("health-badge");
    await expect(badge).toBeVisible();

    // The badge starts as "API: checking…" then resolves once /api/health
    // responds. Against stack-up the API is up — so it must land on "API: ok".
    await expect(badge).toContainText("API: ok", { timeout: 10_000 });

    // The CSS variant class reflects the resolved state — used by globals.css
    // to colour the dot, so this guards the visual cue too.
    await expect(badge).toHaveClass(/health-badge--ok/);
  });
});
