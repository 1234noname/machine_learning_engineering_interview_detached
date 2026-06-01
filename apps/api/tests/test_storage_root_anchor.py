"""Test storage root anchored to repo root regardless of process CWD"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from avsa_core.config import _repo_root, load_config_raw
from avsa_core.storage import _build_backend
from avsa_core.storage.local import LocalStorageBackend
from httpx import ASGITransport, AsyncClient

_REAL_KEY = "fashion200k/images/women/tops/sleeveless_and_tank_tops/91268533/91268533_0.jpeg.jpg"

_JPEG_MAGIC = b"\xff\xd8"


def _is_real_image(path: Path) -> bool:
    """True only when path holds actual image bytes, not a Git LFS pointer file."""
    if not path.exists():
        return False
    with path.open("rb") as fh:
        return fh.read(2) == _JPEG_MAGIC


# ---------------------------------------------------------------------------
# CWD-independence of _build_backend
# ---------------------------------------------------------------------------


def test_build_backend_root_is_absolute_and_repo_anchored(tmp_path: Path) -> None:
    """_build_backend must produce an absolute root anchored at the repo root.

    This is the core regression guard: when CWD is a non-repo-root directory,
    the relative root_path = "./data" from config must resolve to
    <repo_root>/data, NOT <CWD>/data.
    """
    repo_root = _repo_root()
    config = load_config_raw()

    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        backend = _build_backend(config)
    finally:
        os.chdir(original_cwd)

    assert isinstance(backend, LocalStorageBackend)

    resolved = backend._root.resolve()
    assert resolved.is_absolute(), (
        f"Backend root must be absolute after _build_backend; got {resolved!r}"
    )

    root_path_str = config["storage"]["local"]["root_path"]
    expected_root = (repo_root / root_path_str).resolve()
    assert resolved == expected_root, (
        f"Backend root must be anchored at the repo root.\n"
        f"  Expected: {expected_root}\n"
        f"  Got:      {resolved}\n"
        f"  CWD during _build_backend: {tmp_path}\n"
        f"  (Old code would have produced {(tmp_path / root_path_str).resolve()})"
    )


def test_build_backend_absolute_root_path_honoured(tmp_path: Path) -> None:
    """An already-absolute root_path in config must NOT be re-anchored."""
    config = {
        "storage": {
            "backend": "local",
            "local": {"root_path": str(tmp_path)},
        }
    }
    backend = _build_backend(config)
    assert isinstance(backend, LocalStorageBackend)
    assert backend._root.resolve() == tmp_path.resolve(), (
        f"Absolute root_path {tmp_path!r} must be honoured as-is by _build_backend; "
        f"got {backend._root.resolve()!r}"
    )
    backend.put_object("anchor-check.bin", b"absolute")
    assert backend.get_object("anchor-check.bin") == b"absolute"


def test_get_object_succeeds_from_non_repo_cwd(tmp_path: Path) -> None:
    """get_object on a real on-disk key must succeed even when CWD is not the repo root.

    This directly exercises the scenario that caused the original bug: the API
    server starts with CWD=apps/api, and ./data resolved to
    apps/api/data/ (absent) rather than <repo_root>/data/ (present).

    Skipped when the Fashion200k dataset is not present (local-dev-only data).
    """
    repo_root = _repo_root()
    real_file = repo_root / "data" / _REAL_KEY
    if not _is_real_image(real_file):
        pytest.skip(f"Fashion200k test image not present at {real_file}; skipping real-data test")

    config = load_config_raw()
    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        backend = _build_backend(config)
        data = backend.get_object(_REAL_KEY)
    finally:
        os.chdir(original_cwd)

    assert isinstance(data, bytes) and len(data) > 0, (
        f"get_object must return image bytes for key {_REAL_KEY!r} even when "
        f"the process CWD is {tmp_path!r} (not the repo root)"
    )


# ---------------------------------------------------------------------------
# Integration test: /images route returns 200 + image/jpeg
# ---------------------------------------------------------------------------


@pytest.fixture
async def anchored_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncGenerator[tuple[AsyncClient, LocalStorageBackend], None]:
    """Yield (client, backend) with the production _build_backend (repo-root anchored).

    The backend is built exactly as the lifespan would build it - via
    _build_backend(load_config_raw()) - but injected into app.state so the
    test does not require a real DB or orchestrator. The HMAC secret is set so
    signed_url() can issue tokens.

    We also chdir to a temp dir before building the backend to prove the
    anchoring is CWD-independent.
    """
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "anchor-integration-secret")

    config = load_config_raw()
    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        backend = _build_backend(config)
    finally:
        os.chdir(original_cwd)

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


async def test_images_route_200_real_key(
    anchored_client: tuple[AsyncClient, LocalStorageBackend],
) -> None:
    """GET /images/{key}?token=…&expires=… must return 200 + image/jpeg for a real on-disk key.

    This is the integration guard: it exercises the full request path
    - signed URL issuance, HTTP route, backend.get_object - and asserts that
    the response is the real image bytes (200 image/jpeg), NOT the 404 placeholder.

    Skipped when the Fashion200k dataset is not present locally.
    """
    client, backend = anchored_client

    repo_root = _repo_root()
    real_file = repo_root / "data" / _REAL_KEY
    if not _is_real_image(real_file):
        pytest.skip(f"Fashion200k test image not present at {real_file}; skipping integration test")

    signed = backend.signed_url(_REAL_KEY, ttl_s=60)
    token = signed["token"]
    expires = signed["expires"]

    response = await client.get(f"/images/{_REAL_KEY}?token={token}&expires={expires}")

    assert response.status_code == 200, (
        f"GET /images/{{real_key}} with a valid signed token must return 200; "
        f"got {response.status_code}. "
        f"Backend root: {backend._root.resolve()!r}. "
        f"Body preview: {response.content[:120]!r}"
    )
    ctype = response.headers.get("content-type", "")
    assert ctype.startswith("image/"), f"Response Content-Type must be image/*; got {ctype!r}"
    assert len(response.content) > 1000, (
        f"Real image must be >1 KB; got {len(response.content)} bytes - "
        "this suggests the placeholder PNG was returned instead"
    )


async def test_images_route_404_placeholder_from_tmp_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /images/{key} with a valid token but no object returns 404 + PNG placeholder.

    Uses a fresh tmp_path backend (no seeded files) so the backend is guaranteed
    to produce NotFound. The UI <img> still renders something (PNG placeholder), and the 404
    status lets the caller distinguish missing from served.
    """
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "placeholder-test-secret")
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
        path = "fashion200k/images/ghost.jpg"
        signed = backend.signed_url(path, ttl_s=60)
        token = signed["token"]
        expires = signed["expires"]

        response = await ac.get(f"/images/{path}?token={token}&expires={expires}")

    assert response.status_code == 404
    assert response.headers.get("content-type", "").startswith("image/png")
    assert response.content[:4] == b"\x89PNG"
