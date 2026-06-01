#!/usr/bin/env bash
# smoke-prod.sh — Production deployment smoke gate
#
# Validates the key API surface of the AVSA prod deployment.
# Uses curl only — no Python or jq required.
#
# CHECKS:
#   1. API /health returns HTTP 200
#   2. Batcher /health returns HTTP 200 (via in-cluster URL or AVSA_PROD_BATCHER_URL)
#   3. POST /embed with a 1px JPEG returns HTTP 200 with an "embedding" field
#   4. POST /chat (SSE) with a 1px JPEG returns at least one product_card event
#   5. POST /mcp (JSON-RPC find_similar) returns HTTP 200 with at least one result
#
# USAGE:
#   export AVSA_PROD_API_URL=https://api.prod.avsa.example.com
#   bash scripts/smoke-prod.sh
#
# ENVIRONMENT:
#   AVSA_PROD_API_URL       REQUIRED — base URL of the prod API gateway (no trailing slash)
#   AVSA_PROD_BATCHER_URL   OPTIONAL — direct batcher URL (default: ${AVSA_PROD_API_URL}/_batcher)
#                           In-cluster use: http://batcher-service:8001
#   AVSA_SMOKE_TIMEOUT      OPTIONAL — curl request timeout in seconds (default: 30)
#   AVSA_SMOKE_MCP_API_KEY  OPTIONAL — value of AVSA_MCP_API_KEY from the cluster secret.
#                           When set, CHECK 5 is a hard gate. When absent, CHECK 5 is advisory.
#
# EXIT CODES:
#   0 — all checks passed
#   1 — first failing check (message printed to stderr with detail)

set -euo pipefail

# ─── Configuration ─────────────────────────────────────────────────────────────
if [[ -z "${AVSA_PROD_API_URL:-}" ]]; then
    echo "ERROR: AVSA_PROD_API_URL is not set." >&2
    echo "       Export the base URL of the prod API gateway, e.g.:" >&2
    echo "         export AVSA_PROD_API_URL=https://api.prod.avsa.example.com" >&2
    exit 1
fi

API_URL="${AVSA_PROD_API_URL%/}"   # strip trailing slash
BATCHER_URL="${AVSA_PROD_BATCHER_URL:-${API_URL}/_batcher}"
TIMEOUT="${AVSA_SMOKE_TIMEOUT:-30}"
MCP_API_KEY="${AVSA_SMOKE_MCP_API_KEY:-}"

# Real 224x224 RGB product JPEG (committed fixture). A 1x1 synthetic JPEG passes
# the upload guard but the REAL ViT rejects it (its image processor needs a
# usable 3-channel image), so /chat and /embed must upload a real image to
# exercise the embedding path. Resolved relative to this script so a prod-CI
# checkout finds it regardless of the working directory.
SMOKE_IMAGE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/tests/fixtures/product-photo.jpg"

FAILED=0

fail() {
    local check="$1"
    local msg="$2"
    echo "" >&2
    echo "FAIL [${check}]: ${msg}" >&2
    FAILED=1
}

pass() {
    local check="$1"
    local detail="$2"
    echo "OK   [${check}]: ${detail}"
}

# ─── CHECK 1: API /health ───────────────────────────────────────────────────────
echo "==> smoke-prod: AVSA production smoke gate"
echo "    API_URL:     ${API_URL}"
echo "    BATCHER_URL: ${BATCHER_URL}"
echo "    TIMEOUT:     ${TIMEOUT}s"
echo "    MCP_KEY:     ${MCP_API_KEY:+(set)}"
echo ""

echo "── CHECK 1: API /api/health ───────────────────────────────────────────────"
# The public entry point is the Next.js shopper service (LoadBalancer). It
# proxies GET /api/health → api-service/health. Direct /health is not exposed.
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time "${TIMEOUT}" \
    "${API_URL}/api/health" 2>/dev/null || echo "000")

if [[ "${HTTP_STATUS}" == "200" ]]; then
    pass "api-health" "GET ${API_URL}/api/health → HTTP ${HTTP_STATUS}"
