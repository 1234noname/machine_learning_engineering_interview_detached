"""Tests for POST /chat request guards: size, MIME, and form validation."""

import io

import pytest
from avsa_core.config import APIConfig
from httpx import AsyncClient

from avsa_api.main import app
from tests.conftest import SAMPLE_JPEG as _JPEG_1PX

_MAX_BYTES = 10_485_760


# ---------------------------------------------------------------------------
# Size guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_413_when_content_length_exceeds_limit(client: AsyncClient) -> None:
    """Content-Length header larger than max_upload_bytes must yield 413."""
    too_large = _MAX_BYTES + 1
    response = await client.post(
        "/chat",
        files={"image": ("test.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": "hello"},
        headers={
            "X-Forwarded-For": "10.0.0.2",
            "Content-Length": str(too_large),
        },
    )
    assert response.status_code == 413, f"Expected 413, got {response.status_code}"


@pytest.mark.asyncio
async def test_413_when_body_exceeds_limit_without_content_length(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """413 is returned even when Content-Length is absent/zero (chunked transfer path).

    Content-Length: 0 bypasses the header fast-reject; _read_limited() - the
    authoritative streaming counter - must still reject the oversized body.
    """
    monkeypatch.setattr(
        app.state,
        "config",
        APIConfig(rate_limit_rpm=60, max_upload_bytes=5),
    )
    response = await client.post(
        "/chat",
        files={"image": ("test.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": "hello"},
        headers={
            "X-Forwarded-For": "10.0.0.10",
            "Content-Length": "0",
        },
    )
    assert response.status_code == 413, f"Expected 413, got {response.status_code}"


# ---------------------------------------------------------------------------
# MIME and magic-byte guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_415_when_image_mime_type_not_allowed(client: AsyncClient) -> None:
    """Uploading a GIF must yield 415 Unsupported Media Type."""
    response = await client.post(
        "/chat",
        files={"image": ("test.gif", io.BytesIO(_JPEG_1PX), "image/gif")},
        data={"text": "hello"},
        headers={"X-Forwarded-For": "10.0.0.3"},
    )
    assert response.status_code == 415, f"Expected 415, got {response.status_code}"


@pytest.mark.asyncio
async def test_415_when_magic_bytes_mismatch_declared_mime(client: AsyncClient) -> None:
    """JPEG bytes declared as image/png must yield 415 (magic-byte mismatch)."""
    response = await client.post(
        "/chat",
        files={"image": ("photo.png", io.BytesIO(_JPEG_1PX), "image/png")},
        data={"text": "hello"},
        headers={"X-Forwarded-For": "10.0.0.6"},
    )
    assert response.status_code == 415, f"Expected 415, got {response.status_code}"


@pytest.mark.asyncio
async def test_415_when_jpeg_bytes_declared_as_webp(client: AsyncClient) -> None:
    """JPEG bytes declared as image/webp must yield 415 (RIFF+WEBP magic not present)."""
    response = await client.post(
        "/chat",
        files={"image": ("photo.webp", io.BytesIO(_JPEG_1PX), "image/webp")},
        data={"text": "hello"},
        headers={"X-Forwarded-For": "10.0.0.7"},
    )
    assert response.status_code == 415, f"Expected 415, got {response.status_code}"


@pytest.mark.asyncio
async def test_415_when_jpeg_bytes_declared_as_heic(client: AsyncClient) -> None:
    """JPEG bytes declared as image/heic must yield 415 (ftyp box not present)."""
    response = await client.post(
        "/chat",
        files={"image": ("photo.heic", io.BytesIO(_JPEG_1PX), "image/heic")},
        data={"text": "hello"},
        headers={"X-Forwarded-For": "10.0.0.8"},
    )
    assert response.status_code == 415, f"Expected 415, got {response.status_code}"


# ---------------------------------------------------------------------------
# Form validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_422_when_image_field_missing_and_text_empty(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """Multipart request without image AND with empty text must yield 422 (no usable input)."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")
    response = await client.post(
        "/chat",
        data={"text": ""},
        headers={"X-Forwarded-For": "10.0.0.4"},
    )
    assert response.status_code == 422, f"Expected 422, got {response.status_code}"


@pytest.mark.asyncio
async def test_422_when_text_field_exceeds_max_length(client: AsyncClient) -> None:
    """text field longer than 2000 characters must yield 422."""
    response = await client.post(
        "/chat",
        files={"image": ("test.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        data={"text": "a" * 2001},
        headers={"X-Forwarded-For": "10.0.0.9"},
    )
    assert response.status_code == 422, f"Expected 422, got {response.status_code}"
