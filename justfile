# AVSA — task runner.
# `just` with no arg lists targets. See README "Quick start" and
# docs/runbooks/setup.md "Bootstrap" for context.

set dotenv-load := true

# Default target — list available recipes.
default:
    @just --list

# Run the full polyglot test suite across all four runtimes.
#
# Python: two-phase pytest (phase 1 bulk suite; phase 2 loadtest in its own
# process because `import locust` monkey-patches ssl/socket via gevent and
# breaks anyio if both collect in the same run).
# Elixir: mix test in apps/orchestrator/.
# Rust:   cargo test in crates/batcher/.
# Node:   generate shared types (gitignored, derived from the OpenAPI spec),
#         then run each workspace package's `test` script. `--if-present`
#         skips packages without one (e.g. @avsa/shared), so a not-yet-
#         populated package doesn't break the recipe. (`pnpm test
#         --passWithNoTests` does NOT work: pnpm consumes the flag instead
#         of forwarding it to vitest, and the root `test` script is
#         `pnpm -r run test`, which fails on packages lacking a `test` script.)
#
# CI calls `just test-ci` instead; keep local and CI in sync unless a
# runtime-specific flag is truly needed (e.g. GPU skips).
test:
    uv run pytest -m "not slow"
    uv run pytest tests/test_loadtest.py tests/test_story021_workload.py --override-ini="addopts=--strict-markers -ra"
    cd apps/orchestrator && mix test
    cd crates/batcher && cargo test
    cd frontend && pnpm --filter @avsa/shared run generate && pnpm -r --if-present run test

# Bootstrap a fresh clone: install brew packages (macOS), Python, deps, and
# git hooks. Idempotent — re-running is safe and fast. Each named step
# prints `[<step>] elapsed: <N>s` so the cold-clone CI job can assert
# per-step + total budgets. Steps that require macOS-only tooling (Homebrew,
# gcloud CLI) skip cleanly on Linux, where CI installs `just` + `uv` via
# pinned setup actions before invoking this recipe.
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    total_start=$SECONDS

    if [[ "$(uname -s)" == "Darwin" ]]; then
        echo "==> Installing Homebrew packages from Brewfile..."
        step_start=$SECONDS
        brew bundle
        echo "[brew] elapsed: $((SECONDS - step_start))s"
    else
        echo "==> Skipping Brewfile (non-macOS host)"
        echo "[brew] elapsed: 0s"
    fi

    echo "==> Pulling Git LFS objects (Fashion200k + embedding artifact + attribute heads)..."
    step_start=$SECONDS
    # --skip-repo: install the LFS filter config (smudge/clean) into the local
    # repo without writing hooks. The project's pre-push hook lives in
    # .githooks/pre-push (via core.hooksPath) and forwards to `git lfs pre-push`
    # itself — see that hook's header. Idempotent: safe to re-run.
    git lfs install --local --skip-repo
    git lfs pull
    echo "[git-lfs] elapsed: $((SECONDS - step_start))s"

    echo "==> Installing pinned Python (from .python-version)..."
    step_start=$SECONDS
    uv python install
    echo "[python] elapsed: $((SECONDS - step_start))s"

    echo "==> Syncing Python dev dependencies..."
    step_start=$SECONDS
    # Recreate the venv if its interpreter has been replaced (brew Python upgrade,
    # uv-managed Python relocation, etc.). `uv sync` does not self-heal a venv
    # whose `bin/python3` can no longer locate its libpython dylib, so we check
    # first and remove the venv if it can't run — keeping setup idempotent
    # across the most common machine-state churn.
    if [[ -d .venv ]] && ! .venv/bin/python3 --version >/dev/null 2>&1; then
        echo "==> .venv references a missing Python interpreter; recreating..."
        rm -rf .venv
    fi
    uv sync
    echo "[uv-sync] elapsed: $((SECONDS - step_start))s"

    if command -v gcloud >/dev/null 2>&1; then
        echo "==> Installing gke-gcloud-auth-plugin (gcloud component)..."
        step_start=$SECONDS
        # Non-fatal: the plugin is only needed for kubectl-against-GKE (deploy,
        # Phase 3). On CI runners where `gcloud components install` is disabled
        # (managed/snap installs), don't fail the whole bootstrap over it.
        gcloud components install gke-gcloud-auth-plugin --quiet \
            || echo "==> gke-gcloud-auth-plugin install skipped (non-fatal; needed only for GKE deploy)"
        echo "[gcloud] elapsed: $((SECONDS - step_start))s"
    else
        echo "==> Skipping gcloud components (gcloud CLI not installed)"
        echo "[gcloud] elapsed: 0s"
    fi

    echo "==> Installing protoc-gen-elixir escript (Elixir proto codegen)..."
    step_start=$SECONDS
    mix escript.install hex protobuf --force
    echo "[protoc-gen-elixir] elapsed: $((SECONDS - step_start))s"

    echo "==> Fetching Elixir dependencies (apps/orchestrator/)..."
    step_start=$SECONDS
    cd apps/orchestrator && mix deps.get && cd -
    echo "[elixir] elapsed: $((SECONDS - step_start))s"

    echo "==> Prefetching Rust dependencies (crates/)..."
    step_start=$SECONDS
    cargo fetch --manifest-path crates/batcher/Cargo.toml
    echo "[rust] elapsed: $((SECONDS - step_start))s"

    echo "==> Ensuring pnpm matches frontend/package.json packageManager pin..."
    step_start=$SECONDS
    # Read the pinned version (e.g. "pnpm@9.15.9" -> "9.15.9") and install it
    # globally via npm if the current pnpm doesn't match. Idempotent: if the
    # pinned version is already on PATH this is a no-op. See Brewfile note for
    # why pnpm is npm-owned, not brew-owned.
    pinned_pnpm=$(python3 -c "import json; print(json.load(open('frontend/package.json'))['packageManager'].split('@',1)[1])")
    if ! command -v pnpm >/dev/null 2>&1 || [[ "$(pnpm --version 2>/dev/null)" != "$pinned_pnpm" ]]; then
        echo "==> Installing pnpm@${pinned_pnpm} via npm..."
        npm install -g "pnpm@${pinned_pnpm}"
    else
        echo "==> pnpm ${pinned_pnpm} already installed"
    fi
    echo "[pnpm] elapsed: $((SECONDS - step_start))s"

    echo "==> Installing Node dependencies (frontend/)..."
    step_start=$SECONDS
    cd frontend && pnpm install --frozen-lockfile && cd -
    echo "[node] elapsed: $((SECONDS - step_start))s"

    echo "==> Configuring git core.hooksPath -> .githooks/..."
    step_start=$SECONDS
    git config core.hooksPath .githooks
    echo "[hooks] elapsed: $((SECONDS - step_start))s"

    echo "==> Setup complete. total elapsed: $((SECONDS - total_start))s"

