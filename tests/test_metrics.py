"""Tests that both the api and model services expose a Prometheus /metrics endpoint.

Written before instrumentation is added (test-first Phase 1).
Expected to fail with HTTP 404 until `prometheus-fastapi-instrumentator` is
wired into api.py and model.py.
"""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from machine_learning_engineering_interview import model
from machine_learning_engineering_interview.api import api


def test_api_metrics_returns_200() -> None:
    """GET /metrics on the api service returns HTTP 200."""
    client = TestClient(api)
    resp = client.get("/metrics")
    assert resp.status_code == 200


def test_api_metrics_contains_http_requests_total() -> None:
    """GET /metrics body contains the http_requests_total counter."""
    client = TestClient(api)
    resp = client.get("/metrics")
    assert "http_requests_total" in resp.text


def test_model_metrics_returns_200() -> None:
    """GET /metrics on the model service returns HTTP 200."""
    mock_image_model = MagicMock()
    with patch.object(model, "get_model", return_value=mock_image_model):
        client = TestClient(model.app)
        resp = client.get("/metrics")
    assert resp.status_code == 200


def test_model_metrics_contains_http_requests_total() -> None:
    """GET /metrics body contains the http_requests_total counter."""
    mock_image_model = MagicMock()
    with patch.object(model, "get_model", return_value=mock_image_model):
        client = TestClient(model.app)
        resp = client.get("/metrics")
    assert "http_requests_total" in resp.text
