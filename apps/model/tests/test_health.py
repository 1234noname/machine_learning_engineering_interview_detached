"""Liveness-probe tests for the model service (GET /health, /healthz).

The local e2e compose healthcheck curls ``/health`` to decide the model is up,
and the batcher/orchestrator ``depends_on`` it being healthy - so a missing
probe silently blocks the whole local stack. These run in stub mode (the
conftest default) and assert both paths return 200 ``{"status": "ok"}``; the
route reads nothing from ``app.state``, so it is up the moment the app serves.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/healthz"])
async def test_liveness_probe_returns_ok(client: AsyncClient, path: str) -> None:
    """Both liveness paths return 200 {"status": "ok"} (compose uses /health, Modal /healthz)."""
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}
