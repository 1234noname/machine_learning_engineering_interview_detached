"""Failing tests for — the ``fashion200k`` seeder source loader.

Authored at step 2A-i (pre-implementation). The module under test
(``avsa_data.catalog_fashion200k``) does not yet exist; we import inside a
``try/except ImportError`` so collection succeeds and every test fails with a
meaningful *assertion-shape* failure (``pytest.fail`` / ``AssertionError`` /
domain-exception assertion) rather than a collection-time ``ImportError`` —
per docs/agents/standards/testing.md § "Test-first protocol".

Module-location choice — ``avsa_data.catalog_fashion200k`` (data-prep library):
    The loader MUST reuse ``avsa_data.embedding_pipeline.load_embedding_artifact``
    (issue brief: "do NOT fork a second reader") and the ``avsa_core.storage``
    ``LocalStorageBackend``. So the loader does not live next to
    ``synthetic_product`` in the repo-root
    ``machine_learning_engineering_interview.catalog_seed`` module. It is placed
    alongside its siblings ``acquisition`` / ``fashion200k_metadata`` /
    ``embedding_pipeline`` in the ``avsa_data`` package, mirroring the Phase-3
    offline-pipeline placement. ``scripts/seed-catalog.py`` imports it as an
    installed package (``import avsa_data``) via uv's editable path dependency.

    The ``copy_rows`` parameterization and the source-dispatch function stay in
    the repo-root ``catalog_seed`` module (where ``copy_rows`` /
    ``synthetic_product`` already live); their tests are in
    ``tests/test_catalog_seed.py``.

Public surface under test (expected after implementation):
    ``fashion200k_rows(manifest_path, artifact_dir, *, backend, limit=None)``
    → ``Iterator[dict[str, Any]]`` — one row per manifest item, joined to the
    #062 artifact embeddings by ``id``.

missing-artifact-id behaviour chosen: **raise a domain error**
(``Fashion200kSeedError``). A manifest id absent from the artifact is a
build-time inconsistency between #061 (manifest) and #062 (embeddings); seeding
half a catalog silently would ship a corpus that disagrees with its own
manifest. We fail loud. (The test asserts the raise; the implementation must
expose this error type from the module.)

colour vocabulary + fallback:
    keyword match against {black, white, navy, red, green, beige, grey/gray,
    blue, pink, brown, yellow, purple, orange, gold, silver}; fallback default
    when no colour word appears: ``"multicolour"`` (colour is NOT NULL).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

try:
    from avsa_data.catalog_fashion200k import (
        Fashion200kSeedError,
        fashion200k_rows,
    )

    _LOADER_AVAILABLE = True
except ImportError:
    _LOADER_AVAILABLE = False

try:
    from avsa_core.storage.local import LocalStorageBackend

    _STORAGE_AVAILABLE = True
except ImportError:
    _STORAGE_AVAILABLE = False


def _require_loader() -> None:
    if not _LOADER_AVAILABLE:
        pytest.fail(
            "avsa_data.catalog_fashion200k (fashion200k_rows / Fashion200kSeedError) "
            "not implemented yet — expected during 2A-i pre-implementation. "
            "Implement per issues/063-fashion200k-seeder-loader.md and "
            "plans/061-071-real-catalog-and-dual-head-plan.md § Phase 3."
        )


def _require_storage() -> None:
    if not _STORAGE_AVAILABLE:
        pytest.fail(
            "avsa_core.storage.local.LocalStorageBackend not importable — the "
            "fashion200k loader reads the #062 artifact through a StorageBackend "
            "(landed in )."
        )


# ----------------------------------------------------------------------------
# Fixtures — a small self-describing manifest + a matching #062 artifact dir.
# ----------------------------------------------------------------------------

# Item ids deliberately end in ``.jpeg`` to mirror the real
# evals/catalog/fashion200k/manifest.json — the acquired storage key appends
# ``.jpg`` to the *whole* id (see avsa_data.acquisition.acquire_image, which
# writes ``fashion200k/images/{item_id}.jpg``).
_ITEMS: list[dict[str, str]] = [
    {
        "id": "women/dresses/casual/100/100_1.jpeg",
        "category": "dress",
        "title": "black knit midi dress",
        "source_url": "https://example.invalid/100.jpeg",
        "split": "train",
    },
    {
        "id": "women/tops/blouses/200/200_2.jpeg",
        "category": "top",
        "title": "silk blouse",  # no colour word → fallback default expected
        "source_url": "https://example.invalid/200.jpeg",
        "split": "train",
    },
    {
        "id": "women/skirts/mini/300/300_3.jpeg",
        "category": "skirt",
        "title": "navy pleated mini skirt",
        "source_url": "https://example.invalid/300.jpeg",
        "split": "test",
    },
]

_IMAGE_DIM = 768
_TEXT_DIM = 512


def _write_manifest(path: Path, items: list[dict[str, str]]) -> None:
    """Write a rev2 self-describing manifest JSON at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "seed": 17,
        "criteria": {"count": len(items), "selection": "fixture"},
        "dataset_version": "fashion200k-v1.0",
        "items": items,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seeded_artifact(
    backend: Any,
    artifact_dir: Path,
    ids: list[str],
) -> None:
    """Write a #062-format artifact (``embeddings.jsonl`` + ``manifest.json``).

    Distinct per-id vectors (id-hash-seeded floats) so a join that crosses ids
    would be detectable; lengths are the contract that matters most.
    """
    artifact_dir_str = str(artifact_dir).replace("\\", "/")

    lines: list[str] = []
    for idx, item_id in enumerate(ids):
        row = {
            "id": item_id,
            "image_embedding": [float((idx + 1) % 7) / 7.0] * _IMAGE_DIM,
            "text_embedding": [float((idx + 2) % 5) / 5.0] * _TEXT_DIM,
        }
        lines.append(json.dumps(row))
    backend.put_object(
        f"{artifact_dir_str}/embeddings.jsonl",
        ("\n".join(lines) + "\n").encode("utf-8"),
    )

    manifest = {
        "model_version_image": "vit-b-16@2026-05-01",
        "model_version_text": "minilm-l6-v2@2026-05-01",
        "image_dim": _IMAGE_DIM,
        "text_dim": _TEXT_DIM,
        "item_count": len(ids),
        "content_hash": "fixture-hash",
        "generated_at": "2026-05-25T00:00:00Z",
    }
    backend.put_object(
        f"{artifact_dir_str}/manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def _setup(tmp_path: Path, items: list[dict[str, str]] | None = None) -> tuple[Path, Path, Any]:
    """Build (manifest_path, artifact_dir, backend) for a row-loader call."""
    _require_storage()
    chosen = items if items is not None else _ITEMS
    backend = LocalStorageBackend(root=tmp_path)
    manifest_path = tmp_path / "evals" / "fashion200k" / "manifest.json"
    _write_manifest(manifest_path, chosen)
    artifact_dir = Path("data/embeddings/fixture-hash")
    _seeded_artifact(backend, artifact_dir, [item["id"] for item in chosen])
    return manifest_path, artifact_dir, backend


def _rows(tmp_path: Path, **kwargs: Any) -> list[dict[str, Any]]:
    manifest_path, artifact_dir, backend = _setup(tmp_path)
    return list(fashion200k_rows(manifest_path, artifact_dir, backend=backend, **kwargs))


# ----------------------------------------------------------------------------
# All NOT NULL columns + text_embedding populated, non-empty.
# ----------------------------------------------------------------------------

# catalog.products NOT NULL set + the nullable text_embedding the fashion200k
# path always supplies (specs/db/catalog.sql).
_REQUIRED_COLUMNS = (
    "title",
    "category",
    "colour",
    "formality",
    "occasion",
    "price_cents",
    "image_url",
    "embedding",
    "text_embedding",
)


def test_fashion200k_rows_yields_all_not_null_columns(tmp_path: Path) -> None:
    _require_loader()
    rows = _rows(tmp_path)
    assert len(rows) == len(_ITEMS), (
        f"expected one row per manifest item ({len(_ITEMS)}); got {len(rows)}"
    )
    for i, row in enumerate(rows):
        for key in _REQUIRED_COLUMNS:
            assert key in row, f"row {i} missing required column {key!r}; got {sorted(row)!r}"
        for key in ("title", "category", "colour", "formality", "occasion", "image_url"):
            value = row[key]
            assert isinstance(value, str) and value, (
                f"row {i} column {key!r} must be a non-empty string; got {value!r}"
            )


def test_fashion200k_rows_title_and_category_from_manifest(tmp_path: Path) -> None:
    _require_loader()
    rows = _rows(tmp_path)
    by_title = {row["title"]: row for row in rows}
    assert "black knit midi dress" in by_title, (
        f"title must be taken verbatim from the manifest; got titles {sorted(by_title)!r}"
    )
    assert by_title["black knit midi dress"]["category"] == "dress", (
        "category must be taken verbatim from the manifest item"
    )


def test_fashion200k_embedding_dims(tmp_path: Path) -> None:
    _require_loader()
    rows = _rows(tmp_path)
    for i, row in enumerate(rows):
        assert len(row["embedding"]) == _IMAGE_DIM, (
            f"row {i}: image embedding must be {_IMAGE_DIM}-d; got {len(row['embedding'])}"
        )
        assert len(row["text_embedding"]) == _TEXT_DIM, (
            f"row {i}: text embedding must be {_TEXT_DIM}-d; got {len(row['text_embedding'])}"
        )


def test_fashion200k_embedding_joined_by_id(tmp_path: Path) -> None:
    """Each row's image embedding is the artifact vector for *its own* id.

    The fixture writes a distinct constant per artifact position; a join that
    crosses ids (e.g. zips manifest order against artifact order without keying
    on id) would attach the wrong vector. We assert the dress row (manifest
    position 0) carries the position-0 artifact image vector value (1/7).
    """
    _require_loader()
    rows = _rows(tmp_path)
    by_title = {row["title"]: row for row in rows}
    dress = by_title["black knit midi dress"]
    expected_value = pytest.approx(1.0 / 7.0)
    assert dress["embedding"][0] == expected_value, (
        "the dress row must carry the artifact image_embedding keyed on its own "
        f"id; got first component {dress['embedding'][0]!r}"
    )


# ----------------------------------------------------------------------------
# colour derived from the title; fallback when no colour word present.
# ----------------------------------------------------------------------------


def test_fashion200k_colour_derived_from_title(tmp_path: Path) -> None:
    _require_loader()
    rows = _rows(tmp_path)
    by_title = {row["title"]: row for row in rows}
    assert by_title["black knit midi dress"]["colour"] == "black", (
        "colour must be derived from the first colour word in the title"
    )
    assert by_title["navy pleated mini skirt"]["colour"] == "navy", (
        "colour must be derived from the first colour word in the title"
    )


def test_fashion200k_colour_fallback_when_no_colour_word(tmp_path: Path) -> None:
    _require_loader()
    rows = _rows(tmp_path)
    by_title = {row["title"]: row for row in rows}
    # "silk blouse" carries no colour word — colour is NOT NULL, so the loader
    # must emit the documented fallback default rather than "" or None.
    colour = by_title["silk blouse"]["colour"]
    assert colour == "multicolour", (
        f"a title with no colour word must fall back to 'multicolour'; got {colour!r}"
    )


# ----------------------------------------------------------------------------
# image_url — tokenless stable proxy path under /images/.
# ----------------------------------------------------------------------------


def test_fashion200k_image_url_is_tokenless_proxy_path(tmp_path: Path) -> None:
    _require_loader()
    rows = _rows(tmp_path)
    for i, row in enumerate(rows):
        url = row["image_url"]
        assert isinstance(url, str) and url, f"row {i} image_url must be a non-empty string"
        assert url.startswith("/images/"), (
            f"row {i} image_url must be a /images/ proxy path; got {url!r}"
        )
        assert "token=" not in url, (
            f"row {i} image_url must NOT embed a signing token (read-time concern, "
            f"#066); got {url!r}"
        )
        assert "expires=" not in url, f"row {i} image_url must NOT embed an expiry; got {url!r}"


def test_fashion200k_image_url_contains_storage_key_for_id(tmp_path: Path) -> None:
    """The proxy path embeds the acquisition storage key ``fashion200k/images/{id}.jpg``."""
    _require_loader()
    manifest_path, artifact_dir, backend = _setup(tmp_path)
    rows = list(fashion200k_rows(manifest_path, artifact_dir, backend=backend))
    by_title = {row["title"]: row for row in rows}
    dress = by_title["black knit midi dress"]
    expected_key = "fashion200k/images/women/dresses/casual/100/100_1.jpeg.jpg"
    assert expected_key in dress["image_url"], (
        "image_url must contain the acquisition storage key "
        f"{expected_key!r} for the item id; got {dress['image_url']!r}"
    )


def test_fashion200k_image_url_uses_source_url_when_cdn_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prod CDN policy: ``AVSA_CATALOG_SERVE_SOURCE_URLS=1`` stores the manifest
    ``source_url`` (the original CDN link) as image_url instead of the /images/
    proxy path — so prod serves images from the source CDN without re-hosting."""
    _require_loader()
    monkeypatch.setenv("AVSA_CATALOG_SERVE_SOURCE_URLS", "1")
    manifest_path, artifact_dir, backend = _setup(tmp_path)
    rows = list(fashion200k_rows(manifest_path, artifact_dir, backend=backend))
    by_title = {row["title"]: row for row in rows}
    dress = by_title["black knit midi dress"]
    assert dress["image_url"] == "https://example.invalid/100.jpeg", (
        "with the CDN policy enabled, image_url must be the manifest source_url; "
        f"got {dress['image_url']!r}"
    )


# ----------------------------------------------------------------------------
# price_cents — deterministic, positive, plausible range.
# ----------------------------------------------------------------------------

_PRICE_MIN_CENTS = 1500
_PRICE_MAX_CENTS = 25000


def test_fashion200k_price_cents_positive_and_in_range(tmp_path: Path) -> None:
    _require_loader()
    rows = _rows(tmp_path)
    for i, row in enumerate(rows):
        price = row["price_cents"]
        assert isinstance(price, int), f"row {i} price_cents must be an int; got {type(price)!r}"
        assert price > 0, f"row {i} price_cents must be positive; got {price}"
        assert _PRICE_MIN_CENTS <= price <= _PRICE_MAX_CENTS, (
            f"row {i} price_cents {price} must sit in the apparel range "
            f"[{_PRICE_MIN_CENTS}, {_PRICE_MAX_CENTS}]"
        )


def test_fashion200k_price_cents_is_deterministic(tmp_path: Path) -> None:
    _require_loader()
    first = _rows(tmp_path)
    second = _rows(tmp_path)
    first_prices = {row["title"]: row["price_cents"] for row in first}
    second_prices = {row["title"]: row["price_cents"] for row in second}
    assert first_prices == second_prices, (
        "price_cents must be deterministic per item id across two loader calls; "
        f"{first_prices!r} != {second_prices!r}"
    )


# ----------------------------------------------------------------------------
# formality / occasion — derived, non-empty.
# ----------------------------------------------------------------------------


def test_fashion200k_formality_occasion_nonempty(tmp_path: Path) -> None:
    _require_loader()
    rows = _rows(tmp_path)
    for i, row in enumerate(rows):
        formality = row["formality"]
        occasion = row["occasion"]
        assert isinstance(formality, str) and formality, (
            f"row {i} formality must be a non-empty derived string; got {formality!r}"
        )
        assert isinstance(occasion, str) and occasion, (
            f"row {i} occasion must be a non-empty derived string; got {occasion!r}"
        )


# ----------------------------------------------------------------------------
# Row count bounded by limit / seed_count.
# ----------------------------------------------------------------------------


def test_fashion200k_row_count_respects_limit(tmp_path: Path) -> None:
    _require_loader()
    manifest_path, artifact_dir, backend = _setup(tmp_path)
    rows = list(fashion200k_rows(manifest_path, artifact_dir, backend=backend, limit=2))
    assert len(rows) == 2, (
        f"limit=2 must cap the yielded rows at 2 (manifest has {len(_ITEMS)}); got {len(rows)}"
    )


def test_fashion200k_no_limit_yields_full_manifest(tmp_path: Path) -> None:
    _require_loader()
    rows = _rows(tmp_path)
    assert len(rows) == len(_ITEMS), (
        f"with no limit the loader must yield every manifest item ({len(_ITEMS)}); got {len(rows)}"
    )


# ----------------------------------------------------------------------------
# missing-artifact-id behaviour — raise a clear domain error.
# ----------------------------------------------------------------------------


def test_fashion200k_missing_artifact_id_raises(tmp_path: Path) -> None:
    _require_loader()
    _require_storage()
    backend = LocalStorageBackend(root=tmp_path)
    manifest_path = tmp_path / "evals" / "fashion200k" / "manifest.json"
    _write_manifest(manifest_path, _ITEMS)
    artifact_dir = Path("data/embeddings/fixture-hash")
    # Artifact deliberately MISSING the third item's id → join cannot resolve.
    _seeded_artifact(backend, artifact_dir, [_ITEMS[0]["id"], _ITEMS[1]["id"]])

    with pytest.raises(Fashion200kSeedError) as excinfo:
        list(fashion200k_rows(manifest_path, artifact_dir, backend=backend))
    message = str(excinfo.value)
    assert _ITEMS[2]["id"] in message, (
        "the error must name the manifest id that is absent from the artifact; "
        f"got message {message!r}"
    )


def test_fashion200k_rows_round_trip_json_serialisable(tmp_path: Path) -> None:
    """Rows minus the big vectors must JSON round-trip (COPY text-format safety)."""
    _require_loader()
    rows = _rows(tmp_path)
    for row in rows:
        slim = {k: v for k, v in row.items() if k not in ("embedding", "text_embedding")}
        assert json.loads(json.dumps(slim)) == slim
