"""Tests for the storage backend factory (housekeeping fold-in F2).

avsa_core.storage._build_backend(config) dispatches on
config["storage"]["backend"]: "local" → LocalStorageBackend; any other name →
ValueError. Imported inside a try/except so collection succeeds with a
meaningful pytest.fail rather than a collection-time ImportError.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

try:
    from avsa_core.storage import LocalStorageBackend, _build_backend

    _FACTORY_AVAILABLE = True
except ImportError:
    _FACTORY_AVAILABLE = False


def _require_factory() -> None:
    if not _FACTORY_AVAILABLE:
        pytest.fail("avsa_core.storage._build_backend not importable")


def test_build_backend_returns_local_for_local_config(tmp_path: Path) -> None:
    """`backend = "local"` must dispatch to LocalStorageBackend with the configured root."""
    _require_factory()
    config: dict[str, Any] = {
        "storage": {
            "backend": "local",
            "local": {"root_path": str(tmp_path)},
        }
    }
    backend = _build_backend(config)
    assert isinstance(backend, LocalStorageBackend), (
        f"_build_backend with backend='local' must return a LocalStorageBackend instance; "
        f"got {type(backend).__name__}"
    )
    # Round-trip a byte payload to confirm the root was wired through correctly.
    backend.put_object("factory-check.bin", b"ok")
    assert backend.get_object("factory-check.bin") == b"ok", (
        "Backend returned by factory must honour the configured root_path; "
        "put_object/get_object roundtrip failed."
    )


def test_build_backend_raises_ValueError_for_unknown_backend() -> None:
    """Unknown backend names must surface a clear ValueError naming the bad backend."""
    _require_factory()
    config: dict[str, Any] = {"storage": {"backend": "azure"}}
    with pytest.raises(ValueError) as excinfo:
        _build_backend(config)
    assert "azure" in str(excinfo.value), (
        f"ValueError message must name the unknown backend ('azure'); got: {excinfo.value!r}"
    )
