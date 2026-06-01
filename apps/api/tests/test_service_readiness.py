"""Tests for 503 Service Unavailable when app state is not initialised.

The lifespan handler populates app.state.limiter and app.state.orchestrator on
startup. If the lifespan fails (or someone bypasses it in a future test
harness), every chat request must return 503 rather than an unhandled 500.
These are unit tests: they call get_limiter / get_orchestrator with a mock
request whose app.state has no relevant attributes, without spinning up the app.
"""

import pytest
from fastapi import HTTPException

from avsa_api.clients.orchestrator import get_orchestrator
from avsa_api.middleware.rate_limit import get_limiter


def _request_without_state() -> object:
    """Return a minimal object that makes app.state attribute access raise AttributeError."""

    class _State:
        pass

    class _App:
        state = _State()

    class _Request:
        app = _App()

    return _Request()


def test_get_limiter_raises_503_when_state_missing() -> None:
    """get_limiter must raise HTTP 503 when app.state.limiter is absent."""
    request = _request_without_state()
    with pytest.raises(HTTPException) as exc_info:
        get_limiter(request)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Service not ready"


def test_get_orchestrator_raises_503_when_state_missing() -> None:
    """get_orchestrator must raise HTTP 503 when app.state.orchestrator is absent."""
    request = _request_without_state()
    with pytest.raises(HTTPException) as exc_info:
        get_orchestrator(request)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Service not ready"
