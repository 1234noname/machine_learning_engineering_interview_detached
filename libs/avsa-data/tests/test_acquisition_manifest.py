"""Failing tests for — Fashion200k acquisition pure functions.

These tests are authored at step 2A-i (pre-implementation). The module under
test does not yet exist; we import inside a try/except so collection succeeds
and each test fails with a meaningful assertion failure rather than a
collection-time ImportError — per docs/agents/standards/testing.md.

Module-location choice: avsa_data.acquisition (in-app module, imported by the
CLI entrypoint scripts/acquire-fashion200k.py via sys.path append — the same
pattern scripts/seed-catalog.py already uses). Rationale captured in the
completion report's Pre-implementation Flags.

The acquisition surface under test:
    - select_subset(seed, criteria, available_ids) -> list[str]
    - acquire_image(item_id, source_url, backend) -> AcquisitionResult
    - write_manifest(path, items, seed, criteria, dataset_version) -> None

Schema note (revision cycle 2): ``write_manifest`` now takes a rich
``items`` list (each entry has ``id``, ``category``, ``title``,
``source_url``, ``split``) rather than a bare ``ids`` list. This
self-describing manifest lets a reproducer fetch URLs without needing
the full Fashion200k labels file. rev2 brief.

acquire_image is async (httpx.AsyncClient is the only sanctioned client per
python-agent.md). The fetch boundary is mocked with respx (already in
apps/api/pyproject.toml dev deps); no live network calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

try:
    from avsa_data.acquisition import (
        AcquisitionResult,
        acquire_image,
        select_subset,
        write_manifest,
    )

    _ACQ_AVAILABLE = True
except ImportError:
    _ACQ_AVAILABLE = False

try:
    from avsa_core.storage import NotFound
    from avsa_core.storage.local import LocalStorageBackend

    _STORAGE_AVAILABLE = True
except ImportError:
    _STORAGE_AVAILABLE = False


def _require_acq() -> None:
    if not _ACQ_AVAILABLE:
        pytest.fail(
            "avsa_data.acquisition (select_subset / acquire_image / write_manifest / "
            "AcquisitionResult) not implemented yet — expected during 2A-i "
            "pre-implementation. Implement per "
            "plans/061-071-real-catalog-and-dual-head-plan.md § Phase 1."
        )


def _require_storage() -> None:
    if not _STORAGE_AVAILABLE:
        pytest.fail(
            "avsa_core.storage.local.LocalStorageBackend not implemented yet — "
            "acquire_image tests depend on a StorageBackend implementation."
        )


# ----------------------------------------------------------------------------
# select_subset
# ----------------------------------------------------------------------------


def _universe(n: int = 200_000) -> list[str]:
    """A synthetic universe of item IDs (Fashion200k-style zero-padded strings)."""
    return [f"item_{i:07d}" for i in range(n)]


def test_select_subset_is_deterministic_for_seed() -> None:
    _require_acq()
    universe = _universe(50_000)
    criteria = {"count": 1000}
    first = select_subset(seed=42, criteria=criteria, available_ids=universe)
    second = select_subset(seed=42, criteria=criteria, available_ids=universe)
    assert first == second, (
        "select_subset must be deterministic: same (seed, criteria, universe) "
        "must produce the same ordered output across calls."
    )


def test_select_subset_respects_count_bound() -> None:
    _require_acq()
    universe = _universe(200_000)
    selected = select_subset(seed=7, criteria={"count": 12_000}, available_ids=universe)
    assert len(selected) == 12_000, (
        f"select_subset with count=12000 should return exactly 12000 ids; got {len(selected)}"
    )
    # And every selected id must come from the universe.
    universe_set = set(universe)
    assert set(selected).issubset(universe_set), (
        "select_subset must return ids drawn from the supplied universe."
    )


def test_select_subset_count_within_phase2_bounds() -> None:
    """Default count must be in the [10_000, 20_000] Phase-2 envelope per ADR-0007."""
    _require_acq()
    universe = _universe(200_000)
    # No 'count' key → implementation must apply its default.
    selected = select_subset(seed=0, criteria={}, available_ids=universe)
    assert 10_000 <= len(selected) <= 20_000, (
        f"Default subset size must be in Phase-2 bounds [10000, 20000]; got {len(selected)}"
    )


def test_select_subset_changes_with_seed() -> None:
    _require_acq()
    universe = _universe(50_000)
    criteria = {"count": 1000}
    a = select_subset(seed=1, criteria=criteria, available_ids=universe)
    b = select_subset(seed=2, criteria=criteria, available_ids=universe)
    # Probability of two random 1000-of-50000 samples being identical is ~0;
    # if they match, the seed is being ignored.
    assert a != b, (
        "Different seeds must produce different subsets (probabilistic; if this "
        "fires, the implementation is ignoring the seed)."
    )


def test_select_subset_rejects_count_below_one() -> None:
    """count < 1 is invalid input → ValueError (fail fast, not a silent empty subset)."""
    _require_acq()
    with pytest.raises(ValueError, match=r"(?i)count"):
        select_subset(seed=0, criteria={"count": 0}, available_ids=_universe(10))


def test_select_subset_returns_all_when_count_exceeds_universe() -> None:
    """count larger than the universe returns every id (sorted), not an error."""
    _require_acq()
    universe = _universe(50)
    selected = select_subset(seed=3, criteria={"count": 1000}, available_ids=universe)
    assert selected == sorted(universe), (
        f"count > len(available_ids) must return all ids sorted; "
        f"got {len(selected)} of {len(universe)}"
    )


# ----------------------------------------------------------------------------
# write_manifest
# ----------------------------------------------------------------------------


def _three_items() -> list[dict[str, object]]:
    """Three rich items in non-sorted order, used by multiple manifest tests."""
    return [
        {
            "id": "item_0000003",
            "category": "dress",
            "title": "green floral midi dress",
            "source_url": "https://example.test/3.jpeg",
            "split": "test",
        },
        {
            "id": "item_0000001",
            "category": "dress",
            "title": "red contemporary cami dress",
            "source_url": "https://example.test/1.jpeg",
            "split": "train",
        },
        {
            "id": "item_0000002",
            "category": "jacket",
            "title": "blue denim jacket",
            "source_url": "https://example.test/2.jpeg",
            "split": "train",
        },
    ]


def test_write_manifest_shape_includes_required_keys(tmp_path: Path) -> None:
    _require_acq()
    manifest_path = tmp_path / "manifest.json"
    write_manifest(
        path=manifest_path,
        items=_three_items(),
        seed=42,
        criteria={"count": 3, "category": "dresses"},
        dataset_version="fashion200k-v1",
    )
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    for key in ("seed", "criteria", "dataset_version", "items"):
        assert key in raw, f"manifest JSON missing required key {key!r}; got keys {list(raw)}"
    # Rev2 schema: bare ``ids`` is gone — items now carries id + rich fields.
    assert "ids" not in raw, (
        f"manifest JSON should not carry the legacy 'ids' key in rev2 schema; got keys {list(raw)}"
    )
    assert raw["seed"] == 42
    assert raw["criteria"] == {"count": 3, "category": "dresses"}
    assert raw["dataset_version"] == "fashion200k-v1"


def test_write_manifest_items_sorted_by_id(tmp_path: Path) -> None:
    _require_acq()
    manifest_path = tmp_path / "manifest.json"
    items = _three_items()
    write_manifest(
        path=manifest_path,
        items=items,
        seed=42,
        criteria={"count": 3},
        dataset_version="fashion200k-v1",
    )
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    ids_in_order = [entry["id"] for entry in raw["items"]]
    assert ids_in_order == sorted(entry["id"] for entry in items), (
        f"manifest items must be sorted ascending by id; got {ids_in_order!r}"
    )


def test_write_manifest_preserves_item_values_round_trip(tmp_path: Path) -> None:
    _require_acq()
    manifest_path = tmp_path / "manifest.json"
    items = _three_items()
    write_manifest(
        path=manifest_path,
        items=items,
        seed=42,
        criteria={"count": 3},
        dataset_version="fashion200k-v1",
    )
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id_input = {entry["id"]: entry for entry in items}
    by_id_output = {entry["id"]: entry for entry in raw["items"]}
    assert by_id_input.keys() == by_id_output.keys(), (
        f"round-trip lost ids; in={sorted(by_id_input)} out={sorted(by_id_output)}"
    )
    for item_id, written in by_id_output.items():
        original = by_id_input[item_id]
        for field in ("id", "category", "title", "source_url", "split"):
            assert written[field] == original[field], (
                f"item {item_id!r} field {field!r} drifted: "
                f"in={original[field]!r} out={written[field]!r}"
            )


def test_write_manifest_rejects_items_missing_id(tmp_path: Path) -> None:
    _require_acq()
    manifest_path = tmp_path / "manifest.json"
    bad_items: list[dict[str, object]] = [
        {
            "id": "item_0000001",
            "category": "dress",
            "title": "red dress",
            "source_url": "https://example.test/1.jpeg",
            "split": "train",
        },
        {
            # Missing 'id' — the sort key cannot be computed; defensive raise.
            "category": "jacket",
            "title": "blue jacket",
            "source_url": "https://example.test/2.jpeg",
            "split": "test",
        },
    ]
    with pytest.raises(ValueError, match=r"(?i)id"):
        write_manifest(
            path=manifest_path,
            items=bad_items,
            seed=42,
            criteria={"count": 2},
            dataset_version="fashion200k-v1",
        )


# ----------------------------------------------------------------------------
# acquire_image
# ----------------------------------------------------------------------------


def _make_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a backend inside the test body so a missing implementation
    surfaces as test FAILURE, not fixture ERROR."""
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    return LocalStorageBackend(root=tmp_path)


