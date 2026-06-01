"""Failing tests for — LocalStorageBackend + StorageBackend Protocol.

These tests are authored at step 2A-i (pre-implementation). The module under
test does not yet exist; we import inside a try/except so collection succeeds
and each test fails with a meaningful assertion failure (pytest.fail or
AssertionError / domain-exception assertion) rather than a collection-time
ImportError — per docs/agents/standards/testing.md § "Test-first protocol".

Module-location choice: avsa_core.storage (in-app package). Rationale captured
in the completion report's Pre-implementation Flags.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

try:
    from avsa_core.storage import NotFound  # domain error type
    from avsa_core.storage.local import LocalStorageBackend

    _STORAGE_AVAILABLE = True
except ImportError:
    _STORAGE_AVAILABLE = False


def _require_storage() -> None:
    if not _STORAGE_AVAILABLE:
        pytest.fail(
            "avsa_core.storage.local.LocalStorageBackend / avsa_core.storage.NotFound "
            "not implemented yet — expected during 2A-i pre-implementation. "
            "Implement per plans/061-071-real-catalog-and-dual-head-plan.md § Storage abstraction."
        )


def _make_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a LocalStorageBackend inside the test body (not a fixture) so a
    missing implementation surfaces as test FAILURE, not fixture ERROR."""
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret-default")
    return LocalStorageBackend(root=tmp_path)


def test_put_then_get_roundtrips_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _make_backend(tmp_path, monkeypatch)
    backend.put_object("foo/bar.bin", b"hello world")
    assert backend.get_object("foo/bar.bin") == b"hello world"


def test_get_object_raises_NotFound_on_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _make_backend(tmp_path, monkeypatch)
    with pytest.raises(NotFound):
        backend.get_object("does/not/exist.bin")


def test_list_objects_returns_prefixed_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _make_backend(tmp_path, monkeypatch)
    backend.put_object("a/1.bin", b"x")
    backend.put_object("a/2.bin", b"y")
    backend.put_object("b/3.bin", b"z")
    listed = set(backend.list_objects("a/"))
    assert listed == {"a/1.bin", "a/2.bin"}, (
        f"list_objects('a/') should return only the two 'a/' keys, got {listed!r}"
    )


def test_signed_url_includes_token_and_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F4 update: signed_url() now returns a route-agnostic mapping/dataclass
    # (not a URL string with /images/ baked in). The original assertion
    # "token=" / "expires=" in url was a string-search over the URL form; the
    # new assertion checks the structural contract — token + expires fields
    # are present and well-typed. Logic preserved (we still confirm both
    # fields exist after issuing a token); only the access shape changed.
    backend = _make_backend(tmp_path, monkeypatch)
    result = backend.signed_url("fashion200k/images/abc.jpg", ttl_s=60)
    token, expires = _token_and_expires(result)
    assert isinstance(token, str) and len(token) > 0, (
        f"signed_url must yield a non-empty token string; got {token!r}"
    )
    assert isinstance(expires, int) and expires > 0, (
        f"signed_url must yield a positive int expires (Unix seconds); got {expires!r}"
    )


def test_verify_signed_url_accepts_valid_unexpired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F4 update: read token/expires from the returned mapping/dataclass instead
    # of parsing them out of a URL query string. Verification semantics
    # unchanged — verify_signed_url(path, token, expires) still takes the same
    # arguments and still returns True for an unexpired, properly-signed pair.
    backend = _make_backend(tmp_path, monkeypatch)
    path = "fashion200k/images/abc.jpg"
    result = backend.signed_url(path, ttl_s=60)
    token, expires = _token_and_expires(result)
    assert backend.verify_signed_url(path, token, expires) is True


def test_verify_signed_url_rejects_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # F4 update: pull token from the structured return; the expired-rejection
    # check itself is unchanged (we still substitute a past expires and assert
    # the verifier returns False).
    backend = _make_backend(tmp_path, monkeypatch)
    path = "fashion200k/images/abc.jpg"
    result = backend.signed_url(path, ttl_s=60)
    token, _expires_unused = _token_and_expires(result)
    past = int(time.time()) - 10
    assert backend.verify_signed_url(path, token, past) is False


