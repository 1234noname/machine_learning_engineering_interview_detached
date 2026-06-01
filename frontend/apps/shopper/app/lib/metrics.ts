/**
 * Shared prom-client registry for the Next.js shopper server process.
 *
 * A single Registry instance is re-used across all route handlers so counters
 * and histograms accumulate across requests rather than resetting per-import.
 * Next.js hot-reloads the module in dev mode, so the registry is attached to
 * `globalThis` to survive module re-evaluation without double-registering metrics.
 */
import { Registry, Counter, Histogram, collectDefaultMetrics } from "prom-client";

declare global {
  // eslint-disable-next-line no-var
  var __avsa_metrics_registry: Registry | undefined;
}

function buildRegistry(): Registry {
  const registry = new Registry();

  collectDefaultMetrics({ register: registry, prefix: "avsa_shopper_node_" });

  new Counter({
    name: "avsa_shopper_chat_requests_total",
    help: "Total /chat proxy requests by outcome (ok | upstream_error | bad_request)",
    labelNames: ["outcome"] as const,
    registers: [registry],
  });

  new Histogram({
    name: "avsa_shopper_chat_duration_seconds",
    help: "Time from the Next.js proxy receiving the POST /chat request to streaming the first SSE byte back to the browser",
    labelNames: [] as const,
    buckets: [0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0],
    registers: [registry],
  });

  return registry;
}

export const registry: Registry =
  globalThis.__avsa_metrics_registry ?? (globalThis.__avsa_metrics_registry = buildRegistry());

export function chatRequestsTotal(outcome: string): void {
  (registry.getSingleMetric("avsa_shopper_chat_requests_total") as Counter)
    ?.labels(outcome)
    .inc();
}

export function observeChatDuration(seconds: number): void {
  (registry.getSingleMetric("avsa_shopper_chat_duration_seconds") as Histogram)
    ?.observe(seconds);
}