async def test_acquire_image_skips_when_already_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_acq()
    backend = _make_backend(tmp_path, monkeypatch)
    item_id = "item_0000042"
    backend.put_object(f"fashion200k/images/{item_id}.jpg", b"already-here")

    # respx will assert (via assert_all_called=True default off) that no real
    # HTTP request is made — we route any unexpected call to a 500 and check
    # the result is skipped_existing rather than fetched.
    with respx.mock(assert_all_called=False) as router:
        # Register a catch-all that, if hit, would produce 500 (which would
        # surface as 'failed' — not 'skipped_existing' — and fail the test).
        router.get(url__regex=r".*").respond(500)

        result = await acquire_image(
            item_id=item_id,
            source_url="https://example.invalid/fashion200k/abc.jpg",
            backend=backend,
        )

        # No HTTP call should have been made.
        assert router.calls.call_count == 0, (
            f"acquire_image must not fetch when the object already exists; "
            f"made {router.calls.call_count} HTTP calls."
        )

    assert result == AcquisitionResult.skipped_existing, (
        f"Expected AcquisitionResult.skipped_existing; got {result!r}"
    )

    # And the existing bytes must remain untouched.
    assert backend.get_object(f"fashion200k/images/{item_id}.jpg") == b"already-here"


async def test_acquire_image_fetches_and_writes_on_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_acq()
    backend = _make_backend(tmp_path, monkeypatch)
    item_id = "item_0000099"
    source_url = "https://example.invalid/fashion200k/xyz.jpg"
    payload = b"\xff\xd8\xff\xe0" + b"fake-jpeg-bytes"  # JPEG SOI marker prefix

    with respx.mock() as router:
        route = router.get(source_url).respond(200, content=payload)

        result = await acquire_image(
            item_id=item_id,
            source_url=source_url,
            backend=backend,
        )

        assert route.called, "acquire_image must issue an HTTP GET to source_url on a miss."

    assert result == AcquisitionResult.fetched, (
        f"Expected AcquisitionResult.fetched on a successful download; got {result!r}"
    )
    written = backend.get_object(f"fashion200k/images/{item_id}.jpg")
    assert written == payload, (
        "acquire_image must write the fetched bytes verbatim to "
        "fashion200k/images/<item_id>.jpg via the StorageBackend."
    )