# Re-point core.hooksPath at .githooks/ (assumes toolchain is already installed).
# Used after a fresh clone or after upstream changes to .githooks/.
install-hooks:
    git config core.hooksPath .githooks
    @echo "==> core.hooksPath set to .githooks/."

# Forward the AVSA Postgres container port from the Colima VM to localhost.
# No-op when the port is already reachable, or when not using Colima as the
# Docker runtime (Docker Desktop proxies container ports automatically).
# Called automatically by db-migrate and db-reset; also safe to call directly.
# Why this exists: Colima's Lima VM does not proxy container-published ports to
# the macOS host automatically — an SSH tunnel is required. Docker Desktop and
# Linux CI runners do not have this limitation.
db-tunnel:
    #!/usr/bin/env bash
    set -euo pipefail
    port=5434
    if nc -z 127.0.0.1 "$port" 2>/dev/null; then
        echo "==> port $port already reachable (tunnel up or runtime proxies automatically)"
        exit 0
    fi
    if [[ "$(docker context show 2>/dev/null || echo default)" != "colima" ]]; then
        echo "==> non-Colima Docker runtime; port forwarding is automatic"
        exit 0
    fi
    echo "==> Colima runtime — opening SSH port-forward localhost:${port} -> container postgres"
    ssh_config=$(mktemp /tmp/avsa-colima-ssh.XXXXXX)
    colima ssh-config > "$ssh_config"
    ssh -F "$ssh_config" colima -N -L "${port}:localhost:${port}" &
    ssh_pid=$!
    disown "$ssh_pid"
    for _ in $(seq 1 10); do
        nc -z 127.0.0.1 "$port" 2>/dev/null && break
        sleep 0.5
    done
    rm -f "$ssh_config"
    if ! nc -z 127.0.0.1 "$port" 2>/dev/null; then
        echo "==> SSH tunnel failed to open on localhost:${port}" >&2
        kill "$ssh_pid" 2>/dev/null || true
        exit 1
    fi
    echo "==> tunnel open (PID ${ssh_pid})"

# Start the local Postgres 16 + pgvector container (if not already running)
# and apply all pending migrations. Idempotent — re-running applies only
# migrations not yet recorded in public.schema_migrations. DATABASE_URL
# defaults to the local container per config/avsa.toml [db].url; override it
# to migrate a different database. Requires psql on PATH (Brewfile: libpq).
db-migrate:
    #!/usr/bin/env bash
    set -euo pipefail
    : "${DATABASE_URL:=postgresql://avsa:avsa@localhost:5434/avsa}"
    export DATABASE_URL
    just db-tunnel
    docker compose -f infra/local-db/docker-compose.yml up -d --wait
    bash infra/migrations/migrate.sh

