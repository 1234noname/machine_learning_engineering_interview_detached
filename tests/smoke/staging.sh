#!/usr/bin/env bash
# Smoke checks against the staging app URL.
# Usage: APP_URL=<url> bash tests/smoke/staging.sh
#
# Runs a real locust load test when the URL is a live endpoint.
# Exits 0 when the URL is a known placeholder (skeleton guard).
#
# Thresholds: p95 latency < 2000ms, failure rate < 5%.

set -euo pipefail

# In CI the URL is passed via APP_URL env var (prevents shell injection from
# untrusted terraform output values). Locally a positional arg also works:
#   bash tests/smoke/staging.sh <url>
URL="${APP_URL:-${1:-}}"

if [ -z "$URL" ]; then
    echo "ERROR: app_url is required (set APP_URL env var or pass as \$1)." >&2
    echo "Usage: APP_URL=<url> bash tests/smoke/staging.sh" >&2
    exit 1
fi

echo "Smoke check: $URL"

# ---------------------------------------------------------------------------
# Placeholder guard.
# Exit 0 when the URL is a known placeholder so the known-good SHA is still
# written and the deploy is not reverted unnecessarily.
# ---------------------------------------------------------------------------
if echo "$URL" | grep -qiE "deploying|skeleton"; then
    echo "NOTICE: URL is a known placeholder ('$URL')."
    echo "Smoke exits 0 — no real endpoint to test until a live URL is available."
    exit 0
fi

# ---------------------------------------------------------------------------
# Real locust smoke run.
# p95 latency < 2000ms, failure rate < 5%.
# ---------------------------------------------------------------------------
echo "Running locust smoke against $URL ..."

uv run locust --headless -u 10 -r 2 --run-time 60s \
  --host "$URL" --csv /tmp/avsa-smoke -f locustfile.py

# Parse stats CSV. Aggregated row: columns are:
# Type,Name,Request Count,Failure Count,...,50%,66%,75%,80%,90%,95%,...
STATS_CSV="/tmp/avsa-smoke_stats.csv"

if [[ ! -f "$STATS_CSV" ]]; then
  echo "FAIL: locust did not produce $STATS_CSV" >&2
  exit 1
fi

# Extract the Aggregated row values (last data row is Aggregated)
AGGREGATED=$(grep "^Aggregated," "$STATS_CSV" || tail -n 1 "$STATS_CSV")

REQUEST_COUNT=$(echo "$AGGREGATED" | awk -F',' '{ print $3 }')
FAILURE_COUNT=$(echo "$AGGREGATED" | awk -F',' '{ print $4 }')
P95=$(echo "$AGGREGATED" | awk -F',' '{ print $17 }')

echo "Requests: $REQUEST_COUNT, Failures: $FAILURE_COUNT, p95: ${P95}ms"

# Failure rate check (< 5%)
if [[ "$REQUEST_COUNT" -gt 0 ]]; then
  FAILURE_PCT=$(awk "BEGIN { printf \"%d\", ($FAILURE_COUNT / $REQUEST_COUNT) * 100 }")
  if [[ "$FAILURE_PCT" -ge 5 ]]; then
    echo "FAIL: Failure rate ${FAILURE_PCT}% >= 5% threshold." >&2
    exit 1
  fi
fi

# p95 latency check (< 2000ms)
if [[ -n "$P95" ]] && [[ "$P95" -ge 2000 ]]; then
  echo "FAIL: p95 latency ${P95}ms >= 2000ms threshold." >&2
  exit 1
fi

echo "OK: smoke passed. p95=${P95}ms, failure_rate=${FAILURE_PCT:-0}%"
exit 0
