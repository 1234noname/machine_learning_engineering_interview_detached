import type { APIRequestContext } from "@playwright/test";

/**
 * Poll a Prometheus endpoint until the named metric (with optional labels)
 * exceeds `baseline`, or until `timeoutMs` elapses.
 *
 * Throws if the value does not increase in time. Use `?? 0` on the baseline
 * so "not yet present" (null) is treated as zero.
 */
export async function waitForMetricIncrease(
  request: APIRequestContext,
  url: string,
  name: string,
  baseline: number,
  labels?: Record<string, string>,
  timeoutMs = 5_000,
): Promise<number> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = (await scrapeMetricValue(request, url, name, labels)) ?? 0;
    if (value > baseline) return value;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `waitForMetricIncrease: ${name}${labels ? JSON.stringify(labels) : ""} did not exceed ${baseline} within ${timeoutMs}ms`,
  );
}

/**
 * Scrape a Prometheus metrics endpoint and return the numeric value of a
 * specific metric (optionally filtered by label set).
 *
 * Returns null when the endpoint is unreachable or the metric is not present,
 * so callers can use `?? 0` to treat "not present" as zero.
 */
export async function scrapeMetricValue(
  request: APIRequestContext,
  url: string,
  metricName: string,
  labels?: Record<string, string>,
): Promise<number | null> {
  let text: string;
  try {
    const response = await request.get(url);
    if (!response.ok()) return null;
    text = await response.text();
  } catch {
    return null;
  }
  return parsePrometheusValue(text, metricName, labels);
}

/**
 * Parse a single numeric value from a Prometheus text-format scrape body.
 *
 * Matches lines where the metric name prefix and (optionally) the label set
 * match. Returns the value of the first matching line, or null if none found.
 *
 * Exported for unit-level testing of the parser without a live endpoint.
 */
export function parsePrometheusValue(
  text: string,
  metricName: string,
  labels?: Record<string, string>,
): number | null {
  for (const line of text.split("\n")) {
    if (line.startsWith("#") || !line.trim()) continue;

    const spaceIdx = line.lastIndexOf(" ");
    if (spaceIdx === -1) continue;

    const nameAndLabels = line.slice(0, spaceIdx);
    // Strip optional timestamp from the value field
    const valueStr = line.slice(spaceIdx + 1).split(" ")[0] ?? "";

    if (!nameAndLabels.startsWith(metricName)) continue;

    if (!labels) return parseFloat(valueStr);

    const labelStr = nameAndLabels.match(/\{(.+)\}/)?.[1] ?? "";
    const allMatch = Object.entries(labels).every(([k, v]) =>
      labelStr.includes(`${k}="${v}"`),
    );
    if (allMatch) return parseFloat(valueStr);
  }
  return null;
}
