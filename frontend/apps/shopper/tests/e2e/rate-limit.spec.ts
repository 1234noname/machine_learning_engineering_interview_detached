/**
 * Rate-limiter E2E — exercises apps/api/src/avsa_api/middleware/rate_limit.py
 * against a live API (:8080).
 *
 * The limiter is a per-IP sliding 60s window (config [api.rate_limit]
 * requests_per_minute = 60), keyed on the rightmost X-Forwarded-For entry and
 * checked FIRST in the POST /chat handler — before request validation and
 * before any downstream embed / LLM work. We drive it directly over HTTP (no
 * browser, no model), so this runs against any stack with the API up on :8080
 * (e.g. `just stack-up`).
 *
 * Trick: each request carries a bad-MIME image. The limiter runs first and
 * COUNTS the request; the request then 415s immediately on the MIME allow-list
 * (step 4), never reaching the orchestrator. So we can fill the window with
 * fast 415s and observe the (RPM+1)th flip to 429 — with no LLM cost.
 *
 * Run (with the API up on :8080, e.g. `just stack-up`):
 *   AVSA_API_BASE_URL=http://localhost:8080 pnpm exec playwright test rate-limit
 */

import { test, expect, type APIRequestContext } from "@playwright/test";

const API_BASE = process.env["AVSA_API_BASE_URL"] ?? "http://localhost:8080";

// config/avsa.toml [api.rate_limit] requests_per_minute. Mirrors the unit test
// (test_rate_limit.py::test_rate_limit_60_requests_allowed_61st_rejected).
const RPM = Number(process.env["AVSA_RATE_LIMIT_RPM"] ?? 60);

// The limiter's sliding-window length (rate_limit.py hardcodes 60s). The
// self-heal test waits just past this for a burst to age out of the window.
const WINDOW_S = 60;

/**
 * A unique private-range IP per call. The limiter's window is in-memory and
 * persists for 60s, so reusing an IP across runs within a minute would start
 * from a polluted count. A fresh random IP per test gives each a clean bucket.
 */
function uniqueIp(): string {
  const octet = (): number => Math.floor(Math.random() * 254) + 1;
  return `10.${octet()}.${octet()}.${octet()}`;
}

/**
 * Fire one POST /chat as `ip`. The bad-MIME image passes the limiter then 415s
 * (no embed/LLM), so this is fast regardless of stack mode. Returns the status
 * and any Retry-After header.
 */
async function hitChat(
  request: APIRequestContext,
  ip: string,
): Promise<{ status: number; retryAfter: string | undefined }> {
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
  return { status: response.status(), retryAfter: response.headers()["retry-after"] };
}

test.describe("Rate limiter (apps/api middleware/rate_limit.py)", () => {
  // 60+ sequential requests per test; give them headroom over the 30s default.
  test.setTimeout(90_000);

  test(`allows ${RPM} requests from one IP, then rejects the next with 429 + Retry-After`, async ({
    request,
  }) => {
    const ip = uniqueIp();

    // The first RPM requests must clear the limiter. Each 415s on the bad MIME —
    // the assertion is only that none is rate-limited yet.
    for (let i = 0; i < RPM; i++) {
      const { status } = await hitChat(request, ip);
      expect(
        status,
        `request ${i + 1}/${RPM} from ${ip} should not be rate-limited yet (got ${status})`,
      ).not.toBe(429);
    }

    // The (RPM+1)th request inside the same 60s window must be rejected, and the
    // 429 must carry Retry-After so a client knows when to retry.
    const { status, retryAfter } = await hitChat(request, ip);
    expect(status, `request ${RPM + 1} from ${ip} must be rate-limited`).toBe(429);
    expect(retryAfter, "429 must advertise Retry-After").toBe("60");
  });

  test("rate-limit buckets are isolated per X-Forwarded-For", async ({ request }) => {
    const ipA = uniqueIp();
    const ipB = uniqueIp();

    // Exhaust ipA's window.
    for (let i = 0; i < RPM; i++) {
      await hitChat(request, ipA);
    }
    expect((await hitChat(request, ipA)).status, "ipA's window should be exhausted").toBe(429);

    // ipB has its own bucket — a single request must NOT be rate-limited. This
    // proves the limit is per-IP, not global.
    expect(
      (await hitChat(request, ipB)).status,
      "ipB must have an independent bucket, unaffected by ipA",
    ).not.toBe(429);
  });

  test(`single IP: spammed to 429, then self-heals after the ${WINDOW_S}s window slides`, async ({
    request,
  }) => {
    // Drives the REAL sliding window against the wall clock, so it deliberately
    // waits ~${WINDOW_S}s. (The unit test mocks monotonic() to avoid the sleep;
    // this is the live-stack counterpart proving recovery with no intervention.)
    test.setTimeout((WINDOW_S + 30) * 1000);
    const ip = uniqueIp();

    // Spam past the limit → tripped.
    for (let i = 0; i < RPM; i++) await hitChat(request, ip);
    expect((await hitChat(request, ip)).status, "window full → rejected").toBe(429);

    // Wait just past the window so the burst's timestamps age out — no manual
    // reset, no restart: the limiter heals itself as the window slides.
    await new Promise((resolve) => setTimeout(resolve, (WINDOW_S + 2) * 1000));

    // Same IP, no intervention → served again.
    expect(
      (await hitChat(request, ip)).status,
      `after the ${WINDOW_S}s window slides, the IP must be served again`,
    ).not.toBe(429);
  });

  test("slow-drip across many IPs is isolated and served, and the limiter survives the fleet", async ({
    request,
  }) => {
    test.setTimeout(60_000);
    const FLEET = 100; // distinct attacker IPs
    const PER_IP = 3; // well under RPM — the low per-IP rate that evades the limit
    const fleetIp = (i: number): string =>
      `172.16.${Math.floor(i / 254)}.${(i % 254) + 1}`;

    // A distributed low-rate pattern: each IP stays under RPM, so by design the
    // per-IP limiter does NOT throttle it. Asserting none is 429 proves the limit
    // is strictly per-IP (no cross-IP aggregation) and that the server stays
    // healthy tracking a fleet of distinct IPs. Each wave fires concurrently.
    for (let wave = 0; wave < PER_IP; wave++) {
      const results = await Promise.all(
        Array.from({ length: FLEET }, (_, i) => hitChat(request, fleetIp(i))),
      );
      results.forEach(({ status }, i) =>
        expect(
          status,
          `drip IP ${fleetIp(i)} (wave ${wave + 1}) must not be throttled`,
        ).not.toBe(429),
      );
    }

    // The fleet did not break or disable enforcement: a genuinely abusive single
    // IP is still caught. (The memory-bound defence against an *unbounded* IP
    // fleet — _MAX_TRACKED_IPS LRU eviction at 100k — is unit-tested, since 100k
    // distinct IPs is infeasible to drive over real HTTP.)
    const abuser = uniqueIp();
    for (let i = 0; i < RPM; i++) await hitChat(request, abuser);
    expect(
      (await hitChat(request, abuser)).status,
      "limiter must still enforce per-IP after handling the drip fleet",
    ).toBe(429);
  });
});