else
    fail "api-health" "GET ${API_URL}/api/health → HTTP ${HTTP_STATUS} (expected 200)"
    echo "FAIL: API health check failed. Aborting remaining checks." >&2
    exit 1
fi

# ─── CHECK 2: Batcher /health (advisory) ──────────────────────────────────────
# The batcher is a ClusterIP-only service (port 8001) and is not reachable from
# outside the cluster unless AVSA_PROD_BATCHER_URL is explicitly set to an
# accessible address (e.g. via kubectl port-forward). When unreachable this
# check warns rather than failing the gate.
echo ""
echo "── CHECK 2: Batcher /health (advisory) ───────────────────────────────────"
BATCHER_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time "${TIMEOUT}" \
    "${BATCHER_URL}/health" 2>/dev/null || echo "000")

if [[ "${BATCHER_STATUS}" == "200" ]]; then
    pass "batcher-health" "GET ${BATCHER_URL}/health → HTTP ${BATCHER_STATUS}"
else
    echo "WARN [batcher-health]: GET ${BATCHER_URL}/health → HTTP ${BATCHER_STATUS} (advisory — verify via kubectl exec or port-forward)"
fi

# ─── CHECK 3: POST /embed (advisory) ──────────────────────────────────────────
# The /embed endpoint is not yet implemented in avsa_api (it's handled by the
# batcher + model pipeline internally). This check is advisory until the route
# is added; failures do not block the gate.
echo ""
echo "── CHECK 3: POST /embed (advisory) ───────────────────────────────────────"

EMBED_HTTP=""
EMBED_BODY=""
EMBED_HTTP=$(curl -s -o /tmp/smoke-prod-embed-body.txt -w "%{http_code}" \
    --max-time "${TIMEOUT}" \
    -F "image=@${SMOKE_IMAGE};type=image/jpeg" \
    "${API_URL}/embed" 2>/dev/null || echo "000")
EMBED_BODY=$(cat /tmp/smoke-prod-embed-body.txt 2>/dev/null || echo "")
rm -f /tmp/smoke-prod-embed-body.txt

if [[ "${EMBED_HTTP}" != "200" ]]; then
    echo "WARN [embed]: POST ${API_URL}/embed → HTTP ${EMBED_HTTP} (advisory — endpoint not yet externally exposed)"
else
    if echo "${EMBED_BODY}" | grep -q '"embedding"'; then
        pass "embed" "POST ${API_URL}/embed → HTTP 200, response contains \"embedding\" field"
    else
        echo "WARN [embed]: POST ${API_URL}/embed → HTTP 200 but response missing \"embedding\" field (advisory)"
    fi
fi

# ─── CHECK 4: POST /chat (SSE) with a real product JPEG ───────────────────────
echo ""
echo "── CHECK 4: POST /chat (SSE) with a real product JPEG ────────────────────"

CHAT_EXIT=0
CHAT_BODY=$(curl -s --max-time "${TIMEOUT}" \
    -F "image=@${SMOKE_IMAGE};type=image/jpeg" \
    -F "text=show me something similar" \
    "${API_URL}/chat" 2>/dev/null) || CHAT_EXIT=$?

# Exit 18 = CURLE_PARTIAL_FILE: SSE stream closed cleanly (known curl/SSE behaviour)
if [[ "${CHAT_EXIT}" -ne 0 && "${CHAT_EXIT}" -ne 18 ]]; then
    fail "chat-sse" "POST ${API_URL}/chat → curl exit ${CHAT_EXIT} (connection failed)"
else
    # Check for at least one product_card event in the SSE stream
    if echo "${CHAT_BODY}" | grep -q 'product_card'; then
        pass "chat-sse" "POST ${API_URL}/chat → SSE stream contains at least one product_card event"
    else
        fail "chat-sse" "POST ${API_URL}/chat → SSE stream contains no product_card event; body (first 500 chars): ${CHAT_BODY:0:500}"
    fi
fi

