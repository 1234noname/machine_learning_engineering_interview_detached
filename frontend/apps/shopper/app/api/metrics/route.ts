/**
 * GET /api/metrics — Prometheus text exposition for the shopper frontend.
 *
 * Exposes avsa_shopper_* metrics (chat request count, chat duration histogram,
 * Node.js default process metrics). Scraped by the local-observability Prometheus
 * scrape job `avsa-shopper` on port 3000.
 */
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

import { registry } from "../../lib/metrics";

export async function GET(): Promise<Response> {
  const body = await registry.metrics();
  return new Response(body, {
    headers: { "content-type": registry.contentType },
  });
}
