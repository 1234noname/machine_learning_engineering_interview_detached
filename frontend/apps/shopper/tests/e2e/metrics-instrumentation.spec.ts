/**
 * Metrics-instrumentation suite.
 *
 * Verifies that every instrumented metric is exported at the correct endpoint
 * and that live user flows (or equivalent direct-API drives) actually cause
 * counters and histograms to increment.
 *
 * Structure
 * ─────────
 * GROUP 1: Format / presence checks — all 5 endpoints, no user flow needed.
 *          Hard-to-trigger metrics (circuit melt/reset, upstream errors) are
 *          included here so CI catches if a metric disappears from the codebase.
 * GROUP 2: Text search flow — shopper, API, and orchestrator metrics.
 * GROUP 3: Image search flow — batcher and orchestrator attribute metrics.
 * GROUP 4: Embed cache hit — same image in two consecutive turns.
 * GROUP 5: Rate limit trigger — exhausts per-IP window, asserts 429 counter.
 * GROUP 6: Server-Timing header — direct API POST.
 * GROUP 7: Circuit state — polls for avsa_circuit_state after startup.
 *
 * Prerequisites:
 *   - Shopper frontend  :3000  (/api/metrics and /chat)
 *   - AVSA API          :8080  (/metrics and /chat)
 *   - AVSA batcher      :8081  (/metrics and /embed)
 *   - AVSA orchestrator :9568  (/metrics)
 *   - AVSA model        :8090  (/metrics)
 *
 * Run with: pnpm exec playwright test metrics-instrumentation
 */

import { test, expect, type APIRequestContext } from "@playwright/test";
import { readFileSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { randomUUID } from "crypto";
import { scrapeMetricValue, waitForMetricIncrease } from "./helpers/metrics";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ─── Endpoints ───────────────────────────────────────────────────────────────

const SHOPPER_METRICS = "http://localhost:3000/api/metrics";
const API_METRICS = "http://localhost:8080/metrics";
const BATCHER_METRICS = "http://localhost:8081/metrics";
const ORCH_METRICS = "http://localhost:9568/metrics";
const MODEL_METRICS = "http://localhost:8090/metrics";
const API_BASE = "http://localhost:8080";
// Toxiproxy control API — used by circuit-breaker tests to inject failures.
// Toxiproxy proxies :18081 → :8081 (batcher); the orchestrator is pointed at
// :18081 via AVSA_BATCHER_URL so all embed calls pass through it.
const TOXI_API = "http://localhost:8474";

// ─── Shared helpers ───────────────────────────────────────────────────────────

const RPM = Number(process.env["AVSA_RATE_LIMIT_RPM"] ?? 60);

/**
 * Poll a metrics endpoint until it returns a non-empty Prometheus text body
 * (i.e. contains "# HELP"). Handles two transient conditions:
 *   1. Service just started — port open but handler not yet initialised.
 *   2. TelemetryMetricsPrometheus (Elixir) — only emits a metric after the
 *      first event; on a freshly-started orchestrator, most counters are
 *      absent until at least one request has been processed.
 */
async function waitForMetricsBody(
  request: APIRequestContext,
  url: string,
  timeoutMs = 8_000,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await request.get(url);
      if (res.ok()) {
        const text = await res.text();
        if (text.includes("# HELP")) return text;
      }
    } catch { /* endpoint not yet ready */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`${url} did not return Prometheus text within ${timeoutMs}ms`);
}

const FIXTURE_IMAGE = path.join(__dirname, "../fixtures/product-photo.jpg");

function uniqueIp(): string {
  const octet = (): number => Math.floor(Math.random() * 254) + 1;
  return `10.${octet()}.${octet()}.${octet()}`;
}

