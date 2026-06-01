#!/usr/bin/env bash
# emit-deploy-event.sh — emit a structured deploy event to GCS JSONL and as a
# workflow artifact.
#
# All inputs via environment variables (never positional args — prevents
# shell injection when values contain spaces or special characters).
#
# Required env vars:
#   EVENT_TYPE    — one of: started, succeeded, failed, reverted
#   DEPLOY_ENV    — e.g. dev-pr-42, staging, prod
#   PR_NUMBER     — pull request number (may be empty for push-to-main flows)
#   COMMIT_SHA    — full or short commit SHA
#   ACTOR         — GitHub actor (github.actor)
#   RUN_ID        — GitHub Actions run ID (github.run_id)
#
# Optional env vars:
#   DURATION_MS   — elapsed milliseconds (required for terminal events:
#                   succeeded, failed, reverted); set by caller
#   ERROR_SUMMARY — short error description for failed/reverted events
#   METRICS_JSON  — JSON object with optional metrics for succeeded events
#   SKIP_GCS      — if set to "1", skip the GCS write (useful for local testing)
#   GCP_PROJECT_ID — GCP project ID (used when creating the events bucket)
#
# Exit codes:
#   0 — always; GCS failures are logged to stderr but do not fail the script.
#       Observability must not gate the deployment.
#
# Usage (in GitHub Actions):
#   env:
#     EVENT_TYPE: started
#     DEPLOY_ENV: staging
#     PR_NUMBER: ${{ github.event.number }}
#     COMMIT_SHA: ${{ github.sha }}
#     ACTOR: ${{ github.actor }}
#     RUN_ID: ${{ github.run_id }}
#   run: bash scripts/emit-deploy-event.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Validate required inputs
# ---------------------------------------------------------------------------
: "${EVENT_TYPE:?Required env var EVENT_TYPE is not set}"
: "${DEPLOY_ENV:?Required env var DEPLOY_ENV is not set}"
: "${COMMIT_SHA:?Required env var COMMIT_SHA is not set}"
: "${ACTOR:?Required env var ACTOR is not set}"
: "${RUN_ID:?Required env var RUN_ID is not set}"

# PR_NUMBER may be empty for push-triggered workflows (staging, prod); default to empty string.
PR_NUMBER="${PR_NUMBER:-}"

# Validate EVENT_TYPE is one of the allowed values.
case "$EVENT_TYPE" in
    started|succeeded|failed|reverted) ;;
    *) echo "::error::Invalid EVENT_TYPE '$EVENT_TYPE'. Must be: started, succeeded, failed, reverted" >&2; exit 1 ;;
esac

# ---------------------------------------------------------------------------
# Build the JSON payload
# ---------------------------------------------------------------------------
TIMESTAMP_MS=$(python3 -c "import time; print(int(time.time() * 1000))" 2>/dev/null \
    || node -e "console.log(Date.now())" 2>/dev/null \
    || echo "$(( $(date +%s) * 1000 ))")

# Start with required fields
JSON=$(jq -cn \
    --arg event "$EVENT_TYPE" \
    --arg env "$DEPLOY_ENV" \
    --arg pr_number "$PR_NUMBER" \
    --arg commit_sha "$COMMIT_SHA" \
    --arg actor "$ACTOR" \
    --argjson timestamp_ms "$TIMESTAMP_MS" \
    '{
        event: $event,
        env: $env,
        pr_number: $pr_number,
        commit_sha: $commit_sha,
        actor: $actor,
        timestamp_ms: $timestamp_ms
    }')

# Add duration_ms for terminal events (succeeded, failed, reverted)
case "$EVENT_TYPE" in
    succeeded|failed|reverted)
        DURATION_MS="${DURATION_MS:-0}"
        JSON=$(echo "$JSON" | jq --argjson duration_ms "$DURATION_MS" '. + {duration_ms: $duration_ms}')
        ;;
esac

