"""Behavioural tests for `machine_learning_engineering_interview.model`.

The legacy ViT-b-16 weights are ~330MB; loading them in CI is slow and
network-dependent. Tests monkey-patch `get_model` to return a mock so
the real ViT never loads. The endpoint plumbing (request → model →
response) is what we verify, not the model itself.
"""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from machine_learning_engineering_interview import model


def test_predict_endpoint_returns_class_index() -> None:
    """The /predict endpoint forwards image_url to the model and returns its result."""
    mock_image_model = MagicMock()
    mock_image_model.predict.return_value = {"class_index": 285}

    with patch.object(model, "get_model", return_value=mock_image_model):
        client = TestClient(model.app)
        resp = client.get("/predict?image_url=http://test.example/img.jpg")

    assert resp.status_code == 200
    assert resp.json() == {"class_index": 285}
    mock_image_model.predict.assert_called_once_with("http://test.example/img.jpg")


def test_predict_endpoint_requires_image_url() -> None:
    """Missing image_url query param → 422 (FastAPI validation)."""
    with patch.object(model, "get_model", return_value=MagicMock()):
        client = TestClient(model.app)
        resp = client.get("/predict")
    assert resp.status_code == 422