# DEV/TEST ONLY. Drop and recreate the database from scratch, then re-run all
# migrations. Guarded by AVSA_ALLOW_RESET=1 so it can't wipe data by accident;
# never point DATABASE_URL at a non-local database when running this.
db-reset:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "${AVSA_ALLOW_RESET:-0}" != "1" ]]; then
        echo "db-reset refused: set AVSA_ALLOW_RESET=1 to confirm (dev/test only)." >&2
        exit 1
    fi
    : "${DATABASE_URL:=postgresql://avsa:avsa@localhost:5434/avsa}"
    export DATABASE_URL
    just db-tunnel
    docker compose -f infra/local-db/docker-compose.yml up -d --wait
    # Drop/recreate from the maintenance DB — you can't drop the DB you're
    # connected to. Derive the admin URL and target db name from DATABASE_URL
    # so nothing is hard-coded.
    admin_url="${DATABASE_URL%/*}/postgres"
    dbname="${DATABASE_URL##*/}"
    psql "$admin_url" -v ON_ERROR_STOP=1 \
        -c "DROP DATABASE IF EXISTS \"${dbname}\" WITH (FORCE);" \
        -c "CREATE DATABASE \"${dbname}\";"
    bash infra/migrations/migrate.sh

# Run the integration test suite against the local Postgres container.
# Starts the container, opens the Colima tunnel if needed, applies
# migrations, then runs tests/integration/ with DATABASE_URL set.
# For the full unit + integration suite run `just test && just test-integration`.
test-integration:
    #!/usr/bin/env bash
    set -euo pipefail
    : "${DATABASE_URL:=postgresql://avsa:avsa@localhost:5434/avsa}"
    export DATABASE_URL
    just db-migrate
    uv run pytest tests/integration/ -v

# Populate catalog.products with the full ≥10k row seed defined in
# config/avsa.toml. Hits the real ViT /embed endpoint (AVSA_EMBED_STUB=0);
# expects the model service reachable at $AVSA_EMBED_URL (default
# http://localhost:8001/embed). The pytest fixture used in per-PR CI lives
# at tests/fixtures/catalog.py and does NOT call this recipe — see issue
# #028 for the per-PR / scheduled split.
seed-catalog:
    AVSA_EMBED_STUB=0 uv run python scripts/seed-catalog.py

# Manual first-deploy to prod from the local machine via Application Default
# Credentials (ADC). Runs terraform apply against environments/prod/shared,
# ensures the Helm namespace exists, and prints next steps.
#
# Prerequisites:
#   gcloud auth application-default login
#   GCP_PROJECT_ID exported (e.g. export GCP_PROJECT_ID=avsa-prd)
#
# Phases: terraform → secrets → Cloud Build (4 GKE images) → Prometheus →
# Modal model deploy → helm upgrade → catalog seed → smoke gate.
# See docs/runbooks/release-pipeline.md "Manual first-deploy procedure".
deploy-prod:
    @echo "Running manual prod deploy from local machine..."
    bash scripts/deploy-prod.sh


