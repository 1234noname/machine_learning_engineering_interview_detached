"""Tests for POST /embed_text - run with AVSA_MODEL_STUB=1.

All test cases must pass under stub mode; no model weights are downloaded.
"""

import hashlib
import math
import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Force stub mode before importing the app.
os.environ["AVSA_MODEL_STUB"] = "1"

from avsa_model.main import app
from avsa_model.text_stub import StubTextEmbedder

EMBEDDING_DIM = 512


def _stub_text_embedding(text: str) -> list[float]:
    """Reproduce the documented stub text-embedding algorithm independently.

    Mirrors avsa_model.text_stub.StubTextEmbedder: sha256(utf-8) -> 512
    floats (raw[i % 32] / 255.0) -> L2-normalise. If the implementation
    changes the algorithm the byte-for-byte test below fails - which is the
    point: queries embedded with a drifted algorithm would mismatch a catalog
    already seeded (infra/scripts/seed_text_embeddings.py) with the old one.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [digest[i % 32] / 255.0 for i in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw] if norm else raw


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async test client with stub text embedder pre-loaded on app.state."""
    app.state.text_embedder = StubTextEmbedder()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_embed_text_returns_512_dim(client: AsyncClient) -> None:
    """POST /embed_text with a valid batch returns 200 with embeddings of shape (N, 512)."""
    response = await client.post("/embed_text", json={"texts": ["a red dress", "blue jeans"]})
    assert response.status_code == 200, response.text
    data = response.json()
    assert "embeddings" in data
    embeddings = data["embeddings"]
    assert len(embeddings) == 2, f"Expected 2 embeddings, got {len(embeddings)}"
    for emb in embeddings:
        assert len(emb) == EMBEDDING_DIM, f"Expected {EMBEDDING_DIM}-dim embedding, got {len(emb)}"


@pytest.mark.asyncio
async def test_embed_text_matches_documented_stub_algorithm(client: AsyncClient) -> None:
    """The returned embedding is exactly the documented sha256 -> 512 -> L2 vector.

    Pins the stub text-embed algorithm byte-for-byte (symmetric to the image
    stub's backward-compat test). Guards against silent drift that would break
    text retrieval against an already-seeded catalog.
    """
    text = "a red floral summer dress"
    response = await client.post("/embed_text", json={"texts": [text]})
    assert response.status_code == 200, response.text
    embedding = response.json()["embeddings"][0]
    assert embedding == _stub_text_embedding(text), (
        "stub text-embedding bytes changed - a drifted algorithm would mismatch "
        "catalog text vectors seeded with the documented sha256 -> 512 -> L2 algorithm"
    )


@pytest.mark.asyncio
async def test_embed_text_determinism(client: AsyncClient) -> None:
    """Same text submitted twice returns identical embedding vectors."""
    payload = {"texts": ["a red dress"]}

    response1 = await client.post("/embed_text", json=payload)
    assert response1.status_code == 200, response1.text
    response2 = await client.post("/embed_text", json=payload)
    assert response2.status_code == 200, response2.text

    emb1 = response1.json()["embeddings"][0]
    emb2 = response2.json()["embeddings"][0]
    assert emb1 == emb2, "Stub text embedder is not deterministic - embeddings differ across calls"


@pytest.mark.asyncio
async def test_embed_text_empty_batch_422(client: AsyncClient) -> None:
    """POST /embed_text with empty texts list returns 422 Unprocessable Entity."""
    response = await client.post("/embed_text", json={"texts": []})
    assert response.status_code == 422, (
        f"Expected 422 for empty batch, got {response.status_code}: {response.text}"
    )


@pytest.mark.asyncio
async def test_embed_text_malformed_payload_422(client: AsyncClient) -> None:
    """POST /embed_text with texts as integer returns 422 (Pydantic type validation)."""
    response = await client.post("/embed_text", json={"texts": 123})
    assert response.status_code == 422, (
        f"Expected 422 for malformed payload, got {response.status_code}: {response.text}"
    )
