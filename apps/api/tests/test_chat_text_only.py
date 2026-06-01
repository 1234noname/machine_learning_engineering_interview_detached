"""Tests for text-only (no image) POST /chat turns."""

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from httpx import AsyncClient


def _parse_sse_events(body: str) -> list[dict]:
    """Return parsed JSON objects from SSE data lines."""
    import json

    return [
        json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_text_only_chat_returns_200_with_product_cards(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """POST /chat with only text (no image) returns 200 and at least one product_card SSE event."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")

    response = await client.post(
        "/chat",
        data={"text": "show me a summer dress"},
        headers={"X-Forwarded-For": "10.0.1.1"},
    )

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    assert "text/event-stream" in response.headers.get("content-type", "")

    events = _parse_sse_events(response.text)
    assert len(events) >= 1, f"Expected at least one SSE event, got: {response.text!r}"
    card_events = [e for e in events if e.get("type") == "product_card"]
    assert card_events, f"Expected a product_card event, got: {events}"


@pytest.mark.asyncio
async def test_text_only_chat_stub_receives_empty_image_bytes(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """stream_chat must receive an empty image_bytes list when no image is uploaded."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")

    captured: dict[str, object] = {}

    async def _fake_stream_chat(
        image_bytes: list[bytes],
        text: str,  # noqa: ARG001
        conversation_id: str = "",  # noqa: ARG001
    ) -> AsyncGenerator[dict[str, object], None]:
        captured["image_bytes"] = image_bytes
        yield {
            "type": "product_card",
            "card": {
                "id": "stub-001",
                "title": "Stub Product",
                "price": 0.0,
                "currency": "ZAR",
                "image_url": "http://example.com/stub.jpg",
                "category": "stub",
            },
        }

    from avsa_api.main import app

    stub_instance = app.state.orchestrator
    with patch.object(stub_instance, "stream_chat", side_effect=_fake_stream_chat):
        response = await client.post(
            "/chat",
            data={"text": "find me a dress"},
            headers={"X-Forwarded-For": "10.0.1.3"},
        )

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    assert "image_bytes" in captured, "stream_chat was never called"
    assert captured["image_bytes"] == [], (
        f"Expected an empty image_bytes list, got {captured['image_bytes']!r}"
    )


@pytest.mark.asyncio
async def test_multi_image_chat_forwards_all_uploads(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """Every uploaded image reaches stream_chat as an ordered list (combined query)."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")

    captured: dict[str, object] = {}

    async def _fake_stream_chat(
        image_bytes: list[bytes],
        text: str,  # noqa: ARG001
        conversation_id: str = "",  # noqa: ARG001
    ) -> AsyncGenerator[dict[str, object], None]:
        captured["image_bytes"] = image_bytes
        yield {
            "type": "product_card",
            "card": {
                "id": "stub-001",
                "title": "Stub Product",
                "price": 0.0,
                "currency": "ZAR",
                "image_url": "http://example.com/stub.jpg",
                "category": "stub",
            },
        }

    # Valid JPEG magic
    jpeg_a = b"\xff\xd8\xff" + b"\x00" * 32
    jpeg_b = b"\xff\xd8\xff" + b"\x11" * 32

    from avsa_api.main import app

    stub_instance = app.state.orchestrator
    with patch.object(stub_instance, "stream_chat", side_effect=_fake_stream_chat):
        response = await client.post(
            "/chat",
            files=[
                ("image", ("a.jpg", jpeg_a, "image/jpeg")),
                ("image", ("b.jpg", jpeg_b, "image/jpeg")),
            ],
            headers={"X-Forwarded-For": "10.0.1.7"},
        )

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    assert captured.get("image_bytes") == [jpeg_a, jpeg_b], (
        f"Expected both images forwarded in order, got {captured.get('image_bytes')!r}"
    )
