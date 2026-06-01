"""Tests for POST /embed error paths - run with AVSA_MODEL_STUB=1.

The happy-path embedding contract (shape, determinism, byte-for-byte stub
values) is covered by test_embed_attributes.py's byte-for-byte test; this
file pins the route's own validation logic - empty batch -> 422, malformed
base64 -> 400. The stub-mode client fixture lives in conftest.py.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_embed_empty_batch_returns_422(client: AsyncClient) -> None:
    """POST /embed with empty images list returns 422 Unprocessable Entity."""
    response = await client.post("/embed", json={"images": []})
    assert response.status_code == 422, (
        f"Expected 422 for empty batch, got {response.status_code}: {response.text}"
    )


@pytest.mark.asyncio
async def test_embed_malformed_base64_returns_400(client: AsyncClient) -> None:
    """POST /embed with malformed base64 string returns 400 Bad Request."""
    response = await client.post("/embed", json={"images": ["not-valid-base64!!@@##"]})
    assert response.status_code == 400, (
        f"Expected 400 for malformed base64, got {response.status_code}: {response.text}"
    )
