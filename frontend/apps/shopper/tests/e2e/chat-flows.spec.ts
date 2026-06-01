/**
 * Browser-level /chat flows not covered by the single-image full-journey spec
 * or the (mocked-fetch) vitest component tests:
 *
 *   - Multi-image upload UX (#2 thumbnails, #4 combined query, #5 easy removal)
 *     — the real <input multiple> + object-URL thumbnail tray + per-image remove,
 *       which only a real browser exercises.
 *   - Conversation resume — the frontend re-sends X-Resume-Conversation-Id on a
 *     second turn (ChatInput wires it from the first turn's X-Conversation-Id).
 *     The feature is live but its only browser test (text-modality) was removed with
 *     the turn-thread UI, leaving it unverified end-to-end.
 *   - Error rendering — a failed /chat surfaces an accessible alert and the form
 *     recovers (submit not stuck disabled). vitest tests the onError callback;
 *     this asserts the real alert DOM + recovery.
 *
 * Run against a stack with the shopper (:3000) + API (:8080) up — e.g.
 * `just stack-up` — via `pnpm exec playwright test chat-flows`.
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { test, expect } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_BYTES = fs.readFileSync(path.resolve(__dirname, "../fixtures/product-photo.jpg"));

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const jpeg = (name: string) => ({ name, mimeType: "image/jpeg", buffer: FIXTURE_BYTES });

test.beforeEach(async ({ page }) => {
  await page.goto("/");
});

test.describe("Multi-image upload UX", () => {
  test("staged images render as removable thumbnails and submit to product cards", async ({
    page,
  }) => {
    // Two images selected via the real <input multiple> stage as two thumbnails.
    await page.locator('input[type="file"]').setInputFiles([jpeg("a.jpg"), jpeg("b.jpg")]);
    await expect(page.getByTestId("upload-thumb")).toHaveCount(2);

    // Submit the combined (2-image) query → product cards render from the SSE stream.
    const [resp] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/chat") && r.request().method() === "POST"),
      page.getByRole("button", { name: /search/i }).click(),
    ]);
    expect(resp.status()).toBe(200);
    await expect(page.locator(".product-card").first()).toBeVisible({ timeout: 15_000 });
  });

  test("a staged image can be removed before submitting", async ({ page }) => {
    await page.locator('input[type="file"]').setInputFiles([jpeg("a.jpg"), jpeg("b.jpg")]);
    await expect(page.getByTestId("upload-thumb")).toHaveCount(2);

    // Remove the first image; only the second thumbnail remains.
    await page.getByRole("button", { name: "Remove a.jpg" }).click();
    await expect(page.getByTestId("upload-thumb")).toHaveCount(1);
  });
});

test.describe("Conversation resume", () => {
  test("a second turn re-sends X-Resume-Conversation-Id from the first turn", async ({ page }) => {
    // Turn 1 (text-only): capture the server-issued conversation id.
    const [firstResp] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith("/chat") && r.request().method() === "POST"),
      (async () => {
        await page.getByLabel("Search query").fill("a red summer dress");
        await page.getByRole("button", { name: /search/i }).click();
      })(),
    ]);
    const firstConvId = firstResp.headers()["x-conversation-id"] ?? "";
    expect(firstConvId, "first turn must return a server-generated UUID").toMatch(UUID_RE);
    await expect(page.locator(".product-card").first()).toBeVisible({ timeout: 15_000 });

    // Turn 2: the request must carry X-Resume-Conversation-Id == the first id, so
    // the orchestrator continues the same conversation.
    const [secondReq] = await Promise.all([
      page.waitForRequest((r) => r.url().endsWith("/chat") && r.method() === "POST"),
      (async () => {
        await page.getByLabel("Search query").fill("show me more like the first one");
        await page.getByRole("button", { name: /search/i }).click();
      })(),
    ]);
    expect(secondReq.headers()["x-resume-conversation-id"]).toBe(firstConvId);
  });
});

test.describe("Error handling", () => {
  test("a failed /chat surfaces an alert and re-enables the form", async ({ page }) => {
    // Force the chat call to fail at the network boundary.
    await page.route("**/chat", (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "service unavailable" }),
      }),
    );

    await page.getByLabel("Search query").fill("anything");
    await page.getByRole("button", { name: /search/i }).click();

    // The failure is surfaced as an accessible alert...
    await expect(page.getByTestId("chat-error")).toBeVisible();
    // ...and the form recovers — submit is not stuck in the streaming-disabled state.
    await expect(page.getByRole("button", { name: /search/i })).toBeEnabled();
  });
});
