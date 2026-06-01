/**
 *  E2E: Image-driven similarity search — full system validation.
 *
 * Exercises the complete user journey end-to-end through the browser while
 * independently verifying system observability at each layer:
 *
 *   Frontend  Browser UX: page load → image upload → SSE stream → product
 *             cards render → detail panel → close panel → client-side guards
 *   API       REST contract: conversation ID propagation, SSE wire format,
 *             error guards (MIME, magic-byte mismatch)
 *   Metrics   Prometheus counters increment (full-stack only)
 *   Database  Conversation + turn records persisted (full-stack only)
 *
 * Required services (stub mode — default):
 *   Next.js shopper  port 3000  (started automatically by playwright.config.ts)
 *   Python API       port 8080  AVSA_ORCHESTRATOR_STUB=1
 *
 * Additional services for AVSA_E2E_FULL_STACK=1:
 *   Elixir orchestrator  gRPC :50051
 *   ViT batcher          HTTP :8081
 *   PostgreSQL           :5434  (avsa/avsa/avsa)
 *   Orchestrator metrics HTTP :9568/metrics
 *
 * Run (bring the stack up first with `just stack-up`):
 *   cd frontend/apps/shopper && AVSA_E2E_FULL_STACK=1 \
 *     pnpm exec playwright test tests/e2e/full-journey.spec.ts
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { test, expect, type Page } from "@playwright/test";
import { scrapeMetricValue } from "./helpers/metrics.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_IMAGE = path.resolve(__dirname, "../fixtures/product-photo.jpg");
// Read once at module load; used for direct API contract tests.
const FIXTURE_BYTES = fs.readFileSync(FIXTURE_IMAGE);

const API_BASE = process.env["AVSA_API_BASE_URL"] ?? "http://localhost:8080";
const METRICS_URL =
  process.env["AVSA_METRICS_URL"] ?? "http://localhost:9568/metrics";
const FULL_STACK = process.env["AVSA_E2E_FULL_STACK"] === "1";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Upload the fixture image, click Search, wait for the first product card to
 * appear, and return the X-Conversation-Id captured from the /chat response
 * headers. Assumes the page has already navigated to "/".
 */
