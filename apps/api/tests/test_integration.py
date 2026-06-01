"""These tests are skipped automatically when AVSA_ORCHESTRATOR_STUB=1 (CI / local
stub mode). To run them, point AVSA_ORCHESTRATOR_ADDR at a live orchestrator and
unset AVSA_ORCHESTRATOR_STUB (or set it to anything other than "1").
"""

import base64
import io
import json
import os
import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration

# A real 224x224 RGB JPEG (solid colour). The earlier 1x1-pixel fixture has an
# ambiguous channel dimension ((1,1,3)) that the REAL ViT image processor rejects
# (mean must have 1 elements ... got 3) → 500 → batcher 502.
_JPEG_REAL = base64.b64decode(
    """
/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsK
CwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQU
FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCADgAOADASIA
AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA
AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3
ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm
p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA
AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx
BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK
U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3
uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDwKiii
vzI/uMKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAC
iiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKK
KKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooo
oAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiig
AooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAC
iiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKK
KKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooo
oAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiig
AooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAC
iiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooA//2Q==
"""
)


@pytest.mark.skipif(
    os.environ.get("AVSA_ORCHESTRATOR_STUB", "1") == "1",
    reason="Real orchestrator not available in CI stub mode",
)
@pytest.mark.asyncio
async def test_chat_round_trip_returns_product_card(client: AsyncClient) -> None:
    """Full round-trip POST /chat - requires real orchestrator at AVSA_ORCHESTRATOR_ADDR.

    Asserts:
    - Response status 200
    - Content-Type is text/event-stream
    - At least one SSE data: line with type=product_card
    - X-Conversation-Id header is a valid UUID

    Also exercises the conversation resume flow end-to-end:
    - First request generates a server-assigned UUID in the response header.
    - Second request sends X-Resume-Conversation-Id with that UUID and asserts
      the response X-Conversation-Id echoes it back (session continuity).
    """
    # ── First request: fresh conversation ────────────────────────────────────
    response1 = await client.post(
        "/chat",
        files={"image": ("product.jpg", io.BytesIO(_JPEG_REAL), "image/jpeg")},
        data={"text": "what is this product?"},
        headers={"X-Forwarded-For": "10.0.2.1"},
    )

    assert response1.status_code == 200, (
        f"Expected 200, got {response1.status_code}: {response1.text}"
    )
    assert "text/event-stream" in response1.headers.get("content-type", ""), (
        f"Expected text/event-stream, got {response1.headers.get('content-type')}"
    )

    events1 = [
        json.loads(line[len("data: ") :])
        for line in response1.text.splitlines()
        if line.startswith("data: ")
    ]
    assert len(events1) >= 1, f"Expected at least one SSE event, got: {response1.text!r}"

    card_events1 = [e for e in events1 if e.get("type") == "product_card"]
    assert card_events1, f"Expected at least one product_card event, got: {events1}"

    first_conv_id = response1.headers.get("X-Conversation-Id", "")
    assert first_conv_id, "Expected X-Conversation-Id in first response"
    uuid.UUID(first_conv_id)  # must be a valid UUID

    # ── Second request: resume the conversation ───────────────────────────────
    response2 = await client.post(
        "/chat",
        files={"image": ("product.jpg", io.BytesIO(_JPEG_REAL), "image/jpeg")},
        data={"text": "tell me more about it"},
        headers={
            "X-Forwarded-For": "10.0.2.1",
            "X-Resume-Conversation-Id": first_conv_id,
        },
    )

    assert response2.status_code == 200, (
        f"Expected 200, got {response2.status_code}: {response2.text}"
    )
    assert "text/event-stream" in response2.headers.get("content-type", ""), (
        f"Expected text/event-stream, got {response2.headers.get('content-type')}"
    )

    resumed_id = response2.headers.get("X-Conversation-Id", "")
    assert resumed_id == first_conv_id, (
        f"Expected resumed X-Conversation-Id={first_conv_id!r}, got {resumed_id!r}"
    )