# ─── CHECK 5: POST /mcp (JSON-RPC find_similar) ──────────────────────────────
# JSON-RPC 2.0 tools/call, proxied through the shopper LoadBalancer at /mcp →
# the conformant AVSA.MCP.Server. find_similar is image-native but
# modality-aware: with no image it takes a free-text query (512-d CLIP), which
# is what we use here so the smoke needs no image file.
# Auth is Bearer (Authorization: Bearer <AVSA_MCP_API_KEY>); the JSON-RPC server
# compares it in constant time. When AVSA_SMOKE_MCP_API_KEY is set this is a hard
# gate; otherwise advisory.
echo ""
if [[ -n "${MCP_API_KEY}" ]]; then
    echo "── CHECK 5: POST /mcp (JSON-RPC find_similar) ────────────────────────────"
else
    echo "── CHECK 5: POST /mcp (JSON-RPC find_similar) (advisory — no MCP key) ────"
fi

# JSON-RPC 2.0 envelope: tools/call → find_similar with a free-text query.
MCP_BODY=$(python3 -c "
import json
body = {
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'tools/call',
    'params': {
        'name': 'find_similar',
        'arguments': {
            'text': 'a blue casual dress for everyday wear',
            'limit': 3,
        },
    },
}
print(json.dumps(body))
")
MCP_HTTP=""
MCP_RESPONSE=""
MCP_HTTP=$(curl -s -o /tmp/smoke-prod-mcp-body.txt -w "%{http_code}" \
    --max-time "${TIMEOUT}" \
    -X POST \
    -H "Content-Type: application/json" \
    ${MCP_API_KEY:+-H "Authorization: Bearer ${MCP_API_KEY}"} \
    -d "${MCP_BODY}" \
    "${API_URL}/mcp" 2>/dev/null || echo "000")
MCP_RESPONSE=$(cat /tmp/smoke-prod-mcp-body.txt 2>/dev/null || echo "")
rm -f /tmp/smoke-prod-mcp-body.txt

# The JSON-RPC server returns HTTP 200 for both results AND errors (the error is
# in the body), so a pass requires HTTP 200, a "results" payload, and no JSON-RPC
# "error" envelope.
if [[ "${MCP_HTTP}" == "200" ]] && ! echo "${MCP_RESPONSE}" | grep -q '"error"'; then
    if echo "${MCP_RESPONSE}" | grep -qE '"results"|product_id|image_url'; then
        pass "mcp-find-similar" "POST ${API_URL}/mcp (JSON-RPC find_similar) → HTTP 200, response contains results"
    else
        if [[ -n "${MCP_API_KEY}" ]]; then
            fail "mcp-find-similar" "POST ${API_URL}/mcp → HTTP 200 but no results in response; body: ${MCP_RESPONSE:0:300}"
        else
            echo "WARN [mcp-find-similar]: POST ${API_URL}/mcp → HTTP 200 but no results in response (advisory)"
        fi
    fi
else
    if [[ -n "${MCP_API_KEY}" ]]; then
        fail "mcp-find-similar" "POST ${API_URL}/mcp → HTTP ${MCP_HTTP}$(echo "${MCP_RESPONSE}" | grep -q '"error"' && echo ' with JSON-RPC error') (expected 200 + results); body: ${MCP_RESPONSE:0:300}"
    else
        echo "WARN [mcp-find-similar]: POST ${API_URL}/mcp → HTTP ${MCP_HTTP} (advisory — set AVSA_SMOKE_MCP_API_KEY to make this a hard gate)"
    fi
fi

# ─── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "── Summary ───────────────────────────────────────────────────────────────"
if [[ "${FAILED}" -eq 0 ]]; then
    echo "PASS: all production smoke checks passed."
    exit 0
else
    echo "FAIL: one or more smoke checks FAILED (see FAIL lines above)." >&2
    echo "" >&2
    echo "  Troubleshooting:" >&2
    echo "    [ ] Verify the deployment completed: kubectl rollout status deploy -n avsa" >&2
    echo "    [ ] Check pod logs:  kubectl logs -n avsa -l app=api-service --tail=50" >&2
    echo "    [ ] Check batcher:   kubectl logs -n avsa -l app=batcher-service --tail=50" >&2
    echo "    [ ] Verify secrets:  kubectl get secret avsa-env -n avsa" >&2
    echo "    [ ] Check ingress:   kubectl describe ingress -n avsa" >&2
    exit 1
fi
