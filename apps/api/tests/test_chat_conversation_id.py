"""Tests for conversation_id propagation through POST /chat."""

import io
import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import SAMPLE_JPEG as _JPEG_1PX


@pytest.mark.asyncio
async def test_provided_conversation_id_header_ignored_server_generates_uuid(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """POST /chat with X-Conversation-Id request header must NOT echo it back.

    The server always generates a fresh UUID to prevent session fixation: a
    client-supplied ID could allow an attacker to hijack the push queue for a
    known UUID.  The response X-Conversation-Id is always a server-generated UUID4.
    """
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")
    client_supplied_id = "my-test-conv-id-123"

    response = await client.post(
        "/chat",
        files={"image": ("product.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": "what is this?"},
        headers={
            "X-Forwarded-For": "10.0.1.1",
            "X-Conversation-Id": client_supplied_id,
        },
    )

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    actual_id = response.headers.get("X-Conversation-Id", "")
    # Must be a valid UUID
    assert actual_id != client_supplied_id, (
        "Server must not echo a client-supplied X-Conversation-Id (session fixation risk)"
    )
    try:
        uuid.UUID(actual_id)
    except ValueError:
        pytest.fail(f"X-Conversation-Id in response is not a valid UUID: {actual_id!r}")


@pytest.mark.asyncio
async def test_missing_conversation_id_generates_uuid_in_response(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """POST /chat without X-Conversation-Id must generate a UUID and return it in header."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")

    response = await client.post(
        "/chat",
        files={"image": ("product.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": "what is this?"},
        headers={"X-Forwarded-For": "10.0.1.2"},
    )

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    conv_id_header = response.headers.get("X-Conversation-Id", "")
    assert conv_id_header, "Expected X-Conversation-Id header to be present"
    try:
        uuid.UUID(conv_id_header)
    except ValueError:
        pytest.fail(f"X-Conversation-Id is not a valid UUID: {conv_id_header!r}")


@pytest.mark.asyncio
async def test_stream_chat_called_with_server_generated_conversation_id(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """stream_chat must be called with the server-generated UUID, not the client header.

    The server ignores X-Conversation-Id request headers (session fixation defence)
    and always passes its own UUID4 to stream_chat and back in the response header.
    """
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")
    client_supplied_id = "conv-id-for-call-check"

    captured_kwargs: dict = {}

    async def mock_stream_chat(
        image_bytes: bytes,  # noqa: ARG001
        text: str,  # noqa: ARG001
        conversation_id: str = "",
    ):
        captured_kwargs["conversation_id"] = conversation_id
        yield {
            "type": "product_card",
            "card": {
                "id": "mock-001",
                "title": "Mock",
                "price": 0.0,
                "currency": "ZAR",
                "image_url": "",
                "category": "",
                "score": 1.0,
            },
        }

    from avsa_api.main import app

    app.state.orchestrator.stream_chat = mock_stream_chat  # type: ignore[method-assign]

    response = await client.post(
        "/chat",
        files={"image": ("product.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": ""},
        headers={
            "X-Forwarded-For": "10.0.1.3",
            "X-Conversation-Id": client_supplied_id,
        },
    )

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    actual_id = captured_kwargs.get("conversation_id", "")
    # must be a real UUID
    assert actual_id != client_supplied_id, (
        "stream_chat must not receive a client-supplied conversation_id (session fixation risk)"
    )
    try:
        uuid.UUID(actual_id)
    except ValueError:
        pytest.fail(f"stream_chat received non-UUID conversation_id: {actual_id!r}")