// Bad-MIME trick from rate-limit.spec.ts: the limiter counts the request then
// the API 415s immediately — no embed/LLM cost, no network round-trip to model.
async function hitChat(
  request: APIRequestContext,
  ip: string,
): Promise<{ status: number }> {
  const response = await request.post(`${API_BASE}/chat`, {
    headers: { "x-forwarded-for": ip },
    multipart: {
      image: {
        name: "not-an-image.pdf",
        mimeType: "application/pdf",
        buffer: Buffer.from("%PDF-1.4 not an image"),
      },
    },
    timeout: 15_000,
  });
  return { status: response.status() };
}

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 1: Format / presence checks
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Format checks — metric names present at each endpoint", () => {
  test("Shopper /api/metrics exports expected metric names", async ({ request }) => {
    const res = await request.get(SHOPPER_METRICS);
    expect(res.ok()).toBeTruthy();
    const text = await res.text();
    expect(text).toContain("# HELP");
    for (const name of [
      "avsa_shopper_chat_requests_total",
      "avsa_shopper_chat_duration_seconds",
    ]) {
      expect(text, `Shopper metrics missing ${name}`).toContain(name);
    }
  });

  test("API /metrics exports expected metric names", async ({ request }) => {
    const res = await request.get(API_METRICS);
    expect(res.ok()).toBeTruthy();
    const text = await res.text();
    expect(text).toContain("# HELP");
    for (const name of [
      "http_requests_total",
      "avsa_api_rate_limit_total",
      "avsa_api_orchestrator_call_duration_seconds",
    ]) {
      expect(text, `API metrics missing ${name}`).toContain(name);
    }
  });

  test("Batcher /metrics exports expected metric names", async ({ request }) => {
    test.setTimeout(30_000);
    const imageBytes = readFileSync(FIXTURE_IMAGE);
    // Fire one image request to initialise lazy_static counters in the Rust batcher —
    // avsa_batcher_requests_total won't appear until the first request is processed.
    await request.post(`${API_BASE}/chat`, {
      multipart: {
        image: { name: "product.jpg", mimeType: "image/jpeg", buffer: imageBytes },
        text: "warm-up for batcher metrics",
      },
      timeout: 15_000,
    }).catch(() => {});

    // Poll until the counter is visible (lazy_static registration is deferred).
    const deadline = Date.now() + 15_000;
    let text = "";
    while (Date.now() < deadline) {
      const res = await request.get(BATCHER_METRICS).catch(() => null);
      if (res?.ok()) {
        text = await res.text();
        if (text.includes("avsa_batcher_requests_total")) break;
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    for (const name of [
      "avsa_batcher_requests_total",
      "avsa_batcher_request_latency_seconds",
      "avsa_batcher_queue_depth",
      "avsa_batcher_flush_latency_seconds",
    ]) {
      expect(text, `Batcher metrics missing ${name}`).toContain(name);
    }
  });

  test("Orchestrator /metrics exports expected metric names", async ({ request }) => {
    // TelemetryMetricsPrometheus only emits a metric after it has received at
    // least one event. Warm up the pipeline with a real IMAGE chat request so
    // that ALL counters/histograms appear, including:
    //   - avsa_orch_tool_retrieval_duration_seconds (image retrieval path only;
    //     text-only queries fire retrieval_text which has no registered metric)
    //   - avsa_embed_latency_seconds, avsa_attribute_* (require the ViT/batcher)
    const imageBytes = readFileSync(FIXTURE_IMAGE);
    await request.post(`${API_BASE}/chat`, {
      multipart: {
        image: { name: "product.jpg", mimeType: "image/jpeg", buffer: imageBytes },
        text: "warm-up query for metrics format check",
      },
      timeout: 30_000,
    });
    const text = await waitForMetricsBody(request, ORCH_METRICS);
    for (const name of [
      // conversation lifecycle
      "avsa_chat_outcome_total",
      "avsa_conversation_latency_seconds",
      // embed pipeline — ViT/text latency tracked by the orchestrator
      "avsa_embed_latency_seconds",
      "avsa_text_embed_latency_seconds",
      "avsa_vit_qps_total",
      // retrieval
      "avsa_orch_tool_retrieval_duration_seconds",
      "avsa_retrieval_results",
      // verifier
      "avsa_verifier_outcome_total",
      // attribute pipeline
      "avsa_attribute_source_total",
      "avsa_attribute_prediction_total",
      "avsa_attribute_confidence",
      "avsa_attribute_llm_calls_total",
      "avsa_orch_tool_attribute_duration_seconds",
      // tool dispatch counters
      "avsa_tool_dispatch_extract_attributes_total",
      "avsa_tool_dispatch_find_similar_total",
      // embed cache — added by this PR; visible after the first embed event
      "avsa_embed_cache_hit_total",
      "avsa_embed_cache_miss_total",
      // circuit state — emitted immediately by CircuitMonitor at startup
      "avsa_circuit_state",
    ]) {
      expect(text, `Orchestrator metrics missing ${name}`).toContain(name);
    }
  });


  test("Model /metrics returns valid Prometheus text", async ({ request }) => {
    // The model service at :8090 exposes only HTTP framework metrics (no avsa_*
    // names). Custom model metrics (avsa_embed_latency_seconds, avsa_vit_qps_total)
    // are tracked on the orchestrator side, which calls the model.
    const res = await request.get(MODEL_METRICS);
    expect(res.ok()).toBeTruthy();
    const text = await res.text();
    expect(text).toContain("# HELP");
    expect(text).toContain("http_requests_total");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 2: Text search flow
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Text search flow — shopper, API, and orchestrator counters", () => {
  test.setTimeout(90_000);

  test("text query increments counters across the full pipeline", async ({
    page,
    request,
  }) => {
    const beforeShopper = (await scrapeMetricValue(
      request, SHOPPER_METRICS, "avsa_shopper_chat_requests_total",
    )) ?? 0;
    const beforeApiOrch = (await scrapeMetricValue(
      request, API_METRICS, "avsa_api_orchestrator_call_duration_seconds_count",
    )) ?? 0;
    // conversation_latency has labels {outcome, modality}; modality=text = text-only queries.
    // avsa_chat_outcome_total has labels {outcome}; filter to "success" so we track
    // the success counter, which increments on completion, not the error counter.
    const beforeConvLatency = (await scrapeMetricValue(
      request, ORCH_METRICS, "avsa_conversation_latency_seconds_count",
      { outcome: "success", modality: "text" },
    )) ?? 0;
    const beforeChatOutcome = (await scrapeMetricValue(
      request, ORCH_METRICS, "avsa_chat_outcome_total", { outcome: "success" },
    )) ?? 0;
    const beforeMissText = (await scrapeMetricValue(
      request, ORCH_METRICS, "avsa_text_embed_latency_seconds_count",
    )) ?? 0;

    await page.goto("/");
    await page.getByLabel("Search query").fill("a blue summer dress");
    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/chat") && r.request().method() === "POST"),
      page.getByRole("button", { name: /search/i }).click(),
    ]);

    await waitForMetricIncrease(request, SHOPPER_METRICS, "avsa_shopper_chat_requests_total", beforeShopper);
    await waitForMetricIncrease(request, API_METRICS, "avsa_api_orchestrator_call_duration_seconds_count", beforeApiOrch, undefined, 45_000);
    // conversation_latency and chat_outcome fire after the full LLM pipeline completes.
    // Allow 45 s: the orchestrator makes at least two LLM round-trips.
    await waitForMetricIncrease(request, ORCH_METRICS, "avsa_conversation_latency_seconds_count", beforeConvLatency,
      { outcome: "success", modality: "text" }, 45_000);
    await waitForMetricIncrease(request, ORCH_METRICS, "avsa_chat_outcome_total", beforeChatOutcome,
      { outcome: "success" }, 45_000);
    // avsa_text_embed_latency_seconds fires for each text query embed
    await waitForMetricIncrease(request, ORCH_METRICS, "avsa_text_embed_latency_seconds_count", beforeMissText, undefined, 45_000);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 3: Image search flow
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Image search flow — batcher and orchestrator attribute metrics", () => {
  test.setTimeout(90_000);

  test("image upload increments batcher request counters and attribute pipeline metrics", async ({
    page,
    request,
  }) => {
    const beforeBatcherOk = (await scrapeMetricValue(
      request, BATCHER_METRICS, "avsa_batcher_requests_total", { outcome: "ok" },
    )) ?? 0;
    const beforeBatcherLatency = (await scrapeMetricValue(
      request, BATCHER_METRICS, "avsa_batcher_request_latency_seconds_count",
    )) ?? 0;
    // attribute_source_total has labels {attribute, source}; filter to a label set
    // that fires on every image request: ViT always provides category for image uploads.
    const beforeAttrSource = (await scrapeMetricValue(
      request, ORCH_METRICS, "avsa_attribute_source_total",
      { attribute: "category", source: "vit" },
    )) ?? 0;
    const beforeAttrDuration = (await scrapeMetricValue(
      request, ORCH_METRICS, "avsa_orch_tool_attribute_duration_seconds_count",
    )) ?? 0;

    await page.goto("/");
    await page.locator('input[type="file"]').setInputFiles(FIXTURE_IMAGE);
    await page.getByLabel("Search query").fill("find similar items");
    await Promise.all([
      page.waitForResponse((r) => r.url().includes("/chat") && r.request().method() === "POST"),
      page.getByRole("button", { name: /search/i }).click(),
    ]);

    // Batcher counters are updated synchronously — should be visible quickly.
    await waitForMetricIncrease(request, BATCHER_METRICS, "avsa_batcher_requests_total", beforeBatcherOk, { outcome: "ok" });
    await waitForMetricIncrease(request, BATCHER_METRICS, "avsa_batcher_request_latency_seconds_count", beforeBatcherLatency);
    // Orchestrator attribute pipeline metrics fire after the full embed+attribute pass.
    // Allow 20 s: the attribute tool takes 1–5 s; page.waitForResponse resolves on the
    // first SSE chunk, not conversation completion, so the pipeline may still be running.
    await waitForMetricIncrease(request, ORCH_METRICS, "avsa_attribute_source_total", beforeAttrSource,
      { attribute: "category", source: "vit" }, 20_000);
    await waitForMetricIncrease(request, ORCH_METRICS, "avsa_orch_tool_attribute_duration_seconds_count", beforeAttrDuration, undefined, 20_000);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 4: Embed cache hit
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Embed cache — same image in two consecutive turns hits the cache", () => {
  test.setTimeout(60_000);

  test("second turn with the same image bytes increments embed_cache_hit_total", async ({
    request,
  }) => {
    const imageBytes = readFileSync(FIXTURE_IMAGE);
    const conversationId = randomUUID();

    const beforeMiss = (await scrapeMetricValue(
      request, ORCH_METRICS, "avsa_embed_cache_miss_total", { modality: "image" },
    )) ?? 0;
    const beforeHit = (await scrapeMetricValue(
      request, ORCH_METRICS, "avsa_embed_cache_hit_total", { modality: "image" },
    )) ?? 0;

    // Turn 1: first time this image is seen — must be a cache miss.
    await request.post(`${API_BASE}/chat`, {
      multipart: {
        image: { name: "product.jpg", mimeType: "image/jpeg", buffer: imageBytes },
        text: "what is this item",
        conversation_id: conversationId,
      },
    });

    const afterFirstMiss = await waitForMetricIncrease(
      request, ORCH_METRICS, "avsa_embed_cache_miss_total", beforeMiss, { modality: "image" },
    );

    // Turn 2: same image bytes, same conversation — must be a cache hit.
    await request.post(`${API_BASE}/chat`, {
      multipart: {
        image: { name: "product.jpg", mimeType: "image/jpeg", buffer: imageBytes },
        text: "show me similar items please",
        conversation_id: conversationId,
      },
    });

    await waitForMetricIncrease(
      request, ORCH_METRICS, "avsa_embed_cache_hit_total", beforeHit, { modality: "image" },
    );
    // We assert only that the hit counter grew. The orchestrator embeds the same
    // image across multiple tool calls per turn (attribute + retrieval), so one
    // additional miss per turn alongside a hit is expected behaviour.
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 5: Rate limit trigger
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Rate limit — avsa_api_rate_limit_total increments on 429", () => {
  test.setTimeout(120_000);

  test("exhausting the per-IP window increments avsa_api_rate_limit_total", async ({
    request,
  }) => {
    const before = (await scrapeMetricValue(
      request, API_METRICS, "avsa_api_rate_limit_total",
    )) ?? 0;

    const ip = uniqueIp();
    for (let i = 0; i < RPM; i++) {
      await hitChat(request, ip);
    }
    const { status } = await hitChat(request, ip);
    expect(status, "window exhausted — should be rate-limited").toBe(429);

    await waitForMetricIncrease(request, API_METRICS, "avsa_api_rate_limit_total", before);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 6: Server-Timing header
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Server-Timing header", () => {
  test.setTimeout(30_000);
  test("POST /chat on the API includes Server-Timing: api;dur=N", async ({ request }) => {
    // The shopper proxy (app/chat/route.ts) does not forward the server-timing
    // header, so we test the API directly.  APIRequestContext buffers the full
    // SSE body before returning; a text-only query produces a short stream that
    // terminates once the LLM finishes (~5-15 s), well within the 25 s timeout.
    const response = await request.post(`${API_BASE}/chat`, {
      multipart: { text: "server-timing probe" },
      timeout: 25_000,
    });
    expect(response.headers()["server-timing"]).toMatch(/api;dur=/);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 7: Circuit state (requires CircuitMonitor startup poll)
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Circuit breaker state — avsa_circuit_state populated by CircuitMonitor", () => {
  test.setTimeout(15_000);

  test("avsa_circuit_state is present within 10s of orchestrator startup", async ({
    request,
  }) => {
    // CircuitMonitor.init/1 sends itself :poll immediately so the first gauge
    // emission happens at startup. Allow 10s of headroom for slow boot.
    const deadline = Date.now() + 10_000;
    let value: number | null = null;
    while (Date.now() < deadline) {
      value = await scrapeMetricValue(request, ORCH_METRICS, "avsa_circuit_state");
      if (value !== null) break;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    expect(value, "avsa_circuit_state never appeared in orchestrator /metrics").not.toBeNull();
    // 0 = CLOSED (healthy), 1 = BLOWN. Either is valid; we just assert presence.
    expect(value).toBeGreaterThanOrEqual(0);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 8: Circuit breaker melt and reset (requires Toxiproxy)
//
// Architecture: orchestrator → Toxiproxy :18081 → batcher :8081
// The batcher_circuit fuse config: {{:standard, 5, 10_000}, {:reset, 60_000}}
//   5 failures within 10 s blows the circuit; auto-resets after 60 s.
// CircuitMonitor polls every 5 s and emits avsa_circuit_reset_total on
// the first blown→ok transition it observes after the fuse auto-resets.
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Circuit breaker melt and reset (Toxiproxy)", () => {
  // 75 s guard (circuit may be blown from parallel test runs) + 6 fast-failing
  // image requests + 60 s fuse reset window + 10 s poll buffer
  test.setTimeout(200_000);

  test("batcher_circuit melts under injected failures and resets after recovery", async ({
    request,
  }) => {
    const imageBytes = readFileSync(FIXTURE_IMAGE);

    const beforeMelt = (await scrapeMetricValue(
      request, ORCH_METRICS, "avsa_circuit_melt_total", { breaker: "batcher_circuit" },
    )) ?? 0;
    const beforeReset = (await scrapeMetricValue(
      request, ORCH_METRICS, "avsa_circuit_reset_total", { breaker: "batcher_circuit" },
    )) ?? 0;

    // Guard: if the circuit is already blown from a previous test run (fuse
    // blows after 5 melts in 10 s and takes 60 s to auto-reset), requests will
    // short-circuit with {:error, :circuit_open} instead of reaching the embed
    // step — no new melts are possible. Wait for avsa_circuit_state to return
    // to 0 (closed) before injecting failures. Allow 75 s (60 s fuse + 5 s
    // CircuitMonitor poll interval + margin).
    {
      const circuitDeadline = Date.now() + 75_000;
      while (Date.now() < circuitDeadline) {
        const state = await scrapeMetricValue(
          request, ORCH_METRICS, "avsa_circuit_state", { breaker: "batcher_circuit" },
        );
        if (state === null || state === 0) break;
        await new Promise((r) => setTimeout(r, 2_000));
      }
    }

    // Toxiproxy toxics only apply to NEW connections. Finch's connection pool
    // keeps a persistent HTTP/1.1 keep-alive to Toxiproxy from the warm-up
    // requests that run in earlier test groups.  Simply adding a toxic has no
    // effect on that existing connection.
    //
    // Fix: delete and recreate the proxy before injecting the toxic.  Deleting
    // kills all open connections immediately; Finch reconnects on the next
    // request — that fresh connection then hits the toxic.
    //
    // Toxic choice: `reset_peer` on `upstream` (timeout: 0).
    // This immediately RSTs the Toxiproxy→batcher connection on every new
    // request, causing Finch to receive {:error, :closed}. The embed step
    // treats any {:error, _} as a melt.  `timeout` downstream was ineffective
    // because the batcher responds in ~20 ms, well within any timeout window.
    // Use native fetch for Toxiproxy control calls — Playwright's APIRequestContext
    // routes through the browser's network stack (for HAR recording, tracing, etc.)
    // which can interfere with non-browser-target requests like the Toxiproxy REST API.
    await fetch(`${TOXI_API}/proxies/batcher`, { method: "DELETE" });
    await fetch(`${TOXI_API}/populate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify([{
        name: "batcher",
        listen: "0.0.0.0:18081",
        upstream: "host.docker.internal:8081",
        enabled: true,
      }]),
    });
    await fetch(`${TOXI_API}/proxies/batcher/toxics`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: "rst_batcher",
        type: "reset_peer",
        stream: "upstream",
        toxicity: 1.0,
        attributes: { timeout: 0 },
      }),
    });

    try {
      // Send 6 image requests IN PARALLEL so all LLM calls fire at the same time.
      // Each request calls the embed step once the LLM responds (~3-5 s); with the
      // toxic active all 6 embed calls fail within the same narrow window, landing
      // 5+ melts inside the 10 s fuse window.  Sequential requests never achieve
      // this because each LLM round-trip alone takes longer than the window allows.
      await Promise.all(
        Array.from({ length: 6 }, (_, i) =>
          request
            .post(`${API_BASE}/chat`, {
              multipart: {
                image: { name: "product.jpg", mimeType: "image/jpeg", buffer: imageBytes },
                text: `circuit melt test ${i}`,
                conversation_id: randomUUID(),
              },
              timeout: 30_000,
            })
            .catch(() => {})
        )
      );

      await waitForMetricIncrease(
        request, ORCH_METRICS, "avsa_circuit_melt_total", beforeMelt, { breaker: "batcher_circuit" }, 30_000,
      );
    } finally {
      // Restore the proxy to clean state — no toxics, batcher reachable again.
      await fetch(`${TOXI_API}/proxies/batcher`, { method: "DELETE" }).catch(() => {});
      await fetch(`${TOXI_API}/populate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify([{
          name: "batcher",
          listen: "0.0.0.0:18081",
          upstream: "host.docker.internal:8081",
          enabled: true,
        }]),
      }).catch(() => {});
    }

    // fuse auto-resets after 60 s; CircuitMonitor then emits avsa_circuit_reset_total
    // on its next 5 s poll. Allow 75 s total.
    await waitForMetricIncrease(
      request, ORCH_METRICS, "avsa_circuit_reset_total", beforeReset,
      { breaker: "batcher_circuit" }, 75_000,
    );

    // Both counter names must now be present in the scrape (they weren't before
    // the first event fired due to TelemetryMetricsPrometheus lazy registration).
    const text = await waitForMetricsBody(request, ORCH_METRICS);
    expect(text).toContain("avsa_circuit_melt_total");
    expect(text).toContain("avsa_circuit_reset_total");
  });
});