# Stand up the full local AVSA stack in LOCAL-REAL mode:
#   - model on :8090 with AVSA_MODEL_STUB=0 (real ViT weights, no download needed)
#   - Rust batcher on :8081 (reads :8090)
#   - orchestrator with AVSA_LLM_STUB unset (real AVSA.LLM.Anthropic)
#   - API with AVSA_ORCHESTRATOR_STUB unset (real gRPC)
#   - shopper on :3000
#
# Secrets (AVSA_ANTHROPIC_API_KEY, AVSA_STORAGE_HMAC_SECRET) are sourced from
# .env early so all backgrounded services inherit them. Values are never echoed.
#
# To opt out of real LLM (e.g. no Anthropic key): set AVSA_LLM_STUB=1 before
# calling stack-up, or export it from your shell. See docs/runbooks/local-real-stack.md.
#
# Services run in the background; logs go to /tmp/avsa-logs/<name>.log.
# Run `just stack-down` to stop everything.
stack-up:
    #!/usr/bin/env bash
    set -euo pipefail
    root=$(git rev-parse --show-toplevel)
    log_dir=/tmp/avsa-logs
    pid_file=/tmp/avsa-stack.pids
    mkdir -p "${log_dir}"

    # ── Tear down stale services + observability container ──────────────────
    # Always start fresh so we never pick up a stale binary, cached config, or
    # half-dead container. The local DB is preserved by default (data lives in
    # the named docker volume); pass STACK_UP_RESET_DB=1 to also drop the DB
    # volume + reseed the catalog from scratch.
    echo "==> Tearing down old services + observability container..."
    if [[ -f "${pid_file}" ]]; then
        while IFS=' ' read -r name pid; do
            kill "${pid}" 2>/dev/null || true
        done < "${pid_file}"
        rm -f "${pid_file}"
    fi
    # Port sweep (catches anything started outside stack-up too). LISTEN-only
    # so we never kill client processes that just hold a connection to one of
    # these ports (e.g. the API's gRPC channel to the orchestrator).
    for port in 8090 8081 9568 8080 3000; do
        pid=$(lsof -ti ":${port}" -sTCP:LISTEN 2>/dev/null || true)
        [[ -n "${pid}" ]] && kill -9 ${pid} 2>/dev/null && echo "==> killed :${port}"
    done
    docker compose -f "${root}/infra/local-observability/docker-compose.yml" down 2>/dev/null || true

    if [[ "${STACK_UP_RESET_DB:-0}" == "1" ]]; then
        echo "==> STACK_UP_RESET_DB=1: dropping local DB volume (will reseed catalog from Fashion200k)..."
        docker compose -f "${root}/infra/local-db/docker-compose.yml" down -v 2>/dev/null || true
    fi

    # ── Generate local config ───────────────────────────────────────────────
    # Merge avsa.base.toml + avsa.local.toml → config/avsa.toml so the model
    # service, batcher, and scripts all read the same generated file. The local
    # overlay sets device=mps and use_fp16=true; the committed base stays CI-safe.
    echo "==> Generating config/avsa.toml (env=local)..."
    just config-gen local

    # ── Source secrets early so all child processes inherit them ────────────
    # AVSA_ANTHROPIC_API_KEY and AVSA_STORAGE_HMAC_SECRET are read from .env.
    # set -a exports every variable that gets set; set +a restores normal mode.
    # Values are NEVER echoed — only a presence check is printed below.
    set -a
    [ -f "${root}/.env" ] && . "${root}/.env"
    set +a

    # Report key presence without revealing values.
    if [[ -n "${AVSA_ANTHROPIC_API_KEY:-}" ]]; then
        echo "==> AVSA_ANTHROPIC_API_KEY: present (${#AVSA_ANTHROPIC_API_KEY} chars)"
    else
        echo "WARN: AVSA_ANTHROPIC_API_KEY not set — real LLM calls will fail." >&2
        echo "      Set AVSA_LLM_STUB=1 to use the mock LLM instead." >&2
    fi

    # ── Docker / Colima check ───────────────────────────────────────────────
    # Colima reports "done" before its Docker socket is ready; retry briefly.
    for i in $(seq 1 30); do
        docker info >/dev/null 2>&1 && break
        [[ $i -eq 30 ]] && { echo "ERROR: Docker daemon unreachable — run 'colima start' first." >&2; exit 1; }
        echo "==> Waiting for Docker daemon... (${i}s)"
        sleep 2
    done

    # ── Build code fresh ───────────────────────────────────────────────────
    # Always rebuild from source so a stale binary / venv / module never lands
    # in the running stack. Incremental builds are fast.
    echo "==> Building Rust batcher (cargo --release)..."
    cargo build --release --manifest-path "${root}/crates/batcher/Cargo.toml"

    echo "==> Fetching + compiling orchestrator (mix deps.get + mix compile)..."
    (cd "${root}/apps/orchestrator" && mix deps.get && mix compile)

    # Self-heal stale venvs (see `setup` recipe for the rationale).
    for v in "${root}/apps/model/.venv" "${root}/apps/api/.venv"; do
        if [[ -d "$v" ]] && ! "$v/bin/python3" --version >/dev/null 2>&1; then
            echo "==> $v references a missing Python interpreter; recreating..."
            rm -rf "$v"
        fi
    done

    echo "==> Syncing model venv (uv --extra model)..."
    uv sync --extra model --directory "${root}/apps/model"

    echo "==> Syncing API venv (uv)..."
    uv sync --directory "${root}/apps/api"

    echo "==> Installing JS workspace deps (pnpm)..."
    # Plain `pnpm install` (NOT --frozen-lockfile) so a parallel session adding
    # a dep doesn't strand stack-up — it regenerates the lockfile on the fly.
    # CI runs --frozen-lockfile separately to catch lockfile drift in PRs.
    (cd "${root}/frontend" && pnpm install)

    # ── Containers ─────────────────────────────────────────────────────────
    echo "==> Starting DB container and applying migrations..."
    just db-migrate

    # Seed catalog only when empty. On a fresh machine, seed using the real
    # Fashion200k source (honouring config/avsa.toml [catalog] seed_count and
    # source). On the standard dev machine 5000 rows are already present and
    # this block is skipped.
    echo "==> Checking catalog row count..."
    existing=$(psql "${DATABASE_URL:-postgresql://avsa:avsa@localhost:5434/avsa}" \
        -tAc "SELECT COUNT(*) FROM catalog.products" 2>/dev/null || echo 0)
    if [[ "${existing}" -eq 0 ]]; then
        echo "==> Catalog empty — seeding real fashion200k (honours [catalog] seed_count in avsa.toml)..."
        AVSA_EMBED_STUB=0 uv run python scripts/seed-catalog.py
        echo "==> catalog seeded"
    else
        echo "==> catalog already has ${existing} rows, skipping seed"
    fi

    echo "==> Starting observability stack (Prometheus :9090, Grafana :3010)..."
    docker compose -f "${root}/infra/local-observability/docker-compose.yml" up -d

    # ── Background service launcher ─────────────────────────────────────────
    : > "${pid_file}"
    _bg() {
        local name=$1 port=$2; shift 2
        local log="${log_dir}/${name}.log"
        if nc -z 127.0.0.1 "${port}" 2>/dev/null; then
            echo "==> ${name}: already listening on :${port}, skipping"
            return
        fi
        echo "==> Starting ${name} (log: ${log})..."
        "$@" >"${log}" 2>&1 &
        echo "${name} $!" >> "${pid_file}"
    }

    # ── Services (LOCAL-REAL, all stubs off) ───────────────────────────────
    # Services bind to 0.0.0.0 so Prometheus can reach them via host.docker.internal.

    # MODEL — real ViT on :8090. Weights (google/vit-base-patch16-224 +
    # clip-ViT-B-32) are cached locally; no download. CPU/MPS load takes
    # 30–90 s before /embed responds; the health gate below waits up to 4 min.
    # AVSA_MODEL_STUB=0 selects the real HuggingFace/CLIP inference path.
    #
    # DEVICE: local defaults to MPS (Apple GPU) — measured ~1.8× the CPU QPS
    # (100 vs 56 img/s, cosine-equivalent; see docs/qps-local-optimization.md).
    # The committed config default stays cpu so CI is unaffected; resolve_device
    # gives env precedence, so this env wins locally. Override for a
    # non-Apple box (AVSA_MODEL_DEVICE=cpu) or prod (=cuda).
    #
    # Run uvicorn from the REPO ROOT (not apps/model) using the model venv
    # directly. This is necessary because config/avsa.toml [model]
    # attribute_heads_dir is a relative path ("./data/attribute_heads/...")
    # that must resolve against the repo root, not apps/model. Using
    # `uv --directory apps/model run` sets the Python cwd to apps/model,
    # which would break the relative heads-dir lookup.
    # DEVICE and FP16 are driven by the generated config/avsa.toml (avsa.local.toml
    # overlay sets device=mps, use_fp16=true). AVSA_MODEL_DEVICE / AVSA_MODEL_FP16
    # env vars still take precedence if set in the calling shell (env beats config).
    _bg model       8090 \
        bash -c "cd '${root}' && AVSA_MODEL_STUB=0 \
            '${root}/apps/model/.venv/bin/uvicorn' avsa_model.main:app \
            --host 0.0.0.0 --port 8090"

    # BATCHER — Rust binary on :8081. Reads vit_service_url (:8090) from
    # config/avsa.toml [batcher]. Run from repo root so relative config path
    # "config/avsa.toml" resolves correctly.
    _bg batcher     8081 \
        bash -c "cd '${root}' && exec '${root}/crates/batcher/target/release/avsa-batcher'"

    # ORCHESTRATOR — real AVSA.LLM.Anthropic (AVSA_LLM_STUB unset).
    # AVSA_ANTHROPIC_API_KEY is inherited from the sourced .env above.
    # AVSA_MODEL_URL points TextTool /embed_text at the real ViT on :8090
    # (default in text_tool.ex is :8001; runtime.exs reads AVSA_MODEL_URL).
    # To opt out of real LLM: export AVSA_LLM_STUB=1 before calling stack-up.
    # AVSA_BATCHER_URL routes orchestrator embed calls through the Toxiproxy
    # batcher proxy (:18081 → :8081). With no toxics active this is a transparent
    # passthrough; E2E circuit-breaker tests add toxics via the Toxiproxy control
    # API (:8474) to trigger controlled failures without touching the batcher itself.
    _bg orchestrator 9568 \
        bash -c "cd '${root}/apps/orchestrator' && AVSA_MODEL_URL=http://localhost:8090 AVSA_BATCHER_URL=http://localhost:18081 exec mix phx.server"

    # API — real gRPC to orchestrator (AVSA_ORCHESTRATOR_STUB unset).
    # batcher_url defaults to http://localhost:8081 (avsa_api/config.py:47).
    _bg api          8080 \
        uv --directory "${root}/apps/api" run uvicorn avsa_api.main:app \
            --host 0.0.0.0 --port 8080

    # NEXT_PUBLIC_* envs surface the conditional navbar links (Metrics, API
    # Docs) — pointed at the local Grafana baseline dashboard and the shopper's
    # own /api/docs proxy (which relays the API's Swagger UI). next dev reads
    # them at render time, so the shopper must be (re)started with them set.
    _bg shopper      3000 \
        bash -c "cd '${root}/frontend' && \
            AVSA_API_URL=http://localhost:8080 \
            NEXT_PUBLIC_GRAFANA_URL=http://localhost:3010/d/avsa-operations \
            NEXT_PUBLIC_API_DOCS_URL=http://localhost:3000/api/docs \
            exec pnpm --filter @avsa/shopper run dev"

    # ── Health gates ────────────────────────────────────────────────────────
    _wait() {
        local name=$1 url=$2 secs=${3:-60}
        echo -n "    waiting for ${name} ..."
        for i in $(seq 1 "${secs}"); do
            curl -sf --max-time 5 "${url}" >/dev/null 2>&1 && echo " ready (${i}s)" && return
            sleep 1
        done
        echo " TIMEOUT after ${secs}s" >&2; return 1
    }
    _wait_port() {
        local name=$1 port=$2 secs=${3:-60}
        echo -n "    waiting for ${name} ..."
        for i in $(seq 1 "${secs}"); do
            nc -z 127.0.0.1 "${port}" 2>/dev/null && echo " listening (${i}s)" && return
            sleep 1
        done
        echo " TIMEOUT after ${secs}s" >&2; return 1
    }

    # Model (:8090) — real ViT load takes 30–90 s on CPU/MPS; allow up to 4 min.
    echo -n "    waiting for model (:8090, real ViT — may take 30-90s on CPU/MPS) ..."
    for i in $(seq 1 240); do
        code=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8090/embed \
            -H "Content-Type: application/json" -d '{"images":[]}' 2>/dev/null || true)
        [[ "${code}" != "000" ]] && echo " ready (${i}s, HTTP ${code})" && break
        sleep 1
        [[ $i -eq 240 ]] && { echo " TIMEOUT" >&2; exit 1; }
    done

    # Batcher (:8081) — TCP health gate (no HTTP health endpoint needed).
    _wait_port batcher 8081 30

    _wait orchestrator http://127.0.0.1:9568/metrics 90
    _wait api          http://127.0.0.1:8080/health
    # Next.js binds the port within seconds but first-compile HTTP response
    # takes 60-90s; check TCP only so we don't hang on the initial compile.
    _wait_port shopper 3000 60

    # ── Readiness summary ───────────────────────────────────────────────────
    echo ""
    echo "  ┌─────────────────────────────────────────────────────────────┐"
    echo "  │  AVSA LOCAL-REAL stack is up                   │"
    echo "  ├─────────────────────────────────────────────────────────────┤"
    echo "  │  Model (real ViT) http://localhost:8090                     │"
    echo "  │  Batcher          http://localhost:8081                     │"
    echo "  │  Orchestrator     http://localhost:9568/metrics             │"
    echo "  │  API              http://localhost:8080/docs                │"
    echo "  │  Shopper          http://localhost:3000                     │"
    echo "  │  Grafana          http://localhost:3010/d/avsa-operations   │"
    echo "  │  Prometheus       http://localhost:9090                     │"
    echo "  ├─────────────────────────────────────────────────────────────┤"
    echo "  │  LLM: REAL (unset AVSA_LLM_STUB)  — opt-out: AVSA_LLM_STUB=1 │"
    echo "  │  Model: REAL (AVSA_MODEL_STUB=0)  — no stub                │"
    echo "  │  PIDs → ${pid_file}"
    echo "  │  Logs → ${log_dir}/"
    echo "  │  Stop      → just stack-down                                │"
    echo "  │  Reset DB  → STACK_UP_RESET_DB=1 just stack-up              │"
    echo "  └─────────────────────────────────────────────────────────────┘"

