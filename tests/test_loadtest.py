"""Behavioural tests for `machine_learning_engineering_interview.loadtest`.

The locust task constructs a `/predict?image_url=...` GET to the
configured `API_URL`. Test mocks the locust HTTP client; no real HTTP
issued.

Marked `loadtest` and excluded from the default pytest run — importing
locust monkey-patches `ssl`/`socket` via gevent at module load, which
breaks anyio (used by FastAPI's TestClient) for any test sharing the
process. CI/just runs this file in a separate pytest invocation.
"""

from unittest.mock import MagicMock, patch

import pytest

from machine_learning_engineering_interview.loadtest import QuickstartUser

pytestmark = pytest.mark.loadtest


class _TestQuickstartUser(QuickstartUser):
    # Pin a host so locust's HttpUser.__init__ doesn't raise StopTest.
    # Subclassing is more stable than __new__ + manual attr assignment —
    # if locust adds required __init__ state in a future version, tests
    # break with a clear TypeError rather than a confusing AttributeError.
    host = "http://test.local"


def test_download_image_task_hits_predict_endpoint() -> None:
    user = _TestQuickstartUser(environment=MagicMock())  # type: ignore[no-untyped-call]
    user.client = MagicMock()

    # Pin the random image-id so the assertion is deterministic.
    with patch(
        "machine_learning_engineering_interview.loadtest.random.randint",
        return_value=42,
    ):
        user.download_image()

    user.client.get.assert_called_once()
    call_args = user.client.get.call_args
    assert call_args.args[0] == "/predict", "task should GET /predict"
    assert call_args.kwargs["params"]["image_url"].endswith("/42.jpg"), (
        "task should pass the constructed image URL as the image_url query param"
    )


def test_quickstart_user_wait_time_configured() -> None:
    """Sanity: the locust `wait_time` is set (between 1 and 5 seconds)."""
    # `wait_time` on a HttpUser is a callable returning seconds; existence
    # is the contract — we don't pin the actual values.
    assert callable(QuickstartUser.wait_time)
