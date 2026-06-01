"""Tests for the sliding-window rate limiter on POST /chat."""

import io
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from avsa_api.middleware.rate_limit import SlidingWindowLimiter
from tests.conftest import SAMPLE_JPEG as _JPEG_1PX


def _multipart_payload(ip: str) -> dict:
    return {
        "files": {"image": ("test.jpg", io.BytesIO(_JPEG_1PX), "image/jpeg")},
        "data": {"text": "find me a product"},
        "headers": {"X-Forwarded-For": ip},
    }


@pytest.mark.asyncio
async def test_rate_limit_60_requests_allowed_61st_rejected(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    """First 60 requests from the same IP must succeed; 61st must be 429."""
    monkeypatch.setenv("AVSA_ORCHESTRATOR_STUB", "1")
    ip = "10.0.0.1"
    for i in range(60):
        payload = _multipart_payload(ip)
        response = await client.post(
            "/chat",
            files=payload["files"],
            data=payload["data"],
            headers=payload["headers"],
        )
        assert response.status_code != 429, f"Request {i + 1} was rate-limited unexpectedly"

    payload = _multipart_payload(ip)
    response = await client.post(
        "/chat",
        files=payload["files"],
        data=payload["data"],
        headers=payload["headers"],
    )
    assert response.status_code == 429, f"Expected 429 on request 61, got {response.status_code}"
    assert "Retry-After" in response.headers, "429 response must include Retry-After header"
    assert response.headers["Retry-After"] == "60"


def test_sliding_window_resets_after_60s() -> None:
    """This is a unit test of SlidingWindowLimiter directly so we can control
    monotonic time without real sleeps. It validates the sliding aspect of
    the window: after the first burst expires, the IP is no longer throttled.
    """
    limiter = SlidingWindowLimiter(rpm=2)

    with patch("avsa_api.middleware.rate_limit.monotonic") as mock_time:
        mock_time.return_value = 0.0
        limiter.check("10.1.1.1")
        limiter.check("10.1.1.1")

        with pytest.raises(HTTPException) as exc_info:
            limiter.check("10.1.1.1")
        assert exc_info.value.status_code == 429
        assert exc_info.value.headers["Retry-After"] == "60"

        mock_time.return_value = 61.0
        limiter.check("10.1.1.1")


def test_slow_drip_multi_ip_tracking_is_bounded_by_lru_eviction() -> None:
    """Slow-drip multi-IP defence: tracked IPs are capped by _MAX_TRACKED_IPS."""
    with patch("avsa_api.middleware.rate_limit._MAX_TRACKED_IPS", 3):
        limiter = SlidingWindowLimiter(rpm=60)
        for octet in range(1, 6):
            limiter.check(f"10.0.0.{octet}")

        assert len(limiter._window) == 3
        assert set(limiter._window) == {"10.0.0.3", "10.0.0.4", "10.0.0.5"}


def test_evicted_ip_gets_a_fresh_bucket() -> None:
    """The deliberate trade-off of LRU eviction: an evicted IP is 'forgiven'."""
    with patch("avsa_api.middleware.rate_limit._MAX_TRACKED_IPS", 2):
        limiter = SlidingWindowLimiter(rpm=1)
        limiter.check("10.0.0.1")
        with pytest.raises(HTTPException):
            limiter.check("10.0.0.1")

        limiter.check("10.0.0.2")
        limiter.check("10.0.0.3")
        assert "10.0.0.1" not in limiter._window  # evicted (LRU)

        limiter.check("10.0.0.1")
