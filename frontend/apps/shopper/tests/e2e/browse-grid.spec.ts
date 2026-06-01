/**
 * BrowseGrid E2E — exercises the catalog browse surface end-to-end against the
 * live stack (stack-up): real /api/catalog proxy → Python API → ~5000-product
 * Fashion200k catalog.
 *
 * What the unit tests already cover (mocked fetch): render, formatting, loading
 * + empty + error states, pagination state changes, Retry click. What only a
 * real browser against the live stack pins:
 *
 *   1. The catalog grid populates from the real /api/catalog (the proxy →
 *      Python API → DB round-trip, not a mock).
 *   2. Pagination Next/Previous actually fetches the next/previous page from
 *      the live API (not just toggles internal state).
 *   3. The Retry affordance recovers after a real /api/catalog failure
 *      (page.route abort → Retry → second fetch goes through).
 *
 * Run (with the stack up):
 *   just stack-up
 *   cd frontend/apps/shopper && pnpm exec playwright test tests/e2e/browse-grid.spec.ts
 */

import { test, expect } from "@playwright/test";

test.describe("BrowseGrid (catalog browse surface)", () => {
  test("populates from the live /api/catalog on load", async ({ page }) => {
    await page.goto("/");

    const grid = page.locator(".browse-grid__items");
    await expect(grid).toBeVisible({ timeout: 15_000 });

    // At least one card from the real catalog.
    const cards = grid.locator(".product-card");
    await expect(cards.first()).toBeVisible();
    await expect(cards.first().locator(".product-card__title")).not.toBeEmpty();
  });

  test("Next advances the page set; Previous restores it", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(".browse-grid__items")).toBeVisible({
      timeout: 15_000,
    });

    const status = page.locator(".browse-grid__page-status");
    await expect(status).toContainText(/Page 1 of/);

    // Capture page-1 titles so we can verify the set changes after Next and
    // restores after Previous. (Fashion200k titles are distinct, so a stable
    // set comparison is safe.)
    const page1Titles = await page
      .locator(".browse-grid__items .product-card__title")
      .allTextContents();
    expect(page1Titles.length).toBeGreaterThan(0);

    await page.getByRole("button", { name: /^next$/i }).click();
    await expect(status).toContainText(/Page 2 of/);

    const page2Titles = await page
      .locator(".browse-grid__items .product-card__title")
      .allTextContents();
    // Page 2 must NOT be the same set as page 1 — that's the whole point of
    // pagination through a real catalog. A stable-state hit would mean the
    // server ignored ?page=2.
    expect(page2Titles).not.toEqual(page1Titles);

    await page.getByRole("button", { name: /previous/i }).click();
    await expect(status).toContainText(/Page 1 of/);

    const backTo1 = await page
      .locator(".browse-grid__items .product-card__title")
      .allTextContents();
    expect(backTo1).toEqual(page1Titles);
  });

  test("Retry recovers the grid after a /api/catalog failure", async ({
    page,
  }) => {
    // Abort the first /api/catalog request only — subsequent requests (the
    // Retry refetch) hit the real proxy.
    let aborted = false;
    await page.route("**/api/catalog*", async (route) => {
      if (!aborted) {
        aborted = true;
        await route.abort("failed");
      } else {
        await route.continue();
      }
    });

    await page.goto("/");

    // The aborted fetch surfaces the error affordance.
    await expect(page.getByTestId("browse-grid-error")).toBeVisible({
      timeout: 10_000,
    });

    // Retry must fire a SECOND fetch — the one my route handler now allows
    // through. The grid then populates from the real catalog.
    await page.getByRole("button", { name: /retry/i }).click();

    await expect(page.locator(".browse-grid__items")).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByTestId("browse-grid-error")).not.toBeVisible();
  });
});
