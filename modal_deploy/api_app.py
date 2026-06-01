"""Modal deployment — AVSA API service.

Wraps avsa_api FastAPI ASGI app on Modal.  Uses orchestrator stub mode
(AVSA_ORCHESTRATOR_STUB=1) — the Elixir orchestrator is not deployed to Modal
in the initial implementation.

Required Modal secret (named "avsa-api"):
  AVSA_DB_URL           — Cloud SQL (GCP) Postgres connection string
  AVSA_BATCHER_URL      — Modal batcher endpoint HTTPS URL
  AVSA_MCP_API_KEY      — MCP authentication key
  AVSA_STORAGE_HMAC_SECRET — HMAC secret for signed image-proxy URLs

Deploy:  modal deploy modal_deploy/api_app.py
Serve:   modal serve modal_deploy/api_app.py   (hot-reload dev mode)

The generated HTTPS URL becomes the value for the shopper's AVSA_API_URL.
"""

from __future__ import annotations

import modal

# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
api_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi>=0.120,<0.130",
        "starlette>=0.52.1",
        "uvicorn[standard]>=0.27,<0.35",
        "httpx>=0.27,<0.30",
        "pydantic>=2.6,<3.0",
        "asyncpg>=0.29,<1.0",
        "grpcio>=1.60,<2.0",
        "protobuf>=5.0,<7.0",
        "python-multipart>=0.0.9,<1.0",
        "prometheus-fastapi-instrumentator>=7.0",
        "numpy>=1.26,<3",
        "psycopg[binary]>=3.1,<4",
    )
    # env must precede add_local_* (Modal 1.x: no build steps after local mounts).
    .env(
        {
            "PYTHONPATH": "/app/src",
            # Stub the Elixir orchestrator — not deployed on Modal initially.
            "AVSA_ORCHESTRATOR_STUB": "1",
            # Expose OpenAPI docs in this environment for iterative development.
            "AVSA_ENV": "development",
        }
    )
    .add_local_dir("apps/api/src", remote_path="/app/src")
    .add_local_file("config/avsa.toml", remote_path="/app/config/avsa.toml")
    .add_local_dir("specs", remote_path="/app/specs")
)

app = modal.App("avsa-api", image=api_image)


@app.function(
    secrets=[modal.Secret.from_name("avsa-api")],
    scaledown_window=300,
)
@modal.asgi_app()
def api_asgi() -> object:
    """Return the avsa_api FastAPI app for Modal's ASGI runner."""
    from avsa_api.main import app as fastapi_app

    return fastapi_app
