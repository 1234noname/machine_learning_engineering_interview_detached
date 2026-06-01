#!/usr/bin/env bash
#  QPS benchmark — system QPS and in-memory model GPU ceiling.
#
# Wraps scripts/bench-qps.py with the [bench.prod] / [bench.model] config
# profile. Defaults to the local stack; set AVSA_PROD_BATCHER_URL for prod.
#
# Optional env:
#   AVSA_PROD_BATCHER_URL   — batcher URL (default: http://localhost:8081)
#   AVSA_PROD_MODEL_URL     — model URL   (default: http://localhost:8090)
#   AVSA_BENCH_PROD_TARGET  — 'batcher' (default) or 'model'
#   AVSA_BENCH_PROD_OUTDIR  — override output dir (default: evals/qps/baseline)
#   DATABASE_URL            — Postgres URL; when set with --target=model,
#                             recall@5 is measured after the QPS sweep.
#
# Config sections used:
#   --target batcher  ->  [bench.prod]   (Locust concurrency ramp, full system path)
#   --target model    ->  [bench.model]  (in-memory probe via Modal SDK, GPU ceiling)
#
# Output: evals/qps/baseline/prod-frontier.json (or AVSA_BENCH_PROD_OUTDIR)
#
# Usage:
#   just bench-qps                                            # local system QPS
#   AVSA_PROD_BATCHER_URL=http://34.x.x.x:80 just bench-qps  # prod system QPS
#   AVSA_BENCH_PROD_TARGET=model just bench-qps               # local GPU ceiling
#   AVSA_BENCH_PROD_TARGET=model AVSA_PROD_MODEL_URL=https://... just bench-qps  # prod GPU ceiling

set -euo pipefail

# Require an explicit URL — fail fast rather than silently benchmarking the
# wrong target. The Justfile bench-qps recipe sets a localhost default for
# local runs; call this script directly only when you know where to point it.
: "${AVSA_PROD_BATCHER_URL:?AVSA_PROD_BATCHER_URL must be set (e.g. http://localhost:8081 for local, http://34.x.x.x:80 for prod)}"

root=$(git rev-parse --show-toplevel)

BATCHER_URL="${AVSA_PROD_BATCHER_URL:-http://localhost:8081}"
TARGET="${AVSA_BENCH_PROD_TARGET:-batcher}"
MODEL_URL="${AVSA_PROD_MODEL_URL:-http://localhost:8090}"
OUTPUT_DIR="${AVSA_BENCH_PROD_OUTDIR:-evals/qps/baseline}"

# bench.model section drives the in-memory model probe (batch_sizes, n_passes);
# bench.prod drives the Locust batcher saturation sweep (concurrency_levels, etc.).
if [[ "${TARGET}" == "model" ]]; then
    BENCH_SECTION="model"
else
    BENCH_SECTION="prod"
fi

# Append --with-recall for model-direct runs when DATABASE_URL is set so
# recall@5 is measured in the same pass as model QPS.
RECALL_FLAG=""
if [[ "${TARGET}" == "model" && -n "${DATABASE_URL:-}" ]]; then
    RECALL_FLAG="--with-recall"
fi

echo "==> bench-qps: benchmark"
echo "    batcher URL:   ${BATCHER_URL}"
echo "    model URL:     ${MODEL_URL}"
echo "    target:        ${TARGET}"
echo "    bench section: [bench.${BENCH_SECTION}]"
echo "    recall@5:      ${RECALL_FLAG:-disabled (set DATABASE_URL to enable)}"
echo "    output dir:    ${OUTPUT_DIR}"
echo "    config:        ${root}/config/avsa.toml"
echo ""

# The in-memory model probe imports avsa_model.vit.VitEmbedder, which needs
# the [model] extra (torch + transformers + Pillow + torchvision). That extra
# lives in apps/model's uv env (see `uv sync --extra model --directory
# apps/model` in `just stack-up`), not the root env. Use that venv's
# interpreter directly rather than `uv run --directory apps/model` because
# the latter chdirs the process and breaks the repo-root-relative
# `attribute_heads_dir` in config/avsa.toml. Every other target goes through
# the root env via uv run as before.
if [[ "${TARGET}" == "model" ]]; then
    if [[ ! -x "${root}/apps/model/.venv/bin/python" ]]; then
        echo "ERROR: ${root}/apps/model/.venv/bin/python missing — run \`uv sync --extra model --directory apps/model\` first." >&2
        exit 1
    fi
    PY_CMD=("${root}/apps/model/.venv/bin/python")
else
    PY_CMD=(uv run python)
fi

"${PY_CMD[@]}" "${root}/scripts/bench-qps.py" \
    --locustfile    "${root}/locustfile.py" \
    --config        "${root}/config/avsa.toml" \
    --bench-section "${BENCH_SECTION}" \
    --target        "${TARGET}" \
    --batcher-url   "${BATCHER_URL}" \
    --model-url     "${MODEL_URL}" \
    --output-dir    "${OUTPUT_DIR}" \
    ${RECALL_FLAG}

echo ""
echo "==> bench-qps complete. Frontier written to ${OUTPUT_DIR}/prod-frontier.json"
