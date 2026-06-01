# Local Observability Stack

Prometheus + Grafana running locally via Docker Compose. Prometheus scrapes all five AVSA services and the Next.js shopper. Grafana auto-provisions one dashboard:

- **`avsa-operations`** — end-to-end RTT breakdown, per-component latency, throughput, errors, circuit breaker state/melt/recovery, embed cache hit rate.

## How to read `avsa-operations`

The operations dashboard argues one story: **the ViT model is the cost/capacity engine; the LLM owns what the shopper *feels* (latency).** Read it top-to-bottom:

| Row | Question | How it reframes the others |
|---|---|---|
| **End-to-End Request Journey** | Conversation latency p50/p95 vs the 3000 ms SLO; per-stage breakdown | The wait is **LLM-bound (~1–3 s), not ViT-bound (~350 ms)** at bs=24. |
| **Throughput** | Chat requests/s across all layers; ViT embed QPS | "Maximise model QPS" = shoppers served per GPU. Higher QPS lowers cost-per-shopper. |
| **Latency by Component** | Batcher p50/p95; shopper proxy duration | Isolate which layer is regressing. |
| **Errors** | Batcher errors by outcome; rate limit hits; empty retrieval results | A spike here needs immediate investigation. |
| **Circuit Breakers** | State (CLOSED/BLOWN) per circuit; melt rate; recovery events | A non-zero state with no recovery = upstream is still down. |
| **Cache & Retrieval Quality** | Embed cache hit rate; retrieval result count distribution | A low hit rate is expected for single-image turns; a flat result distribution at 0–2 is a quality alarm. |

## Prerequisites

- **Colima** (not Docker Desktop): `colima start` — no extra configuration needed; the compose file already sets `extra_hosts: host-gateway` on both services to resolve `host.docker.internal`.
- **Docker Compose v2**: use `docker compose` (not `docker-compose`).
- **AVSA services running**: `just stack-up` must be active. The services run as native processes on the host (uvicorn, the Rust release binary, `mix`, `pnpm dev`), binding directly to the host ports below. Prometheus reaches them via `host.docker.internal:PORT` from inside the Docker network — the compose file's `extra_hosts` mapping resolves that hostname to the host gateway on Colima.

Service ports that `just stack-up` exposes (matching the targets in `prometheus.yml`):

| Service | Host address | Port |
|---------|-------------|------|
| avsa-api | `host.docker.internal` | 8080 |
| avsa-batcher | `host.docker.internal` | 8081 |
| avsa-model | `host.docker.internal` | 8090 |
| avsa-orchestrator | `host.docker.internal` | 9568 |
| avsa-shopper (Next.js) | `host.docker.internal` | 3000 (scraped at `/api/metrics`) |

## Start the stack

```bash
docker compose -f infra/local-observability/docker-compose.yml up -d
```

## Verify

| Interface | URL |
|-----------|-----|
| Prometheus | http://localhost:9090 |
| Prometheus alerts | http://localhost:9090/alerts |
| Grafana | http://localhost:3010 |
| AVSA Operations dashboard | http://localhost:3010/d/avsa-operations |

**Grafana login:** username `admin`, password `admin`.

> **Security note:** The Grafana admin password is `admin` (plaintext in `docker-compose.yml`). This is acceptable for a local-only non-production stack. Do not use this credential for any environment with external access.

## Tear down

```bash
docker compose -f infra/local-observability/docker-compose.yml down
```

To also remove the Grafana persistent volume (if one was added in future):
```bash
docker compose -f infra/local-observability/docker-compose.yml down -v
```

## Colima note

Docker Desktop auto-populates `host.docker.internal` as the host machine IP; Colima does not. This stack uses `extra_hosts: ["host.docker.internal:host-gateway"]` on both the `prometheus` and `grafana` services, which instructs Docker to resolve `host.docker.internal` to the host gateway IP at runtime. This works on Colima, Docker Desktop, and Linux Docker — no additional configuration is needed.

## File layout

```
infra/local-observability/
├── docker-compose.yml                        # Compose stack definition
├── prometheus.yml                            # Prometheus global config + scrape targets
├── alerts.yml                                # Prometheus alerting rules
├── grafana/
│   ├── provisioning/
│   │   ├── datasources/prometheus.yml        # Auto-configures Prometheus datasource
│   │   └── dashboards/default.yml            # Dashboard provider (loads from /var/lib/grafana/dashboards)
│   └── provisioning/dashboards/default.yml   # loads every *.json from /var/lib/grafana/dashboards
└── README.md                                 # This file

# Dashboards are mounted into the grafana container by docker-compose.yml volumes:
#   ../grafana/baseline.json -> /var/lib/grafana/dashboards/baseline.json
#   ../grafana/detail.json   -> /var/lib/grafana/dashboards/detail.json

infra/grafana/
└── operations.json                           # All-metrics ops dashboard (uid: avsa-operations)

docs/runbooks/
├── model-error-rate.md                       # Runbook for ModelHighErrorRate alert
└── model-latency.md                          # Runbook for ModelLatencyRegression alert
```

## Alert rules

Two alert rules are defined in `alerts.yml`:

| Alert | Condition | For | Runbook |
|-------|-----------|-----|---------|
| `ModelHighErrorRate` | Model 5xx rate > 5% | 2 min | [model-error-rate.md](../../docs/runbooks/model-error-rate.md) |
| `ModelLatencyRegression` | Model p95 latency > 2s | 5 min | [model-latency.md](../../docs/runbooks/model-latency.md) |

## Validating configuration

```bash
promtool check config infra/local-observability/prometheus.yml
promtool check rules infra/local-observability/alerts.yml
docker compose -f infra/local-observability/docker-compose.yml config --quiet
python3 -c "import json; json.load(open('infra/grafana/operations.json'))"
```

Install `promtool` via Homebrew: `brew install prometheus`.

## Post-merge human step

After merging and starting the stack, trigger a synthetic alert to prove the alerting loop end-to-end:

```bash
# 1. Start services
just stack-up

# 2. Start observability stack (separate terminal)
docker compose -f infra/local-observability/docker-compose.yml up -d

# 3. Trigger ModelHighErrorRate (404s inflate error rate)
for i in $(seq 1 200); do curl -s http://localhost:8090/does-not-exist > /dev/null; done

# 4. Wait 2–3 minutes; check http://localhost:9090/alerts — ModelHighErrorRate should be Firing
```
