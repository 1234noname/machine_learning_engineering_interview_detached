"""Storage abstraction for AVSA binary artefacts.

This package defines the ``StorageBackend`` Protocol, the ``SignedToken``
shape that ``signed_url`` returns, and re-exports the domain error type
``NotFound`` (defined in ``avsa_core.storage.errors``). Concrete backends live
in submodules — ``avsa_core.storage.local.LocalStorageBackend`` (filesystem) is
the only backend. The Protocol is shaped to permit a drop-in swap without churn
at call sites should another backend be introduced later.

Per project memory: AVSA runs locally only in the current branch — the
``LocalStorageBackend`` is the only sanctioned backend in production code
paths today, with bytes landing under ``./data/`` (gitignored).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from avsa_core.config import _repo_root
from avsa_core.storage.errors import NotFound
from avsa_core.storage.local import LocalStorageBackend, SignedToken


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol every concrete storage backend implements.

    Methods are synchronous because the only backend today (local
    filesystem) is synchronous I/O; async callers (e.g. ``acquire_image``)
    invoke these from within their own ``async`` boundaries. If a remote
    backend is introduced later the Protocol can grow async variants — kept
    synchronous now to avoid an ``asyncio.to_thread`` wrapper for the
    local case.
    """

    def get_object(self, path: str) -> bytes:
        """Return the bytes stored under ``path``; raise ``NotFound`` if absent."""
        ...

    def put_object(self, path: str, data: bytes) -> None:
        """Persist ``data`` under ``path``; create parent directories as needed."""
        ...

    def list_objects(self, prefix: str) -> Iterable[str]:
        """Yield every stored key under ``prefix`` (relative to the backend root)."""
        ...

    def signed_url(self, path: str, ttl_s: int | None = None) -> SignedToken:
        """Return a time-limited ``SignedToken`` authorising access to ``path``.

        Implementations must use a keyed HMAC (secret out-of-band via env);
        the token covers ``path`` and an absolute Unix expiry. The route layer
        is responsible for turning the token into a URL. ``ttl_s`` is optional:
        ``None`` selects the backend's configured default TTL and any supplied
        value is clamped to the configured maximum ( F-SEC-3).
        """
        ...

    def verify_signed_url(self, path: str, token: str, expires: int) -> bool:
        """Return True iff ``token`` is a valid HMAC for ``path|expires`` AND not expired."""
        ...


def _build_backend(config: dict[str, Any]) -> StorageBackend:
    """Construct the configured ``StorageBackend`` from a raw config mapping.

    Keyed off ``config["storage"]["backend"]``. Only ``"local"`` is supported;
    an unknown name raises ``ValueError`` naming the bad backend.
    """
    backend = config["storage"]["backend"]
    if backend == "local":
        # Thread the signed-URL TTL bounds from [storage.signed_url] so the
        # bounds are config-driven, never hardcoded at the construction site
        # ( F-SEC-3). Missing keys fall back to the backend's own
        # defaults, keeping the [storage.signed_url] table optional.
        signed_url_cfg = config["storage"].get("signed_url", {})
        ttl_kwargs: dict[str, int] = {}
        if "default_ttl_s" in signed_url_cfg:
            ttl_kwargs["default_ttl_s"] = int(signed_url_cfg["default_ttl_s"])
        if "max_ttl_s" in signed_url_cfg:
            ttl_kwargs["max_ttl_s"] = int(signed_url_cfg["max_ttl_s"])
        root_path = Path(config["storage"]["local"]["root_path"])
        # Anchor a relative root_path to the repo root so the backend works
        # regardless of the process CWD (e.g. ``just stack-up`` runs the API
        # via ``uv --directory apps/api``, making CWD=apps/api, not repo root).
        # An already-absolute path is honoured as-is (``Path.is_absolute()``).
        if not root_path.is_absolute():
            root_path = _repo_root() / root_path
        return LocalStorageBackend(
            root=root_path,
            **ttl_kwargs,
        )
    raise ValueError(f"unknown storage backend: {backend!r}; expected 'local'")


__all__ = [
    "LocalStorageBackend",
    "NotFound",
    "SignedToken",
    "StorageBackend",
    "_build_backend",
]