# Stop all services started by `just stack-up` and tear down containers.
# Kills model (:8090), batcher (:8081), orchestrator (:9568), api (:8080),
# shopper (:3000), and tears down Docker containers.
stack-down:
    #!/usr/bin/env bash
    root=$(git rev-parse --show-toplevel)
    pid_file=/tmp/avsa-stack.pids
    # Kill tracked PIDs first (fast path), then sweep all stack ports to catch
    # any processes that were pre-existing or started outside stack-up.
    if [[ -f "${pid_file}" ]]; then
        while IFS=' ' read -r name pid; do
            echo "==> Stopping ${name} (PID ${pid})..."
            kill "${pid}" 2>/dev/null || true
        done < "${pid_file}"
        rm -f "${pid_file}"
    fi
    # Sweep all ports used by the real stack (model :8090, batcher :8081,
    # orchestrator :9568, api :8080, shopper :3000).
    for port in 8090 8081 9568 8080 3000; do
        pid=$(lsof -ti ":${port}" 2>/dev/null || true)
        [[ -n "${pid}" ]] && kill -9 ${pid} 2>/dev/null && echo "==> killed :${port} (PID ${pid})"
    done
    echo "==> Tearing down containers..."
    docker compose -f "${root}/infra/local-observability/docker-compose.yml" down
    docker compose -f "${root}/infra/local-db/docker-compose.yml" down
    echo "==> Done."

