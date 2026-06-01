"""E2E test fixtures for the AVSA full-stack integration suite.

All fixtures in this module are only active when AVSA_E2E=1. When the
environment variable is absent or not "1", every e2e test is skipped via the
`e2e_skip` auto-use fixture.

The e2e stack is managed externally (bring it up with `just stack-up`) — these
fixtures assume the stack is already running and connect to it over HTTP on
localhost.

Asyncio scope note: the root pyproject.toml sets asyncio_mode = "strict".
In strict mode, async fixtures must be scoped to match the event loop their
tests run in. Since the default test event loop is function-scoped, the
`e2e_client` fixture is also function-scoped to avoid "Event loop is closed"
errors between test functions. For a stack with expensive startup, this
is an acceptable trade-off in e2e mode — the real penalty is the external
service startup, not the per-test client construction.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

__all__: list[str] = []


# ---------------------------------------------------------------------------
# Guard — skip the entire session when AVSA_E2E != "1"
# ---------------------------------------------------------------------------


def _is_e2e_enabled() -> bool:
    return os.environ.get("AVSA_E2E") == "1"


@pytest.fixture(scope="session", autouse=True)
def e2e_skip() -> None:
    """Session-scoped auto-use fixture: skip ALL e2e tests unless AVSA_E2E=1.

    Using autouse=True at session scope means pytest evaluates this once and
    either proceeds or skips every test in the e2e package — without requiring
    each test to repeat the check. The skip message names the flag so the CI
    log is self-explaining.
    """
    if not _is_e2e_enabled():
        pytest.skip(
            "E2E tests require a running AVSA stack. "
            "Set AVSA_E2E=1 and bring the stack up with `just stack-up` before executing."  # noqa: E501
        )


# ---------------------------------------------------------------------------
# Base URL
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def e2e_base_url() -> str:
    """Return the base URL of the AVSA API gateway under test.

    Reads AVSA_E2E_API_URL from the environment so CI can override the host
    if the stack runs on a different address. Defaults to localhost:8080 which
    matches the API port that `just stack-up` binds.
    """
    return os.environ.get("AVSA_E2E_API_URL", "http://localhost:8080")


# ---------------------------------------------------------------------------
# HTTP client — function-scoped to avoid event loop lifecycle issues.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def e2e_client(e2e_base_url: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Return an httpx.AsyncClient pre-configured for the e2e stack.

    Function-scoped (default) to avoid "Event loop is closed" errors in
    pytest-asyncio strict mode, where the event loop is function-scoped.
    The client is re-created for each test — acceptable because the cost is in
    TCP connection setup, not fixture creation.

    The client is configured with:
    - A generous timeout (30 s) to accommodate the full ViT inference path.
    - The MCP API key expected by the API gateway in e2e mode.
    - follow_redirects=False to catch unexpected redirects as test failures.
    """
    async with httpx.AsyncClient(
        base_url=e2e_base_url,
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0),
        headers={
            "X-AVSA-API-Key": os.environ.get(
                "AVSA_E2E_MCP_KEY", "e2e-test-key-not-a-secret"
            )
        },
        follow_redirects=False,
    ) as client:
        yield client
