"""Filesystem-backed StorageBackend implementation.

Bytes land under root/ on local disk. Signed URLs use HMAC-SHA256 with
the secret read from the AVSA_STORAGE_HMAC_SECRET environment variable
at signed-url issuance time (NOT at backend construction — unit tests that
never call signed_url may construct the backend without the env var).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TypedDict

from avsa_core.storage.errors import NotFound

_HMAC_SECRET_ENV = "AVSA_STORAGE_HMAC_SECRET"
_DEFAULT_TTL_S = 300
_MAX_TTL_S = 3600


class SignedToken(TypedDict):
    """Route-agnostic signed-access credential returned by signed_url."""

    token: str
    expires: int


class LocalStorageBackend:
    """Synchronous filesystem-backed storage backend.

    Conforms to avsa_core.storage.StorageBackend (structural).
    """

    def __init__(
        self,
        root: Path,
        default_ttl_s: int = _DEFAULT_TTL_S,
        max_ttl_s: int = _MAX_TTL_S,
    ) -> None:
        self._root = Path(root)
        self._default_ttl_s = int(default_ttl_s)
        self._max_ttl_s = int(max_ttl_s)
        self._hmac_secret: bytes | None = self._maybe_load_secret_from_env()

    # ------------------------------------------------------------------
    # Object I/O
    # ------------------------------------------------------------------

    def get_object(self, path: str) -> bytes:
        full = self._resolve_within_root(path)
        try:
            return full.read_bytes()
        except FileNotFoundError as exc:
            raise NotFound(path) from exc

    def put_object(self, path: str, data: bytes) -> None:
        full = self._resolve_within_root(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)

    def list_objects(self, prefix: str) -> Iterable[str]:
        """Yield every stored key matching prefix (relative to root, forward slashes).

        Mirrors GCS prefix semantics — the prefix is a string match, not a
        directory boundary. list_objects("a/") returns every key whose
        relative path starts with "a/"; list_objects("a/1") returns
        "a/1.bin" and "a/12.bin" if both exist. Returns an empty
        iterable when nothing matches.
        """
        results: list[str] = []

        if not self._within_root(prefix):
            return results

        base = self._root / prefix
        if base.is_file():
            results.append(prefix.rstrip("/"))
            return results
        if base.is_dir():
            for entry in base.rglob("*"):
                if entry.is_file():
                    rel = entry.relative_to(self._root).as_posix()
                    results.append(rel)
            return results

        parent_rel = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        parent_dir = self._root / parent_rel if parent_rel else self._root
        if not parent_dir.is_dir():
            return results
        for entry in parent_dir.rglob("*"):
            if not entry.is_file():
                continue
            rel = entry.relative_to(self._root).as_posix()
            if rel.startswith(prefix):
                results.append(rel)
        return results

    # ------------------------------------------------------------------
    # Signed URLs
    # ------------------------------------------------------------------

    def signed_url(self, path: str, ttl_s: int | None = None) -> SignedToken:
        secret = self._require_hmac_secret()
        effective_ttl = self._default_ttl_s if ttl_s is None else min(int(ttl_s), self._max_ttl_s)
        expires = int(time.time()) + effective_ttl
        token = self._compute_hmac(secret, path, expires)
        return SignedToken(token=token, expires=expires)

    def verify_signed_url(self, path: str, token: str, expires: int) -> bool:
        if expires < int(time.time()):
            return False
        if self._hmac_secret is None:
            return False
        expected = self._compute_hmac(self._hmac_secret, path, expires)
        return hmac.compare_digest(expected, token)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_within_root(self, path: str) -> Path:
        """Resolve path under the backend root, refusing traversal escapes."""
        full = (self._root / path).resolve()
        root = self._root.resolve()
        if not full.is_relative_to(root):
            raise NotFound(path)
        return full

    def _within_root(self, path: str) -> bool:
        """Non-raising containment check used by list_objects.

        Returns False when path resolves outside the root (so the caller can
        treat it as "matches nothing" rather than raising), True otherwise.
        """
        full = (self._root / path).resolve()
        return full.is_relative_to(self._root.resolve())

    @staticmethod
    def _compute_hmac(secret: bytes, path: str, expires: int) -> str:
        msg = f"{path}|{expires}".encode()
        return hmac.new(secret, msg, hashlib.sha256).hexdigest()

    @staticmethod
    def _maybe_load_secret_from_env() -> bytes | None:
        raw = os.environ.get(_HMAC_SECRET_ENV)
        if not raw:
            return None
        return raw.encode("utf-8")

    def _require_hmac_secret(self) -> bytes:
        if self._hmac_secret is None:
            raise RuntimeError(
                f"{_HMAC_SECRET_ENV} environment variable was not set when the "
                "LocalStorageBackend was constructed; cannot issue signed URLs. "
                "See scripts/README-acquire-fashion200k.md."
            )
        return self._hmac_secret
