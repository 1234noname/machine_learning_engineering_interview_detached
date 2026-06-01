"""Routes under test:
    GET /images/{path:path}?token=<hex>&expires=<unix>
        - 200 image/<mime>   on valid token + present object
        - 403                on expired / tampered / wrong-path token
        - 404 image/png      placeholder PNG on valid token but missing object
        - 400                on missing token or missing expires query params

Backend setup uses LocalStorageBackend directly to seed bytes and issue tokens,
matching the canonical injection pattern other route tests in this package use.
The route itself is expected to resolve the backend from app state (the
factory landed by F2 will be wired into app.state at lifespan time during
implementation; tests inject the backend via app.state to avoid coupling to a
config-file roundtrip).
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

try:
    from avsa_core.storage.local import LocalStorageBackend

    _STORAGE_AVAILABLE = True
except ImportError:
    _STORAGE_AVAILABLE = False


def _require_storage() -> None:
    if not _STORAGE_AVAILABLE:
        pytest.fail("avsa_core.storage.local.LocalStorageBackend not importable.")


# Minimal valid JPEG bytes (SOI + APP0 JFIF + EOI)
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

# PNG magic header
_PNG_MAGIC = b"\x89PNG"


@pytest.fixture
async def seeded_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[AsyncClient, LocalStorageBackend], None]:
    """Yield (client, backend) with a fresh LocalStorageBackend wired into app.state.

    The image route is expected to read `request.app.state.storage` (or an
    equivalent attribute set at lifespan time) so tests can inject a tmp_path
    backend without touching real config. If the route reads the backend under
    a different attribute name, the test will fail with an assertion-shape
    error (200 vs 404 / 500) that points at the wiring contract.
    """
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "route-test-secret")
    backend = LocalStorageBackend(root=tmp_path)

    from avsa_api.main import app

    app.state.storage = backend

    async with (
        LifespanManager(app) as manager,
        AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
        ) as ac,
    ):
        app.state.storage = backend
        yield ac, backend


async def test_get_image_returns_bytes_on_valid_token(
    seeded_client: tuple[AsyncClient, LocalStorageBackend],
) -> None:
    """A valid (token, expires) pair must serve the stored bytes with image Content-Type."""
    client, backend = seeded_client
    path = "fashion200k/images/abc.jpg"
    backend.put_object(path, _TINY_JPEG)
    signed = backend.signed_url(path, ttl_s=60)
    token, expires = _extract_token_and_expires(signed)

    response = await client.get(f"/images/{path}?token={token}&expires={expires}")

    assert response.status_code == 200, (
        f"valid token + present object must return 200; got {response.status_code} "
        f"body={response.content[:120]!r}"
    )
    assert response.content == _TINY_JPEG, (
        f"response body must equal the stored bytes; got {response.content[:40]!r}"
    )
    ctype = response.headers.get("content-type", "")
    assert ctype.startswith("image/"), (
        f"Content-Type must be an image/* type for a .jpg payload; got {ctype!r}"
    )


async def test_get_image_returns_403_on_expired_token(
    seeded_client: tuple[AsyncClient, LocalStorageBackend],
) -> None:
    """An expires value in the past must be rejected with 403, not 200 or 410."""
    client, backend = seeded_client
    path = "fashion200k/images/expired.jpg"
    backend.put_object(path, _TINY_JPEG)
    signed = backend.signed_url(path, ttl_s=60)
    token, _expires_unused = _extract_token_and_expires(signed)
    past = int(time.time()) - 3600

    response = await client.get(f"/images/{path}?token={token}&expires={past}")

    assert response.status_code == 403, (
        f"expired token must be rejected with 403; got {response.status_code}"
    )


async def test_get_image_returns_403_on_tampered_token(
    seeded_client: tuple[AsyncClient, LocalStorageBackend],
) -> None:
    """Flipping a single hex nibble in the token must invalidate the signature."""
    client, backend = seeded_client
    path = "fashion200k/images/tampered.jpg"
    backend.put_object(path, _TINY_JPEG)
    signed = backend.signed_url(path, ttl_s=60)
    token, expires = _extract_token_and_expires(signed)
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert tampered != token, "test setup: tampered token must differ from original"

    response = await client.get(f"/images/{path}?token={tampered}&expires={expires}")

    assert response.status_code == 403, (
        f"tampered token must be rejected with 403; got {response.status_code}"
    )


async def test_get_image_returns_403_on_wrong_path(
    seeded_client: tuple[AsyncClient, LocalStorageBackend],
) -> None:
    """A token signed for path A must not authorise path B (path-binding test)."""
    client, backend = seeded_client
    path_a = "fashion200k/images/path-a.jpg"
    path_b = "fashion200k/images/path-b.jpg"
    backend.put_object(path_a, _TINY_JPEG)
    backend.put_object(path_b, _TINY_JPEG)
    signed = backend.signed_url(path_a, ttl_s=60)
    token, expires = _extract_token_and_expires(signed)

    response = await client.get(f"/images/{path_b}?token={token}&expires={expires}")

    assert response.status_code == 403, (
        f"token bound to {path_a!r} must not authorise {path_b!r}; got {response.status_code}"
    )


async def test_get_image_returns_404_placeholder_on_missing_object(
    seeded_client: tuple[AsyncClient, LocalStorageBackend],
) -> None:
    """Valid token but no object at path must return 404 with a PNG placeholder."""
    client, backend = seeded_client
    path = "fashion200k/images/does-not-exist.jpg"
    signed = backend.signed_url(path, ttl_s=60)
    token, expires = _extract_token_and_expires(signed)

    response = await client.get(f"/images/{path}?token={token}&expires={expires}")

    assert response.status_code == 404, (
        f"missing object must return 404 (so caller can distinguish missing from served); "
        f"got {response.status_code}"
    )
    ctype = response.headers.get("content-type", "")
    assert ctype.startswith("image/png"), (
        f"placeholder must be served as image/png so the UI <img> tag renders it; "
        f"got Content-Type={ctype!r}"
    )
    assert len(response.content) > 0, "placeholder body must be non-empty"
    assert response.content.startswith(_PNG_MAGIC), (
        f"placeholder must be a real PNG (magic header {_PNG_MAGIC!r}); "
        f"got first bytes {response.content[:8]!r}"
    )


async def test_get_image_requires_both_token_and_expires_query_params(
    seeded_client: tuple[AsyncClient, LocalStorageBackend],
) -> None:
    """Missing either token or expires must yield 400 with a clear error JSON."""
    client, backend = seeded_client
    path = "fashion200k/images/missing-params.jpg"
    backend.put_object(path, _TINY_JPEG)
    signed = backend.signed_url(path, ttl_s=60)
    token, expires = _extract_token_and_expires(signed)

    r_no_token = await client.get(f"/images/{path}?expires={expires}")
    assert r_no_token.status_code == 400, (
        f"missing token query param must return 400; got {r_no_token.status_code}"
    )

    r_no_expires = await client.get(f"/images/{path}?token={token}")
    assert r_no_expires.status_code == 400, (
        f"missing expires query param must return 400; got {r_no_expires.status_code}"
    )


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _extract_token_and_expires(signed: object) -> tuple[str, int]:
    """Pull (token, expires) from whatever shape signed_url() returns.

    Post-F4 the canonical shape is a mapping/dataclass with `token` and
    `expires` attributes/keys. We try mapping access first, then attribute
    access, so the tests stay agnostic to the impl's dataclass-vs-TypedDict
    choice - but assert clearly when neither shape works.
    """
    if isinstance(signed, dict):
        if "token" not in signed or "expires" not in signed:
            pytest.fail(f"signed_url() returned a dict missing token/expires keys: {signed!r}")
        return str(signed["token"]), int(signed["expires"])
    if hasattr(signed, "token") and hasattr(signed, "expires"):
        return str(signed.token), int(signed.expires)  # type: ignore[attr-defined]
    pytest.fail(
        "signed_url() must return a mapping with token/expires keys OR an object "
        f"with .token/.expires attributes (per F4); got {type(signed).__name__}: {signed!r}"
    )