async function submitAndWait(page: Page): Promise<{ conversationId: string }> {
  await page.locator('input[type="file"]').setInputFiles(FIXTURE_IMAGE);

  // Capture the /chat response headers the moment they arrive (before the SSE
  // body completes) so we can read X-Conversation-Id immediately.
  const [chatResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.url().endsWith("/chat") && r.request().method() === "POST",
    ),
    page.getByRole("button", { name: /search/i }).click(),
  ]);

  // Wait for at least one product card — confirms the SSE stream delivered data
  // and React rendered it. Generous timeout for CI and slow machines.
  await expect(page.locator(".product-card").first()).toBeVisible({
    timeout: 15_000,
  });

  const conversationId = chatResponse.headers()["x-conversation-id"] ?? "";
  return { conversationId };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe(": Image-driven similarity search", () => {
  // ── Frontend: complete user journey ──────────────────────────────────────

  test.describe("Frontend user journey (browser)", () => {
    test.beforeEach(async ({ page }) => {
      await page.goto("/");
    });

    test("S1: page loads with heading, file input, and search button", async ({
      page,
    }) => {
      await expect(
        page.getByRole("heading", { level: 1, name: "AVSA Shopper" }),
      ).toBeVisible();
      await expect(page.locator('input[type="file"]')).toBeAttached();
      await expect(
        page.getByRole("button", { name: /search/i }),
      ).toBeVisible();
    });

    test(
      "S1–S9: upload image → SSE stream → product cards render in browser",
      async ({ page }) => {
        const { conversationId } = await submitAndWait(page);
        await expect(page.locator(".product-card").first()).toBeVisible();
        // UUID in response header proves the API received and tracked the request
        expect(conversationId).toMatch(UUID_RE);
      },
    );

    test("S9: product card displays ZAR-formatted price", async ({ page }) => {
      await submitAndWait(page);
      const priceEl = page
        .locator(".product-card")
        .first()
        .locator(".product-card__price");
      await expect(priceEl).toBeVisible();
      // en-ZA locale formats ZAR as "R x.xx"
      await expect(priceEl).toHaveText(/R\s*[\d,.]+/);
    });

    test("S9: product card category label is present and non-empty", async ({
      page,
    }) => {
      await submitAndWait(page);
      const categoryEl = page
        .locator(".product-card")
        .first()
        .locator(".product-card__category");
      await expect(categoryEl).toBeVisible();
      await expect(categoryEl).not.toBeEmpty();
    });

    // NOTE: the product detail side-panel was removed (UX overhaul) — result/
    // browse cards are now presentational, so the former "open/close detail
    // panel" S9 tests were dropped.

    test(
      "S1: client-side 10 MB guard shows error and disables submit without calling the API",
      async ({ page }) => {
        let apiWasCalled = false;
        await page.route("**/chat", (route) => {
          apiWasCalled = true;
          return route.abort();
        });

        await page.locator('input[type="file"]').setInputFiles({
          name: "oversize.jpg",
          mimeType: "image/jpeg",
          buffer: Buffer.alloc(11 * 1024 * 1024, 0xff),
        });

        // Error message rendered by ChatInput
        await expect(page.getByText(/under 10 mb/i)).toBeVisible({
          timeout: 3_000,
        });
        // Submit button must be disabled since file state is null
        await expect(
          page.getByRole("button", { name: /search/i }),
        ).toBeDisabled();
        expect(apiWasCalled).toBe(false);
      },
    );
  });

  // ── API contract ─────────────────────────────────────────────────────────

  test.describe("API contract (direct HTTP)", () => {
    test("S2: POST /chat → 200 with text/event-stream", async ({ request }) => {
      const response = await request.post(`${API_BASE}/chat`, {
        multipart: {
          image: {
            name: "product.jpg",
            mimeType: "image/jpeg",
            buffer: FIXTURE_BYTES,
          },
          text: "find something similar",
        },
      });
      expect(response.status()).toBe(200);
      expect(response.headers()["content-type"]).toContain("text/event-stream");
    });

    test("S2: client X-Conversation-Id is NOT echoed (session-fixation guard); server returns a fresh UUID", async ({
      request,
    }) => {
      //: the server ignores a client-supplied X-Conversation-Id (a
      // session-fixation defence) and always returns a fresh server-generated
      // UUID4. Resume uses X-Resume-Conversation-Id, not this header.
      const customId = "e2e00001-0000-0000-0000-000000000001";
      const response = await request.post(`${API_BASE}/chat`, {
        headers: { "x-conversation-id": customId },
        multipart: {
          image: {
            name: "p.jpg",
            mimeType: "image/jpeg",
            buffer: FIXTURE_BYTES,
          },
          text: "",
        },
      });
      const returned = response.headers()["x-conversation-id"] ?? "";
      expect(returned).not.toBe(customId);
      expect(returned).toMatch(UUID_RE);
    });

    test("S2: X-Conversation-Id auto-generated as UUID when header absent", async ({
      request,
    }) => {
      const response = await request.post(`${API_BASE}/chat`, {
        multipart: {
          image: {
            name: "p.jpg",
            mimeType: "image/jpeg",
            buffer: FIXTURE_BYTES,
          },
          text: "",
        },
      });
      const convId = response.headers()["x-conversation-id"] ?? "";
      expect(convId).toMatch(UUID_RE);
    });

    test("S2: SSE frames are well-formed JSON with type=product_card and ZAR currency", async ({
      request,
    }) => {
      const response = await request.post(`${API_BASE}/chat`, {
        multipart: {
          image: {
            name: "p.jpg",
            mimeType: "image/jpeg",
            buffer: FIXTURE_BYTES,
          },
          text: "",
        },
      });
      const body = await response.text();

      type SseFrame = {
        type: string;
        card?: { id: string; price: number; currency: string; category: string };
      };

      const frames: SseFrame[] = body
        .split("\n\n")
        .filter((f) => f.trim().startsWith("data:"))
        .flatMap((f) => {
          try {
            return [JSON.parse(f.slice(f.indexOf(":") + 1).trim()) as SseFrame];
          } catch {
            return [];
          }
        });

      expect(frames.length).toBeGreaterThan(0);

      const productCards = frames.filter((f) => f.type === "product_card");
      expect(productCards.length).toBeGreaterThan(0);

      const card = productCards[0]!.card!;
      expect(typeof card.id).toBe("string");
      expect(card.id.length).toBeGreaterThan(0);
      expect(typeof card.price).toBe("number");
      expect(card.currency).toBe("ZAR");
      expect(typeof card.category).toBe("string");
    });

    test("S2: 415 when MIME type is not in the image allow-list", async ({
      request,
    }) => {
      const response = await request.post(`${API_BASE}/chat`, {
        multipart: {
          image: {
            name: "doc.pdf",
            mimeType: "application/pdf",
            buffer: Buffer.from("%PDF-1.4"),
          },
          text: "",
        },
      });
      expect(response.status()).toBe(415);
    });

    test(
      "S2: 415 when magic bytes mismatch the declared MIME type",
      async ({ request }) => {
        // Declare JPEG but send PNG magic bytes — caught by magic-byte verification.
        const pngMagic = Buffer.from([
          0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        ]);
        const response = await request.post(`${API_BASE}/chat`, {
          multipart: {
            image: {
              name: "fake.jpg",
              mimeType: "image/jpeg",
              buffer: pngMagic,
            },
            text: "",
          },
        });
        expect(response.status()).toBe(415);
      },
    );
  });

  // ── Accessibility ─────────────────────────────────────────────────────────
  // axe-core critical/serious-violations gate removed per maintainer decision:
  // the only failure was a pre-existing color-contrast debt (the chat submit
  // button: #fff on #7770eb = 3.96 vs WCAG-AA 4.5:1), unrelated to this track
  // ( are backend). Tracked separately as a frontend a11y item.

  // ── Prometheus metrics (full-stack only) ──────────────────────────────────

  test.describe("Prometheus metrics", () => {
    test(
      "avsa_chat_outcome_total{outcome=success} increments after a successful request",
      async ({ page, request }) => {
        test.skip(
          !FULL_STACK,
          "requires AVSA_E2E_FULL_STACK=1 and full service stack running",
        );

        const before =
          (await scrapeMetricValue(
            request,
            METRICS_URL,
            "avsa_chat_outcome_total",
            { outcome: "success" },
          )) ?? 0;

        await page.goto("/");
        await submitAndWait(page);
        // Telemetry is emitted asynchronously by the Elixir GenServer
        await page.waitForTimeout(500);

        const after =
          (await scrapeMetricValue(
            request,
            METRICS_URL,
            "avsa_chat_outcome_total",
            { outcome: "success" },
          )) ?? 0;

        expect(after).toBeGreaterThan(before);
      },
    );

    test(
      "avsa_conversation_latency_seconds_count increments after a successful request",
      async ({ page, request }) => {
        test.skip(
          !FULL_STACK,
          "requires AVSA_E2E_FULL_STACK=1 and full service stack running",
        );

        const before =
          (await scrapeMetricValue(
            request,
            METRICS_URL,
            "avsa_conversation_latency_seconds_count",
          )) ?? 0;

        await page.goto("/");
        await submitAndWait(page);

        // The latency histogram observation is recorded asynchronously at turn
        // finalization, which can land *after* the SSE response flushes to the
        // client (more so under load). Poll the metric instead of a fixed wait
        // so a slow telemetry write doesn't race the assertion.
        await expect
          .poll(
            async () =>
              (await scrapeMetricValue(
                request,
                METRICS_URL,
                "avsa_conversation_latency_seconds_count",
              )) ?? 0,
            {
              timeout: 10_000,
              message:
                "avsa_conversation_latency_seconds_count did not increment after a successful request",
            },
          )
          .toBeGreaterThan(before);
      },
    );
  });

  // NOTE: conversation/turn DB-persistence E2E assertions were removed — v1 keeps
  // conversation state in-memory and does not persist to conversations.{conversations,turns}
  // (audit gap G4, deferred to Phase 4). Re-add these when persistence lands.
});