#  QPS benchmark — system QPS and in-memory model GPU ceiling.
# Wraps scripts/bench-prod.sh using [bench.prod] / [bench.model] sweep profiles.
# Defaults to the local stack; set AVSA_PROD_BATCHER_URL for prod.
#
# Optional env:
#   AVSA_PROD_BATCHER_URL   — batcher URL (default: http://localhost:8081)
#   AVSA_PROD_MODEL_URL     — model URL   (default: http://localhost:8090)
#   AVSA_BENCH_PROD_TARGET  — 'batcher' (default) or 'model'
#   AVSA_BENCH_PROD_OUTDIR  — override output dir
#
# Usage:
#   just bench-qps                                           # local system QPS
#   AVSA_PROD_BATCHER_URL=http://34.x.x.x:80 just bench-qps # prod system QPS
#   AVSA_BENCH_PROD_TARGET=model just bench-qps              # local GPU ceiling
#   AVSA_BENCH_PROD_TARGET=model AVSA_PROD_MODEL_URL=https://erinversfeldcodes--avsa-model-model-api.modal.run just bench-qps  # prod GPU ceiling
bench-qps:
    #!/usr/bin/env bash
    set -euo pipefail
    root=$(git rev-parse --show-toplevel)
    export AVSA_PROD_BATCHER_URL="${AVSA_PROD_BATCHER_URL:-http://localhost:8081}"
    bash "${root}/scripts/bench-prod.sh"

