/**
 * E2E tests for the chat image upload flow.
 *
 * Requires AVSA_ORCHESTRATOR_STUB=1 (canned SSE response from the stub server).
 * The Next.js dev server must be running on http://localhost:3000 before these
 * tests execute (started via the webServer config in playwright.config.ts or
 * by the CI job's pre-step).
 *
 * Axe-core integration: zero critical/serious violations on the chat page.
 */

import path from "path";
import { fileURLToPath } from "url";
import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_IMAGE = path.resolve(
  __dirname,
  "../fixtures/product-photo.jpg",
);

test.describe("Chat image upload flow", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
  });

  test("page loads with accessible heading", async ({ page }) => {
    await expect(
      page.getByRole("heading", { level: 1, name: "AVSA Shopper" }),
    ).toBeVisible();
  });

  test("upload image via file input and submit triggers product cards", async ({
    page,
  }) => {
    // Upload the fixture image via the hidden file input
    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles(FIXTURE_IMAGE);

    // Submit the form
    const submitButton = page.getByRole("button", { name: /search/i });
    await submitButton.click();

    // Wait for at least one product card to appear (from the stub SSE response)
    await expect(page.locator(".product-card").first()).toBeVisible({
      timeout: 15000,
    });
  });

  test("axe-core: zero critical/serious violations on the chat page", async ({
    page,
  }) => {
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();

    const seriousOrCritical = results.violations.filter(
      (v) => v.impact === "critical" || v.impact === "serious",
    );

    expect(
      seriousOrCritical,
      `axe-core found ${seriousOrCritical.length} critical/serious violations:\n` +
        seriousOrCritical
          .map((v) => `  [${v.impact}] ${v.id}: ${v.description}`)
          .join("\n"),
    ).toHaveLength(0);
  });
});
