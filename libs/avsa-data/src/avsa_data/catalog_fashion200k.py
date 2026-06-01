"""Fashion200k seeder source loader.

Joins the self-describing manifest (item metadata) to the embedding
artifact (pre-computed image + text vectors) and yields one row per manifest
item, shaped for ``catalog.products`` (specs/db/catalog.sql) and bulk-loaded by
``machine_learning_engineering_interview.catalog_seed.copy_rows``.

Placement — alongside its siblings ``acquisition`` / ``fashion200k_metadata`` /
``embedding_pipeline`` in the ``avsa_data`` data-prep library: the loader reuses
``avsa_data.embedding_pipeline.load_embedding_artifact`` and the
``avsa_core.storage.StorageBackend`` abstraction. ``scripts/seed-catalog.py``
imports it as an installed package (``import avsa_data``), via uv's editable
workspace-local path dependency.

Derivations (no ground truth in Fashion200k for these apparel attributes):

- ``colour`` — first colour word found in the lowercased title; falls back to
  ``"multicolour"`` (the column is NOT NULL).
- ``price_cents`` — deterministic per item id (SHA-256 of the id, mod into the
  apparel range), so reseeding is reproducible.
- ``formality`` / ``occasion`` — a category→value heuristic map with a default
  for unmapped categories, so both columns are always non-empty.

A manifest id absent from the artifact is a build-time inconsistency between
and; rather than seed half a catalog that disagrees with its own
manifest, the loader raises ``Fashion200kSeedError`` naming the missing id.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING, Any

from avsa_data.embedding_pipeline import load_embedding_artifact

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from avsa_core.storage import StorageBackend


class Fashion200kSeedError(Exception):
    """Raised when the manifest and the artifact disagree.

    The only build-time inconsistency the loader can detect on its own is a
    manifest id that has no matching embedding row in the artifact — the join
    cannot resolve, so seeding would silently drop or mis-attach a row.
    """


# Colour vocabulary scanned (in title word order) against the lowercased title.
# Both UK ("grey") and US ("gray") spellings are present because Fashion200k
# titles mix them; the fallback covers titles with no colour word at all.
_COLOUR_VOCAB: tuple[str, ...] = (
    "black",
    "white",
    "navy",
    "red",
    "green",
    "beige",
    "grey",
    "gray",
    "blue",
    "pink",
    "brown",
    "yellow",
    "purple",
    "orange",
    "gold",
    "silver",
)
_COLOUR_FALLBACK = "multicolour"

# Deterministic price band (cents) — apparel-plausible spread. Bounds match the
# test contract (test_fashion200k_price_cents_positive_and_in_range): every
# derived price sits in [_PRICE_MIN_CENTS, _PRICE_MIN_CENTS + _PRICE_SPAN].
_PRICE_MIN_CENTS = 1500
_PRICE_SPAN = 23500  # → max 25000

# Category → (formality, occasion) heuristic. Fashion200k carries no formality
# or occasion ground truth; this map keeps both NOT-NULL-bound columns
# non-empty and apparel-plausible. Unmapped categories fall back to
# _FORMALITY_OCCASION_DEFAULT so the columns are always populated.
_FORMALITY_OCCASION: dict[str, tuple[str, str]] = {
    "dress": ("smart-casual", "everyday"),
    "top": ("casual", "everyday"),
    "shirt": ("smart-casual", "office"),
    "blouse": ("smart-casual", "office"),
    "skirt": ("smart-casual", "everyday"),
    "trousers": ("smart-casual", "office"),
    "pants": ("smart-casual", "office"),
    "jeans": ("casual", "everyday"),
    "jacket": ("smart-casual", "outdoor"),
    "coat": ("smart-casual", "outdoor"),
    "shoes": ("casual", "everyday"),
    "bag": ("casual", "everyday"),
}
_FORMALITY_OCCASION_DEFAULT: tuple[str, str] = ("casual", "everyday")


def fashion200k_rows(
    manifest_path: Path,
    artifact_dir: Path,
    *,
    backend: StorageBackend,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one ``catalog.products``-shaped row per manifest item.

    ``manifest_path`` is the self-describing manifest JSON
    (``{"items": [{id, category, title, source_url, split}, ...]}``).
    ``artifact_dir`` is the embedding artifact directory, read through
    ``backend`` via :func:`load_embedding_artifact` (the single sanctioned
    artifact reader — not re-implemented here). ``limit`` caps the number of
    rows yielded (``[catalog.seed_count]`` at the call site); ``None`` yields
    the whole manifest.

    Raises:
        Fashion200kSeedError: a manifest id is absent from the artifact.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = list(manifest["items"])
    # ``limit`` (the [catalog.seed_count] bound at the call site) caps the
    # subset; slicing here keeps the join below to just the rows we yield.
    if limit is not None:
        items = items[:limit]

    embeddings, _artifact_manifest = load_embedding_artifact(artifact_dir, backend)
    # Index the artifact rows by id so the join keys on id rather than position
    # — manifest order and artifact order need not agree.
    by_id: dict[str, dict[str, object]] = {str(row["id"]): row for row in embeddings}

    for item in items:
        item_id = str(item["id"])
        artifact_row = by_id.get(item_id)
        if artifact_row is None:
            raise Fashion200kSeedError(
                f"manifest item id {item_id!r} has no embedding in the artifact "
                f"at {artifact_dir} — manifest and embeddings "
                "disagree; rebuild the artifact before seeding."
            )

        title = str(item["title"])
        category = str(item["category"])
        formality, occasion = _formality_occasion_for(category)
        yield {
            "title": title,
            "category": category,
            "colour": _colour_from_title(title),
            "formality": formality,
            "occasion": occasion,
            "price_cents": _price_cents_for(item_id),
            "image_url": (item.get("source_url") or _image_url_for(item_id))
            if _serve_source_urls()
            else _image_url_for(item_id),
            "embedding": artifact_row["image_embedding"],
            "text_embedding": artifact_row["text_embedding"],
        }


def _colour_from_title(title: str) -> str:
    """Return the first colour-vocab word in ``title``; fallback otherwise.

    Matches on whitespace-delimited words (lowercased) so a substring like the
    "red" in "shredded" never registers as a colour.
    """
    words = set(title.lower().split())
    for colour in _COLOUR_VOCAB:
        if colour in words:
            return colour
    return _COLOUR_FALLBACK


def _price_cents_for(item_id: str) -> int:
    """Deterministic apparel-plausible price (cents) keyed on the item id.

    SHA-256 of the id mapped into ``[_PRICE_MIN_CENTS, _PRICE_MIN_CENTS +
    _PRICE_SPAN]``; stable across runs so reseeding is reproducible.
    """
    digest = int(hashlib.sha256(item_id.encode("utf-8")).hexdigest(), 16)
    return _PRICE_MIN_CENTS + (digest % _PRICE_SPAN)


def _image_url_for(item_id: str) -> str:
    """Tokenless ``/images/`` proxy path embedding the acquisition storage key.

    The acquisition layer (``avsa_data.acquisition.acquire_image``) writes the
    image at ``fashion200k/images/{item_id}.jpg``; this path mirrors that key.
    Signing is a read-time concern, so no token/expiry is embedded here.
    """
    return f"/images/fashion200k/images/{item_id}.jpg"


def _serve_source_urls() -> bool:
    """Whether to store the original CDN ``source_url`` as the catalog image_url.

    Default ``False`` → the read-time-signed ``/images/`` proxy path served from
    acquired local/GCS storage (reliable + offline; what the local stack, the
    smoke gate, and the e2e rely on). Set ``AVSA_CATALOG_SERVE_SOURCE_URLS=1`` in
    the prod deploy to store the original CDN ``source_url`` instead, so prod
    serves images straight from the source CDN without re-hosting the ~526M
    dataset. The frontend's imageProxy passes external absolute URLs through
    unchanged; the prod deploy must allowlist the CDN host in next.config
    ``images.remotePatterns`` for that path to render.
    """
    return os.environ.get("AVSA_CATALOG_SERVE_SOURCE_URLS", "0") == "1"


def _formality_occasion_for(category: str) -> tuple[str, str]:
    """Map a Fashion200k category to a (formality, occasion) pair.

    Falls back to ``_FORMALITY_OCCASION_DEFAULT`` for any unmapped category so
    both NOT-NULL-bound columns are always non-empty.
    """
    return _FORMALITY_OCCASION.get(category.lower(), _FORMALITY_OCCASION_DEFAULT)