def test_verify_signed_url_rejects_tampered_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F4 update: pull token + expires from the structured return; the tamper
    # check (flip the last hex nibble and assert the verifier rejects) is
    # unchanged in spirit.
    backend = _make_backend(tmp_path, monkeypatch)
    path = "fashion200k/images/abc.jpg"
    result = backend.signed_url(path, ttl_s=60)
    token, expires = _token_and_expires(result)
    # Flip the last hex nibble — any single-byte change must invalidate the HMAC.
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert tampered != token, "test setup: tampered token must differ from original"
    assert backend.verify_signed_url(path, tampered, expires) is False


def test_verify_signed_url_rejects_wrong_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Path-binding: the object path is part of the signed payload, so a token
    # minted for one object must NOT authorise another — otherwise a single
    # leaked token would unlock the whole catalog. The /images route relies on
    # this (test_images_route.py asserts it end-to-end via 403); this pins the
    # property at the storage layer where the binding actually lives.
    backend = _make_backend(tmp_path, monkeypatch)
    result = backend.signed_url("fashion200k/images/path-a.jpg", ttl_s=60)
    token, expires = _token_and_expires(result)
    assert backend.verify_signed_url("fashion200k/images/path-b.jpg", token, expires) is False, (
        "a token signed for path-a must not verify for path-b (path-binding)"
    )


def test_signed_url_uses_hmac_secret_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F4 update: read token/expires via the structured-return helper instead
    # of _qs_value(). Cross-secret rejection logic is identical: signing under
    # two different secrets and asserting backend_a does not accept backend_b's
    # token. F5 also bears on this test: AVSA_STORAGE_HMAC_SECRET remains the
    # canonical env-var name (now constant in local.py, no longer mirrored in
    # config/avsa.toml).
    _require_storage()
    path = "fashion200k/images/abc.jpg"

    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "secret-alpha")
    backend_a = LocalStorageBackend(root=tmp_path)
    result_a = backend_a.signed_url(path, ttl_s=60)
    token_a, expires_a = _token_and_expires(result_a)

    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "secret-beta")
    backend_b = LocalStorageBackend(root=tmp_path)
    result_b = backend_b.signed_url(path, ttl_s=60)
    token_b, expires_b = _token_and_expires(result_b)

    # Different secrets must produce a distinguishable signed token+expires for
    # the same path. (Different secret OR different expiry both flip the HMAC.)
    assert token_a != token_b or expires_a != expires_b, (
        "Different HMAC secrets must produce a distinguishable signed token+expires; "
        f"got identical pair under both secrets ({token_a=}, {token_b=})"
    )

    # Strong cross-check: backend_a's verifier must REJECT backend_b's token.
    assert backend_a.verify_signed_url(path, token_b, expires_b) is False, (
        "A token signed with a different secret must not verify."
    )


def test_signed_url_no_longer_returns_route_prefixed_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4 defensive: signed_url must NOT return a string with a baked-in route.

    Catches regression to the old shape (`/images/{path}?token=...&expires=...`)
    where the storage layer leaked the HTTP route prefix. The route layer is
    now responsible for constructing the URL — storage returns structured
    (token, expires) only.
    """
    backend = _make_backend(tmp_path, monkeypatch)
    result = backend.signed_url("fashion200k/images/abc.jpg", ttl_s=60)
    if isinstance(result, str):
        assert not result.startswith("/images/"), (
            f"signed_url must not return a route-prefixed string (F4 information-hiding); "
            f"got {result!r}"
        )
        # Even a bare query-string return (e.g. '?token=...&expires=...') is
        # an information-hiding regression vs. the structured shape called for
        # by F4 — the route layer should never receive a string at all.
        pytest.fail(
            f"signed_url must return a structured object (mapping/dataclass with "
            f"token + expires), not a string; got {result!r}"
        )


def test_put_object_creates_parent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _make_backend(tmp_path, monkeypatch)
    # Deeply nested path; parent directories do not exist yet.
    backend.put_object("deeply/nested/path/x.bin", b"payload")
    assert backend.get_object("deeply/nested/path/x.bin") == b"payload"


# ----------------------------------------------------------------------------
# Path-traversal containment ( F-SEC-1)
# ----------------------------------------------------------------------------


def test_get_object_rejects_path_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_object must refuse a ``..`` escape with NotFound — never a file read.

    Defense-in-depth: even though the image route HMAC-verifies before reading,
    the storage layer itself must never read outside its root. A traversal
    attempt must collapse into NotFound (the route's existing 404 branch), NOT
    return bytes from outside the root and NOT raise a different error that
    could confirm the attempt.

    Uses a backend rooted at a *subdirectory* of tmp_path and plants a real
    secret in the parent dir, so ``../secret.txt`` resolves to an existing,
    readable file outside the root. Without the guard this would return the
    secret's bytes; the guard must turn it into NotFound instead. (A bare
    ``../../etc/passwd`` would raise NotFound even unguarded, by coincidental
    FileNotFoundError — this construction proves the guard, not luck.)
    """
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret-default")
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top-secret")
    backend = LocalStorageBackend(root=root)
    with pytest.raises(NotFound):
        backend.get_object("../secret.txt")