# ---------------------------------------------------------------------------
# Modal deployment
# ---------------------------------------------------------------------------
#
# Deploy order:
#   1. just modal-deploy-model   → copy the printed HTTPS URL
#   2. modal secret create avsa-batcher AVSA_MODEL_URL=<model-url>
#   3. just modal-deploy-batcher → copy the printed HTTPS URL
#   4. modal secret create avsa-api AVSA_DB_URL=<neon-url> AVSA_BATCHER_URL=<batcher-url> ...
#   5. just modal-deploy-api
#   6. AVSA_PROD_BATCHER_URL=<batcher-url> just bench-qps
#
# Dev (hot-reload) mode:  `modal serve modal_deploy/<app>.py`
# Tear down:              `modal app stop avsa-model` (etc.)

modal-deploy-model:
    uv run modal deploy modal_deploy/model_app.py

modal-deploy-batcher:
    uv run modal deploy modal_deploy/batcher_app.py

modal-deploy-api:
    uv run modal deploy modal_deploy/api_app.py

modal-deploy-all: modal-deploy-model modal-deploy-batcher modal-deploy-api

# Generate config/avsa.toml from config/avsa.base.toml + an environment overlay.
#
# config/avsa.toml is gitignored — it is always a generated artifact, never committed.
# Readers (model service, batcher, scripts) always consume exactly one file (avsa.toml).
# Source files (avsa.base.toml, avsa.local.toml, avsa.prod.toml) are committed.
#
# Environments:
#   ci     — base only; CI-safe defaults (device=cpu, use_fp16=false)
#   local  — base + config/avsa.local.toml  (device=mps, use_fp16=true, wider bench sweep)
#   prod   — base + config/avsa.prod.toml   (device=cuda, use_fp16=true, k8s service URLs)
#
# Usage:
#   just config-gen local     # local dev machine
#   just config-gen ci        # CI (no overlay, CI-safe defaults)
#   just config-gen prod      # production deploy
config-gen env:
    uv run python scripts/config-gen.py {{env}}

# Regenerate Elixir proto bindings from specs/orchestrator/avsa.proto.
#
# Requires protoc-gen-elixir on PATH (installed by `just setup` via
# `mix escript.install hex protobuf`). The generated file is committed so
# CI can diff it; run this whenever avsa.proto changes.
#
# Usage:
#   just proto-gen
proto-gen:
    #!/usr/bin/env bash
    set -euo pipefail
    export PATH="${HOME}/.mix/escripts:${PATH}"
    root=$(git rev-parse --show-toplevel)
    buf generate "${root}/specs/orchestrator"
    echo "==> Proto bindings regenerated: apps/orchestrator/apps/avsa/lib/avsa/proto/avsa.pb.ex"

