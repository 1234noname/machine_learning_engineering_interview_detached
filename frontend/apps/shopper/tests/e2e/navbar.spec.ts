/**
 * Navbar (site-header) E2E — the only browser-level coverage of the global
 * header. Before this spec the only navbar assertion across the suite was the
 * brand H1 as a "page loaded" smoke (in full-journey + chat-image-upload);
 * HealthBadge has its own spec. Everything else in the header lived untested.
 *
 * The conditional Metrics + API Docs links are env-gated on
 * NEXT_PUBLIC_GRAFANA_URL / NEXT_PUBLIC_API_DOCS_URL. `just stack-up` now sets
 * those to the local Grafana baseline dashboard and the shopper's own
 * /api/docs proxy (which forwards to the API's FastAPI Swagger UI), so both
 * render against the running local stack and we can assert their href +
 * target + rel security attributes.
 *
 * Run (with the stack up):
 *   just stack-up
 *   cd frontend/apps/shopper && pnpm exec playwright test tests/e2e/navbar.spec.ts
 */

import { test, expect } from "@playwright/test";

test.describe("Navbar (site-header)", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
  });

  test("brand link points to / and contains the AVSA Shopper H1", async ({
    page,
  }) => {
    const brand = page.locator(".site-header__brand");
    await expect(brand).toBeVisible();
    await expect(brand).toHaveAttribute("href", "/");
    await expect(brand.locator("h1")).toHaveText("AVSA Shopper");
  });

  test("tagline renders", async ({ page }) => {
    await expect(page.locator(".site-header__tagline")).toHaveText(
      "AI-powered visual search",
    );
  });

  test("Main navigation contains the Search link, marked active on /", async ({
    page,
  }) => {
    const nav = page.getByRole("navigation", { name: "Main navigation" });
    await expect(nav).toBeVisible();

    const search = nav.getByRole("link", { name: "Search" });
    await expect(search).toBeVisible();
    await expect(search).toHaveAttribute("href", "/");
    // The active modifier reflects the current route — guards against a
    // regression where the Search link silently loses its active-state class.
    await expect(search).toHaveClass(/site-header__nav-link--active/);
  });

  test("Metrics link points at the local Grafana dashboard + opens in a new tab securely", async ({
    page,
  }) => {
    const metrics = page.getByRole("link", { name: "Metrics" });
    await expect(metrics).toBeVisible();
    await expect(metrics).toHaveAttribute(
      "href",
      "http://localhost:3010/d/avsa-operations",
    );
    await expect(metrics).toHaveAttribute("target", "_blank");
    // rel must carry both noopener and noreferrer (reverse-tabnabbing guard);
    // either order is fine.
    await expect(metrics).toHaveAttribute("rel", /noopener/);
    await expect(metrics).toHaveAttribute("rel", /noreferrer/);
  });

  test("API Docs link points at the local /api/docs proxy + opens in a new tab securely", async ({
    page,
  }) => {
    const docs = page.getByRole("link", { name: "API Docs" });
    await expect(docs).toBeVisible();
    await expect(docs).toHaveAttribute(
      "href",
      "http://localhost:3000/api/docs",
    );
    await expect(docs).toHaveAttribute("target", "_blank");
    await expect(docs).toHaveAttribute("rel", /noopener/);
    await expect(docs).toHaveAttribute("rel", /noreferrer/);
  });
});