def test_put_object_rejects_path_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """put_object must refuse a ``..`` escape with NotFound and write nothing outside root."""
    backend = _make_backend(tmp_path, monkeypatch)
    # A sibling directory of the backend root — where the escape would land if
    # the guard were absent (tmp_path/../escape.bin).
    escaped_target = tmp_path.parent / "escape.bin"
    assert not escaped_target.exists(), "test setup: escape target must not pre-exist"

    with pytest.raises(NotFound):
        backend.put_object("../escape.bin", b"x")

    assert not escaped_target.exists(), (
        "put_object with a traversal path must not write outside the backend root; "
        f"found a file at {escaped_target}"
    )


def test_list_objects_does_not_escape_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """list_objects with a ``..`` prefix must not enumerate keys outside the root."""
    backend = _make_backend(tmp_path, monkeypatch)
    backend.put_object("inside.bin", b"x")
    # Drop a file in the parent dir that an escaping rglob could otherwise see.
    outside = tmp_path.parent / "outside.bin"
    outside.write_bytes(b"secret")
    try:
        listed = list(backend.list_objects("../"))
    finally:
        outside.unlink(missing_ok=True)
    assert listed == [], f"list_objects('../') must not escape the backend root; got {listed!r}"


# ----------------------------------------------------------------------------
# Bounded signed-URL TTL ( F-SEC-3)
# ----------------------------------------------------------------------------


def test_signed_url_uses_default_ttl_when_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """signed_url() with no ttl_s must expire at now + the configured default TTL."""
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret-default")
    backend = LocalStorageBackend(root=tmp_path, default_ttl_s=300, max_ttl_s=3600)
    before = int(time.time())
    result = backend.signed_url("fashion200k/images/abc.jpg")
    after = int(time.time())
    _token, expires = _token_and_expires(result)
    assert before + 300 <= expires <= after + 300, (
        f"signed_url() with no ttl_s must use the default TTL (300s); "
        f"expires={expires}, window=[{before + 300}, {after + 300}]"
    )


def test_signed_url_clamps_ttl_above_max(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller-supplied ttl_s above max_ttl_s must be clamped down to max_ttl_s."""
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret-default")
    backend = LocalStorageBackend(root=tmp_path, default_ttl_s=300, max_ttl_s=3600)
    before = int(time.time())
    # Request a 1-day TTL; it must be clamped to the 3600s ceiling.
    result = backend.signed_url("fashion200k/images/abc.jpg", ttl_s=86400)
    after = int(time.time())
    _token, expires = _token_and_expires(result)
    assert before + 3600 <= expires <= after + 3600, (
        f"signed_url() ttl_s above max must clamp to max_ttl_s (3600s); "
        f"expires={expires}, window=[{before + 3600}, {after + 3600}]"
    )


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _token_and_expires(result: object) -> tuple[str, int]:
    """Pull (token, expires) from whatever shape signed_url() returns.

    Post-F4 the canonical shape is a mapping or dataclass with `token` and
    `expires` keys/attributes. We try mapping access first, then attribute
    access, so the tests stay agnostic to the impl's dataclass-vs-TypedDict
    choice — and fail clearly when neither shape works (instead of raising
    a KeyError / AttributeError that obscures the contract).
    """
    if isinstance(result, dict):
        if "token" not in result or "expires" not in result:
            pytest.fail(f"signed_url() returned a dict missing token/expires keys: {result!r}")
        return str(result["token"]), int(result["expires"])
    if hasattr(result, "token") and hasattr(result, "expires"):
        return str(result.token), int(result.expires)  # type: ignore[attr-defined]
    pytest.fail(
        "signed_url() must return a mapping with token/expires keys OR an object "
        f"with .token/.expires attributes (per F4); got {type(result).__name__}: {result!r}"
    )
