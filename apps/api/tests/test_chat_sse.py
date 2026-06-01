"""Tests for POST /chat SSE streaming response."""

import io
import json

import pytest
from httpx import AsyncClient

from tests.conftest import SAMPLE_JPEG as _JPEG_1PX

_PNG_VALID = b"\x89PNG\r\n\x1a\n" + bytes(100)
_WEBP_VALID = b"RIFF" + bytes(4) + b"WEBP" + bytes(100)
_HEIC_VALID = bytes(4) + b"ftyp" + bytes(100)


def _parse_sse_events_strict(body: str) -> list[dict]:
    """Parse all SSE data lines; raises ValueError on non-JSON data lines."""
    events = []
    for line in body.splitlines():
        if line.startswith("data:"):
            raw = line[len("data:") :].strip()
            if raw:
                events.append(json.loads(raw))
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "mime", "data"),
    [
        ("photo.png", "image/png", _PNG_VALID),
        ("photo.webp", "image/webp", _WEBP_VALID),
        ("photo.heic", "image/heic", _HEIC_VALID),
    ],
)
async def test_chat_sse_accepts_valid_alternate_format(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    filename: str,
    mime: str,
    data: bytes,
) -> None:
    """Each allowed non-JPEG format with correct magic bytes returns 200 SSE."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")
    response = await client.post(
        "/chat",
        files={"image": (filename, io.BytesIO(data), mime)},
        data={"text": ""},
        headers={"X-Forwarded-For": "10.0.0.14"},
    )
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    assert "text/event-stream" in response.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_sse_stream_emits_product_card_event_in_valid_format(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """Validates the structure of every event in the stream (not just that it
    exists) - catching silent schema regressions in the SSE serialisation path.
    Also the JPEG happy path: a valid JPEG yields a 200 text/event-stream.
    """
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")

    response = await client.post(
        "/chat",
        files={"image": ("product.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": "what is this product?"},
        headers={"X-Forwarded-For": "10.0.0.99"},
    )

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    assert "text/event-stream" in response.headers.get("content-type", "")

    body = response.text

    data_lines = [line for line in body.splitlines() if line.startswith("data:")]
    assert data_lines, f"No data: lines found in SSE body: {body!r}"

    events = _parse_sse_events_strict(body)
    assert events, f"No parseable JSON events in SSE body: {body!r}"

    card_events = [e for e in events if e.get("type") == "product_card"]
    assert card_events, f"No product_card event in stream; events: {events}"

    required_card_fields = {"id", "title", "price", "currency", "image_url", "category"}
    for event in card_events:
        card = event.get("card", {})
        missing = required_card_fields - card.keys()
        assert not missing, f"product_card event missing required fields {missing}: {event}"

    for line in data_lines:
        raw = line[len("data:") :].strip()
        assert raw, f"Unexpected empty data: line in SSE stream: {line!r}"