# Add optional error_summary for failed/reverted events
if [[ -n "${ERROR_SUMMARY:-}" ]]; then
    JSON=$(echo "$JSON" | jq --arg error_summary "$ERROR_SUMMARY" '. + {error_summary: $error_summary}')
fi

# Add optional metrics for succeeded events
if [[ -n "${METRICS_JSON:-}" ]]; then
    # Validate METRICS_JSON is valid JSON before embedding
    if echo "$METRICS_JSON" | jq empty 2>/dev/null; then
        JSON=$(echo "$JSON" | jq --argjson metrics "$METRICS_JSON" '. + {metrics: $metrics}')
    else
        echo "::warning::METRICS_JSON is not valid JSON — omitting from event payload" >&2
    fi
fi

# Add pr_opened_at_ms for prod succeeded events (used for lead-time calculation)
if [[ "$EVENT_TYPE" == "succeeded" && "$DEPLOY_ENV" == "prod" && -n "$PR_NUMBER" ]]; then
    if command -v gh > /dev/null 2>&1; then
        PR_CREATED_AT=$(gh pr view "$PR_NUMBER" --json createdAt --jq '.createdAt' 2>/dev/null || echo "")
        if [[ -n "$PR_CREATED_AT" ]]; then
            # Convert ISO 8601 to epoch milliseconds
            PR_OPENED_AT_MS=$(date -d "$PR_CREATED_AT" +%s%3N 2>/dev/null \
                || PR_CREATED_AT="$PR_CREATED_AT" python3 -c "
import os
from datetime import datetime, timezone
ts = os.environ['PR_CREATED_AT'].rstrip('Z')
dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
print(int(dt.timestamp() * 1000))
" 2>/dev/null || echo "")
            if [[ -n "$PR_OPENED_AT_MS" ]] && [[ "$PR_OPENED_AT_MS" =~ ^[0-9]+$ ]]; then
                JSON=$(echo "$JSON" | jq --argjson pr_opened_at_ms "$PR_OPENED_AT_MS" '. + {pr_opened_at_ms: $pr_opened_at_ms}')
            fi
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Write to local temp file (also used as the workflow artifact)
# ---------------------------------------------------------------------------
DATE_PATH=$(date -u +%Y-%m-%d)
ARTIFACT_DIR="${RUNNER_TEMP:-/tmp}/deploy-events"
mkdir -p "$ARTIFACT_DIR"
ARTIFACT_FILE="$ARTIFACT_DIR/deploy-event-${DEPLOY_ENV}-${RUN_ID}.json"

printf '%s\n' "$JSON" > "$ARTIFACT_FILE"

# Output the JSON to stdout for tooling/debugging
printf '%s\n' "$JSON"

# ---------------------------------------------------------------------------
# Write to GCS (skipped when SKIP_GCS=1 — for local testing)
# ---------------------------------------------------------------------------
if [[ "${SKIP_GCS:-}" == "1" ]]; then
    echo "SKIP_GCS=1: skipping GCS write (test/local mode)" >&2
else
    GCS_BUCKET="avsa-prd-deploy-events"
    GCS_PATH="gs://${GCS_BUCKET}/${DEPLOY_ENV}/${DATE_PATH}/${RUN_ID}.jsonl"

    # The avsa-prd-deploy-events bucket is managed by Terraform (prod/shared/main.tf)
    # and must pre-exist before this script runs. The script writes only; it
    # does not create the bucket.
    if command -v gcloud > /dev/null 2>&1; then
        # Each run_id produces exactly one JSONL file; Track D glob-scans the prefix.
        printf '%s\n' "$JSON" | gcloud storage cp - "$GCS_PATH" >/dev/null 2>&1 || {
            echo "::warning::Failed to write deploy event to $GCS_PATH — event was not persisted to GCS (artifact upload proceeds normally)" >&2
        }
    else
        echo "::warning::gcloud CLI not found — skipping GCS write" >&2
    fi
fi

# Exit 0 always — observability must not gate deployment.
exit 0
