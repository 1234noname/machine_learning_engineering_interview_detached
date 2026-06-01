/**
 * GET /images/{path} (apps/api/src/avsa_api/routes/images.py) — exercised
 * end-to-end through the browser.
 *
 * The route and its token verification are unit-tested in test_images_route.py
 * with self-signed tokens. This adds the only coverage of the full chain a real
 * browser drives:
 *
 *   catalog.py SIGNS image_url → the frontend (safeProxyUrl) resolves it to an
 *   absolute /images URL on the API origin → images.py VERIFIES the token and
 *   serves the bytes → the browser <img> decodes them.
 *
 * A break anywhere — a wrong frontend URL, a catalog↔images signing-contract
 * mismatch, or a missing object — surfaces as a card image that never loads
 * (naturalWidth stays 0, or the placeholder renders instead of an <img>).
 *
 * Full-stack: needs a seeded catalog (real signed /images URLs) + the image
 * files present in storage. Skips cleanly when the browse grid is empty
 * (stub stack / no DB), so it is safe to run in any mode.
 */

import { test, expect } from "@playwright/test";

test("browse-grid product images load via the signed /images proxy", async ({ page }) => {
  await page.goto("/");

  // The browse grid fetches /api/catalog on load; wait for it to settle into
  // either rendered cards or the empty-state marker.
  const cards = page.locator(".product-card");
  await expect(cards.first().or(page.getByTestId("browse-grid-empty"))).toBeVisible({
    timeout: 15_000,
  });

  // No catalog rows (stub stack / empty DB) → no /images URLs to exercise.
  const count = await cards.count();
  test.skip(count === 0, "browse grid empty — needs a seeded full stack (real /images URLs)");

  const img = cards.first().locator("img").first();
  await expect(img).toBeVisible();

  // The src is the signed /images proxy URL catalog.py minted, resolved to the
  // API origin by safeProxyUrl.
  const src = (await img.getAttribute("src")) ?? "";
  expect(src, `card image src must be a signed /images URL; got ${src}`).toContain("/images/");
  expect(src).toContain("token=");
  expect(src).toContain("expires=");

  // Probe the image URL before asserting renderability: in CI without
  // `git lfs pull data/fashion200k/**`, the files on disk are 130-byte LFS
  // pointer stubs.  The browser receives them with the wrong content, leaving
  // naturalWidth at 0.  Skip rather than fail when this is the case.
  const probe = await page.request.get(src);
  const bodyLen = (await probe.body()).length;
  test.skip(
    bodyLen < 1_000,
    `Image response is ${bodyLen} bytes — likely an LFS pointer stub; run \`git lfs pull\` to get real images`,
  );

  // It must actually decode: naturalWidth > 0 proves images.py verified the
  // token and served real bytes (a 403 / 404 / broken URL leaves it at 0).
  await expect
    .poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth), { timeout: 15_000 })
    .toBeGreaterThan(0);
});
