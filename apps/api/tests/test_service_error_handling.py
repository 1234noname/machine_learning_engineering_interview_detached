"""Tests for cross-service error propagation.

Verifies that errors from downstream services (orchestrator stub, batcher)
produce correct HTTP responses rather than 500 Internal Server Error.

These are unit tests (stub mode) that characterise existing error-handling
behaviour - the goal is to make that behaviour explicit and detectable so
future regressions are caught immediately.
"""

from __future__ import annotations

import io
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from tests.conftest import SAMPLE_JPEG as _JPEG_1PX

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable stub mode for every test in this module."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")


@pytest.fixture
async def error_client(_stub_env: None) -> AsyncGenerator[AsyncClient, None]:
    """Like client, but with raise_app_exceptions=False so unhandled server
    exceptions are returned as HTTP 500 responses instead of being re-raised.
    """
    from avsa_api.main import app

    async with (
        LifespanManager(app) as manager,
        AsyncClient(
            transport=ASGITransport(app=manager.app, raise_app_exceptions=False),
            base_url="http://test",
        ) as ac,
    ):
        yield ac


# ---------------------------------------------------------------------------
# stream_chat raises during SSE streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_chat_error_closes_sse_stream(error_client: AsyncClient) -> None:
    """If the orchestrator client's stream_chat raises mid-stream, the SSE stream ends.

    The /chat handler does not catch errors from stream_chat - an exception
    propagates out of the async generator. The HTTP response still starts with
    200 (headers are sent before streaming begins), so the status code is 200
    even when the stream terminates abnormally. This test verifies that the
    client receives a response rather than hanging indefinitely.

    Uses error_client (raise_app_exceptions=False) to receive the response
    rather than re-raising the mid-stream exception in the test process.
    """
    from avsa_api.clients.orchestrator import OrchestratorClient

    async def _failing_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[dict, None]:
        yield {
            "type": "product_card",
            "card": {"id": "x", "title": "X", "price": 0.0, "currency": "ZAR"},
        }
        raise RuntimeError("stream interrupted mid-flight")

    with patch.object(OrchestratorClient, "stream_chat", side_effect=_failing_stream):
        resp = await error_client.post(
            "/chat",
            files={"image": ("product.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
            data={"text": "test"},
            headers={"X-Forwarded-For": "10.0.0.50"},
        )

    assert resp.status_code == 200, f"Expected 200 (SSE errors are in-band), got {resp.status_code}"
    assert resp.text is not None
