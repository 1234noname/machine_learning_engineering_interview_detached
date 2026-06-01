"""Liveness routes for the model service - GET /health and /healthz.

Both return 200 {"status": "ok"} and read nothing from app.state, so a
probe succeeds as soon as the ASGI app is serving -- independent of whether the
(heavy) embedders have finished loading in the lifespan. The local e2e compose
healthcheck curls /health (and the batcher/orchestrator depends_on the
model being healthy); Modal / k8s probes conventionally use /healthz -- both
are exposed so either works. Mirrors the gateway's avsa_api.routes.health.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health", response_model=dict[str, str])
@router.get("/healthz", response_model=dict[str, str])
async def health() -> dict[str, str]:
    """Liveness probe: 200 {"status": "ok"} as soon as the app is serving."""
    return {"status": "ok"}
