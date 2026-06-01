"""Shared fixtures for the model-service /embed test suite (stub mode).

Hosts the stub-mode HTTP client (image embedder pre-loaded on app.state)
and the canonical minimal-JPEG bytes used as /embed input, so test_embed.py
and test_embed_attributes.py share one definition rather than duplicating
them. test_embed_text.py defines its own text-embedder client (it sets
app.state.text_embedder), which shadows this one for that module.
"""

from __future__ import annotations

import importlib.util
import os
import tomllib
from collections.abc import AsyncGenerator
from pathlib import Path
from types import ModuleType

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("AVSA_MODEL_STUB", "1")

from avsa_model.main import app
from avsa_model.stub import StubEmbedder

_SAMPLE_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b"
    "0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e3334"
    "32ffc0000b080001000101011100ffc4001f0000010501010101010100000000000000000102030405060708"
    "090a0bffc400b5100002010303020403050504040000017d0102030004110512213141061351610722711432"
    "8191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a43444546474849"
    "4a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3"
    "a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9ea"
    "f1f2f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)


@pytest.fixture
def sample_jpeg() -> bytes:
    """The canonical minimal valid 1x1 JPEG used as /embed test input."""
    return _SAMPLE_JPEG


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async client with the stub image embedder pre-loaded on app.state.

    Initialise app.state.embedder directly so tests don't depend on lifespan
    startup -- keeps the test surface on the HTTP layer.
    """
    app.state.embedder = StubEmbedder()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _repo_root() -> Path:
    """Walk up to the repo root (the directory containing config/avsa.base.toml)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "config" / "avsa.base.toml").exists():
            return parent
    raise FileNotFoundError("config/avsa.base.toml not found above apps/model/tests/")


def _load_config_gen(root: Path) -> ModuleType:
    """Import the hyphenated scripts/config-gen.py as a module for its _deep_merge."""
    path = root / "scripts" / "config-gen.py"
    spec = importlib.util.spec_from_file_location("config_gen", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def local_config() -> dict[str, object]:
    """The 'local' profile config (base + avsa.local.toml), generated in-memory.

    Mirrors scripts/config-gen.py local via config-gen's own _deep_merge,
    so tests read the values a local dev's generated config/avsa.toml carries
    -- deterministically from the committed sources, and WITHOUT clobbering the
    dev's real (gitignored) config/avsa.toml.
    """
    root = _repo_root()
    config_gen = _load_config_gen(root)
    with (root / "config" / "avsa.base.toml").open("rb") as f:
        base = tomllib.load(f)
    with (root / "config" / "avsa.local.toml").open("rb") as f:
        overlay = tomllib.load(f)
    merged: dict[str, object] = config_gen._deep_merge(base, overlay)
    return merged