async def test_acquire_image_returns_failed_on_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_acq()
    backend = _make_backend(tmp_path, monkeypatch)
    item_id = "item_0000404"
    source_url = "https://example.invalid/fashion200k/missing.jpg"

    with respx.mock() as router:
        router.get(source_url).respond(404)

        result = await acquire_image(
            item_id=item_id,
            source_url=source_url,
            backend=backend,
        )

    assert result == AcquisitionResult.failed, (
        f"Expected AcquisitionResult.failed when the source returns HTTP 404; got {result!r}"
    )
    # Nothing should have been written to the backend.
    with pytest.raises(NotFound):
        backend.get_object(f"fashion200k/images/{item_id}.jpg")


async def test_acquire_image_returns_failed_on_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport-level error (connection refused / timeout) → failed, no write.

    Distinct from the non-2xx path: acquire_image's ``except httpx.HTTPError``
    must catch the request itself raising, not only a bad status code.
    """
    _require_acq()
    backend = _make_backend(tmp_path, monkeypatch)
    item_id = "item_0000500"
    source_url = "https://example.invalid/fashion200k/boom.jpg"

    with respx.mock() as router:
        router.get(source_url).mock(side_effect=httpx.ConnectError("connection refused"))
        result = await acquire_image(item_id=item_id, source_url=source_url, backend=backend)

    assert result == AcquisitionResult.failed, (
        f"a transport error must yield failed (not raise); got {result!r}"
    )
    with pytest.raises(NotFound):
        backend.get_object(f"fashion200k/images/{item_id}.jpg")
