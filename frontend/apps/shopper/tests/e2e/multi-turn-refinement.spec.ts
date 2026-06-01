/**
 * Multi-turn refinement E2E: a follow-up turn resumes the SAME conversation and
 * excludes the products already shown.
 *
 * This exercises the behaviour wired in when the gRPC streaming path was routed
 * through the AVSA.Conversation GenServer: prior_result_ids from turn 1 are
 * carried into turn 2's kNN exclusion. It also guards the Next.js /chat proxy,
 * which must forward X-Resume-Conversation-Id upstream (a regression here makes
 * multi-turn silently never resume, since the API keys resume off that header).
 *
 * Two layers:
 *   Browser (stub + full)  the UI re-search resumes the same conversation id
 *                          end-to-end — works in stub mode because the API
 *                          echoes the resumed id without the orchestrator.
 *   API via proxy (full)   the resumed turn returns a product set DISJOINT from
 *                          the first (same image both turns → identical kNN, so
 *                          a disjoint result proves the exclusion is live).
 *
 * Run (bring the stack up first with `just stack-up`):
 *   cd frontend/apps/shopper && AVSA_E2E_FULL_STACK=1 \
 *     pnpm exec playwright test tests/e2e/multi-turn-refinement.spec.ts
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { test, expect, type Page } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_IMAGE = path.resolve(__dirname, "../fixtures/product-photo.jpg");
const FIXTURE_BYTES = fs.readFileSync(FIXTURE_IMAGE);

const FULL_STACK = process.env["AVSA_E2E_FULL_STACK"] === "1";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface SseFrame {
  type: string;
  card?: { id?: string };
}

/** Extract the non-empty product_card ids from a raw SSE response body, in order. */
function parseCardIds(body: string): string[] {
  return body
    .split("\n\n")
    .filter((f) => f.trim().startsWith("data:"))
    .flatMap((f) => {
      try {
        return [JSON.parse(f.slice(f.indexOf(":") + 1).trim()) as SseFrame];
      } catch {
        return [];
      }
    })
    .filter((e) => e.type === "product_card" && typeof e.card?.id === "string" && e.card.id)
    .map((e) => e.card!.id as string);
}

/** Wait for the first product card from a fresh search after `trigger` runs. */
async function searchAndWaitForCards(page: Page, trigger: () => Promise<void>) {
  const [response] = await Promise.all([
    page.waitForResponse(
      (r) => r.url().endsWith("/chat") && r.request().method() === "POST",
    ),
    trigger(),
  ]);
  await expect(page.locator(".product-card").first()).toBeVisible({ timeout: 15_000 });
  return response;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe("Multi-turn refinement (conversation resume)", () => {
  test(
    "browser re-search resumes the SAME conversation id end-to-end",
    async ({ page }) => {
      await page.goto("/");
      await page.locator('input[type="file"]').setInputFiles(FIXTURE_IMAGE);

      // ── Turn 1 ────────────────────────────────────────────────────────────
      const resp1 = await searchAndWaitForCards(page, () =>
        page.getByRole("button", { name: /search/i }).click(),
      );
      const conv1 = resp1.headers()["x-conversation-id"] ?? "";
      expect(conv1).toMatch(UUID_RE);
      // The conversation id is surfaced in the UI for the shopper.
      await expect(page.getByTestId("session-id-display")).toContainText(conv1);

      // The search button re-enables once the first stream finishes.
      await expect(page.getByRole("button", { name: /search/i })).toBeEnabled();

      // ── Turn 2: search again (image is still staged) — must resume conv1 ───
      const [req2, resp2] = await Promise.all([
        // waitForRequest's predicate receives a Request directly (method() is on
        // it); waitForResponse's receives a Response (request() returns the Request).
        page.waitForRequest(
          (r) => r.url().endsWith("/chat") && r.method() === "POST",
        ),
        page.waitForResponse(
          (r) => r.url().endsWith("/chat") && r.request().method() === "POST",
        ),
        page.getByRole("button", { name: /search/i }).click(),
      ]);

      // The browser sends the resume header carrying turn 1's id...
      expect(req2.headers()["x-resume-conversation-id"]).toBe(conv1);
      // ...and the API resumed the SAME conversation — which only happens if the
      // Next /chat proxy forwarded X-Resume-Conversation-Id upstream. If the
      // proxy dropped it, the API would mint a fresh id here.
      expect(resp2.headers()["x-conversation-id"]).toBe(conv1);

      await expect(page.locator(".product-card").first()).toBeVisible({
        timeout: 15_000,
      });
    },
  );

  test(
    "resumed turn excludes already-shown results (full-stack)",
    async ({ request }) => {
      test.skip(
        !FULL_STACK,
        "requires AVSA_E2E_FULL_STACK=1 and the full service stack (stub mode " +
          "returns one canned card, so disjointness is not observable)",
      );

      // The `request` fixture uses the config baseURL (the Next shopper origin),
      // so this drives the real /chat proxy → API → orchestrator wire path.
      const multipart = {
        image: { name: "product.jpg", mimeType: "image/jpeg", buffer: FIXTURE_BYTES },
        text: "find me something similar",
      };

      // ── Turn 1 ────────────────────────────────────────────────────────────
      const r1 = await request.post("/chat", { multipart });
      expect(r1.status()).toBe(200);
      const conv1 = r1.headers()["x-conversation-id"] ?? "";
      expect(conv1).toMatch(UUID_RE);
      const idsA = parseCardIds(await r1.text());
      expect(idsA.length).toBeGreaterThan(0);

      // ── Turn 2: resume conv1 with the identical query. ────────────────────
      const r2 = await request.post("/chat", {
        headers: { "x-resume-conversation-id": conv1 },
        multipart,
      });
      expect(r2.status()).toBe(200);
      // Resume honoured end-to-end through the proxy.
      expect(r2.headers()["x-conversation-id"]).toBe(conv1);

      const idsB = parseCardIds(await r2.text());
      expect(
        idsB.length,
        "resumed turn returned no products — the e2e catalog must hold more than " +
          "one kNN page (LIMIT 20) so a follow-up turn has fresh items to return",
      ).toBeGreaterThan(0);

      // Same image both turns → identical kNN ordering, so the only reason turn 2
      // differs is prior_result_ids exclusion. A disjoint set proves it is live.
      const overlap = idsB.filter((id) => idsA.includes(id));
      expect(
        overlap,
        "a resumed turn repeated already-shown products — prior_result_ids " +
          "exclusion is not taking effect end-to-end",
      ).toEqual([]);
    },
  );
});
