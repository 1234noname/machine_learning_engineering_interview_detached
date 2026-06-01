"""Tests for conversation resume via X-Resume-Conversation-Id header."""

import io
import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import SAMPLE_JPEG as _JPEG_1PX


@pytest.mark.asyncio
async def test_valid_resume_id_echoed_back(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """X-Resume-Conversation-Id with a valid UUID is used as the conversation_id."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")
    prior_id = str(uuid.uuid4())

    response = await client.post(
        "/chat",
        files={"image": ("p.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": "show me more"},
        headers={"X-Resume-Conversation-Id": prior_id},
    )

    assert response.status_code == 200
    assert response.headers.get("X-Conversation-Id") == prior_id


@pytest.mark.asyncio
async def test_invalid_resume_id_generates_fresh_uuid(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """X-Resume-Conversation-Id with an invalid value is ignored; fresh UUID generated."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")

    response = await client.post(
        "/chat",
        files={"image": ("p.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": "search"},
        headers={"X-Resume-Conversation-Id": "not-a-uuid"},
    )

    assert response.status_code == 200
    returned_id = response.headers.get("X-Conversation-Id", "")
    assert returned_id != "not-a-uuid"
    uuid.UUID(returned_id)  # must be a valid UUID


@pytest.mark.asyncio
async def test_absent_resume_id_generates_fresh_uuid(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """Without X-Resume-Conversation-Id the server always generates a fresh UUID."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")

    response = await client.post(
        "/chat",
        files={"image": ("p.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": "search"},
    )

    assert response.status_code == 200
    returned_id = response.headers.get("X-Conversation-Id", "")
    uuid.UUID(returned_id)


@pytest.mark.asyncio
async def test_resume_id_passed_to_stream_chat(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """stream_chat receives the resume conversation_id when a valid one is supplied."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")
    prior_id = str(uuid.uuid4())
    captured: dict = {}

    async def mock_stream(image_bytes: bytes, text: str, conversation_id: str = ""):  # noqa: ARG001
        captured["conversation_id"] = conversation_id
        yield {
            "type": "product_card",
            "card": {
                "id": "x",
                "title": "X",
                "price": 0.0,
                "currency": "ZAR",
                "image_url": "",
                "category": "",
                "score": 1.0,
            },
        }

    from avsa_api.main import app

    app.state.orchestrator.stream_chat = mock_stream  # type: ignore[method-assign]

    response = await client.post(
        "/chat",
        files={"image": ("p.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": ""},
        headers={"X-Resume-Conversation-Id": prior_id},
    )

    assert response.status_code == 200
    assert captured.get("conversation_id") == prior_id