# Regenerate the PYTHON proto stubs from specs/orchestrator/avsa.proto.
#
# grpc_tools.protoc emits a top-level `import avsa_pb2`, which does not resolve
# when the stubs live in the `avsa_api.proto` package; we rewrite it to a
# package-absolute import so `from avsa_api.proto import avsa_pb2_grpc` works at
# runtime. The drift check in tests/e2e/test_e2e_journey.py applies the SAME
# rewrite before comparing, so committed == regenerated. The generated files are
# committed so CI can diff them; run this whenever avsa.proto changes (alongside
# `just proto-gen` for the Elixir bindings).
#
# Usage:
#   just proto-gen-python
proto-gen-python:
    #!/usr/bin/env bash
    set -euo pipefail
    root=$(git rev-parse --show-toplevel)
    out="${root}/apps/api/src/avsa_api/proto"
    uv run --project "${root}/apps/api" python -m grpc_tools.protoc \
        --proto_path="${root}/specs/orchestrator" \
        --python_out="${out}" \
        --grpc_python_out="${out}" \
        avsa.proto
    # Rewrite the grpc stub's top-level import to a package-absolute one (the
    # fixup grpc_tools does not apply). -i.bak for GNU/BSD sed portability.
    sed -i.bak 's/^import avsa_pb2 as avsa__pb2$/from avsa_api.proto import avsa_pb2 as avsa__pb2/' \
        "${out}/avsa_pb2_grpc.py"
    rm -f "${out}/avsa_pb2_grpc.py.bak"
    echo "==> Python proto stubs regenerated: ${out}/avsa_pb2{,_grpc}.py"

# — recorded external-agent MCP discovery loop (proves Thread B).
#
# Drives AVSA's conformant JSON-RPC 2.0 / Streamable-HTTP MCP surface
# (AVSA.MCP.Server, ADR 0008) as an external client would and writes the full
# transcript + per-turn tool-call trace to evals/mcp/discovery-session.json:
#
#   tools/list -> find_similar(image) -> extract_attributes ->
#   "this but green" colour-constrained refinement ->
#   two boundary-rejection turns (injection payload -> -32602; bad bearer -> 401).
#
# This recipe runs against an ALREADY-LIVE, seeded stack — it does NOT bring up
# Docker. Stand the stack up first so the MCP listener is mounted and the catalog
# is seeded (real kNN), e.g.:
#
#   AVSA_MCP_EXPOSED=1 AVSA_MCP_API_KEY=<key> just stack-up   # or your live stack
#
# (stack-up mounts the MCP listener on :8082 when DATABASE_URL is set — see ADR
# 0008 / runtime.exs.) Then run THIS recipe. The committed artifact at
# evals/mcp/discovery-session.json starts as a clearly-labelled PLACEHOLDER; the
# real recorded transcript is produced here at the live-stack verification.
#
# Env (secrets sourced from .env; AVSA_MCP_API_KEY value is NEVER echoed or
# committed — only its presence is recorded in the artifact):
#   AVSA_MCP_URL      MCP endpoint (default http://localhost:8082/)
#   AVSA_MCP_API_KEY  bearer key, when the server is keyed
#   AVSA_MCP_IMAGE    path to a real product image for the image-driven turns
#
# Usage:
#   just mcp-demo
#   AVSA_MCP_IMAGE=data/sample.jpg just mcp-demo
mcp-demo:
    #!/usr/bin/env bash
    set -euo pipefail
    root=$(git rev-parse --show-toplevel)

    # Source .env so AVSA_MCP_API_KEY propagates — value is NEVER echoed.
    set -a
    [ -f "${root}/.env" ] && . "${root}/.env"
    set +a

    if [[ -n "${AVSA_MCP_API_KEY:-}" ]]; then
        echo "==> AVSA_MCP_API_KEY: present (${#AVSA_MCP_API_KEY} chars) — bearer auth on"
    else
        echo "==> AVSA_MCP_API_KEY: unset — assuming an open localhost server (ADR 0008)"
    fi

    uv run python "${root}/scripts/demo_mcp_session.py" \
        --url "${AVSA_MCP_URL:-http://localhost:8082/}" \
        --out "${root}/evals/mcp/discovery-session.json" \
        ${AVSA_MCP_IMAGE:+--image "${AVSA_MCP_IMAGE}"}

    echo "==> mcp-demo DONE. Artifact: evals/mcp/discovery-session.json"
