"""Behavioural tests for `machine_learning_engineering_interview.api`.

Hardening — replaces the placeholder `tests/test_smoke.py`.
The legacy `download_image` endpoint generates a deterministic synthetic
JPEG keyed by `image_id`; tests verify both the response shape (JPEG magic
bytes, content-type) and the determinism (same id → same bytes).
"""

from fastapi.testclient import TestClient

from machine_learning_engineering_interview.api import api

client = TestClient(api)

# JPEG file magic — first three bytes of every JPEG.
JPEG_MAGIC = b"\xff\xd8\xff"


def test_download_image_returns_jpeg() -> None:
    resp = client.get("/test123.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content[:3] == JPEG_MAGIC
    assert len(resp.content) > 100, "JPEG body suspiciously small"


def test_download_image_is_deterministic() -> None:
    """Same image_id seeds the random number generator → same JPEG bytes."""
    resp1 = client.get("/seed-deterministic.jpg")
    resp2 = client.get("/seed-deterministic.jpg")
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.content == resp2.content, "endpoint is not deterministic-by-id"


def test_download_image_different_ids_differ() -> None:
    """Different image_ids produce different bytes (sanity check)."""
    resp_a = client.get("/id-a.jpg")
    resp_b = client.get("/id-b.jpg")
    assert resp_a.content != resp_b.content, "different ids produced identical bytes"
