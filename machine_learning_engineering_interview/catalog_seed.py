"""Helpers for seeding `catalog.products` with synthetic rows.

The seed pipeline is intentionally split:

* :func:`stub_embedding` returns a deterministic 768-d vector for a
  given seed. It is used both by the pytest fixture (no network) and by
  the CLI when ``AVSA_EMBED_STUB=1`` short-circuits the real ViT call.
* :func:`synthetic_product` returns a deterministic row dict for the
  given index. Picks every NOT NULL column from `catalog.products` from
  a small vocabulary so retrieval/verifier tests have realistic
  attribute filters to exercise.
* :func:`copy_rows` bulk-loads rows into Postgres via ``COPY ... FROM
  STDIN``. Used by both the CLI and the fixture so the throughput-vs-
  correctness trade-off is made in one place.
* :func:`load_config` reads ``[catalog]`` defaults from
  ``config/avsa.toml``.

The synthetic source is named ``synthetic-v1`` and is the default for
; see ``STAKEHOLDERS.md`` for data-provenance notes.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from psycopg import Connection


EMBEDDING_DIM = 768

# Small closed vocabularies so synthetic rows still exercise the
# attribute-filtered kNN path without needing a real catalog. Keep these
# stable — changing them changes every downstream fixture hash.
_CATEGORIES: tuple[str, ...] = (
    "dress",
    "shirt",
    "trousers",
    "jacket",
    "shoes",
    "bag",
    "hat",
    "skirt",
)
_COLOURS: tuple[str, ...] = (
    "black",
    "white",
    "navy",
    "red",
    "green",
    "beige",
    "grey",
    "yellow",
)
_FORMALITIES: tuple[str, ...] = ("casual", "smart-casual", "formal", "athleisure")
_OCCASIONS: tuple[str, ...] = ("everyday", "office", "wedding", "outdoor", "evening")


def stub_embedding(seed: int, dim: int = EMBEDDING_DIM) -> list[float]:
    """Return a deterministic, L2-normalised ``dim``-element vector for ``seed``.

    Used wherever a real ViT embedding is unavailable or undesirable
    (pytest fixture, ``AVSA_EMBED_STUB=1`` CLI path).

    L2 normalisation is required because pgvector's ``<=>`` cosine-distance
    operator gives correct results only for unit vectors — a non-normalised
    stub would silently produce wrong similarity rankings in integration tests.
    """
    rng = np.random.default_rng(seed=seed)
    v = rng.random(dim).astype(np.float32)
    norm: float = float(np.linalg.norm(v))
    if norm < float(np.finfo(np.float32).eps):
        raise ValueError(f"stub_embedding({seed}) produced a near-zero vector")
    return (v / norm).tolist()  # type: ignore[no-any-return]


def synthetic_product(index: int) -> dict[str, Any]:
    """Return one deterministic synthetic product row keyed by ``index``.

    Every NOT NULL column from ``specs/db/catalog.sql`` is populated.
    The deterministic-per-index contract is load-bearing: fixtures and
    snapshot tests rely on it.
    """
    category = _CATEGORIES[index % len(_CATEGORIES)]
    colour = _COLOURS[(index // len(_CATEGORIES)) % len(_COLOURS)]
    formality = _FORMALITIES[index % len(_FORMALITIES)]
    occasion = _OCCASIONS[index % len(_OCCASIONS)]
    # Price spread chosen so the synthetic catalog covers the price-filter
    # decision boundary that the retrieval tool exercises.
    price_cents = 999 + (index % 200) * 250
    return {
        "title": f"{colour.title()} {category} #{index:06d}",
        "category": category,
        "colour": colour,
        "formality": formality,
        "occasion": occasion,
        "price_cents": price_cents,
        "image_url": f"https://example.invalid/synthetic-v1/{index:06d}.jpg",
        "embedding": stub_embedding(index),
    }


@dataclass(frozen=True)
class SeedConfig:
    seed_count: int
    source: str
    # Storage key of the embedding artifact for source=fashion200k; lets
    # the seeder run without an explicit --embedding-artifact flag.
    embedding_artifact: str | None = None


def load_config(path: Path) -> SeedConfig:
    """Load `[catalog]` defaults from ``config/avsa.toml``."""
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    catalog = data.get("catalog", {})
    return SeedConfig(
        seed_count=int(catalog["seed_count"]),
        source=str(catalog["source"]),
        embedding_artifact=catalog.get("embedding_artifact"),
    )


_COPY_COLUMNS: tuple[str, ...] = (
    "title",
    "category",
    "colour",
    "formality",
    "occasion",
    "price_cents",
    "image_url",
    "embedding",
)


def _format_vector(values: Sequence[float]) -> str:
    # pgvector accepts `[v1,v2,...,vn]` in text format. No spaces — the
    # tighter encoding cuts COPY payload size by ~10% on 768-d vectors.
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def copy_rows(
    conn: Connection[Any],
    rows: Iterable[dict[str, Any]],
    *,
    include_text_embedding: bool = False,
) -> int:
    """Bulk-insert ``rows`` into ``catalog.products`` via COPY.

    Returns the number of rows written. The caller is responsible for
    committing the connection — keeping the commit boundary out of the
    helper lets the pytest fixture wrap the whole insert in a single
    transaction that it rolls back on teardown.

    ``include_text_embedding`` is ``False`` by default — the synthetic-v1
    contract: an 8-column COPY with no ``text_embedding`` reference, byte
    identical to the earlier path. When ``True`` (the fashion200k source),
    each row also carries a 512-d ``text_embedding`` encoded as a pgvector text
    literal (same encoding as ``embedding``), making it a 9-column COPY.
    """
    columns = (
        (*_COPY_COLUMNS, "text_embedding") if include_text_embedding else _COPY_COLUMNS
    )
    column_list = ", ".join(columns)
    written = 0
    with (
        conn.cursor() as cur,
        cur.copy(f"COPY catalog.products ({column_list}) FROM STDIN") as copy,
    ):
        for row in rows:
            fields: tuple[object, ...] = (
                row["title"],
                row["category"],
                row["colour"],
                row["formality"],
                row["occasion"],
                row["price_cents"],
                row["image_url"],
                _format_vector(row["embedding"]),
            )
            if include_text_embedding:
                fields = (*fields, _format_vector(row["text_embedding"]))
            copy.write_row(fields)
            written += 1
    return written


def _fashion200k_rows(
    manifest_path: Path | None,
    artifact_dir: Path | None,
    *,
    backend: Any,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Seam onto ``avsa_data.catalog_fashion200k.fashion200k_rows``.

    The import is lazy so the common synthetic-catalog path never imports the
    data-prep library. Tests monkeypatch this name to prove ``rows_for_source``
    routes here for ``source="fashion200k"``.
    """
    if manifest_path is None or artifact_dir is None or backend is None:
        raise ValueError(
            "source='fashion200k' requires manifest_path, artifact_dir and "
            "backend (supplied by scripts/seed-catalog.py)"
        )
    from avsa_data.catalog_fashion200k import fashion200k_rows

    yield from fashion200k_rows(
        manifest_path, artifact_dir, backend=backend, limit=limit
    )


def rows_for_source(
    source: str,
    *,
    count: int,
    manifest_path: Path | None = None,
    artifact_dir: Path | None = None,
    backend: Any | None = None,
) -> Iterator[dict[str, Any]]:
    """Dispatch the catalog row generator selected by ``[catalog.source]``.

    - ``"synthetic-v1"`` → the deterministic :func:`synthetic_product`
      generator, ``count`` rows (the earlier default path).
    - ``"fashion200k"`` → the fashion200k artifact loader via the
      :func:`_fashion200k_rows` seam, bounded by ``count``. Requires
      ``manifest_path``, ``artifact_dir`` and ``backend``.
    - anything else → ``ValueError`` naming the unknown source.
    """
    if source == "synthetic-v1":
        for index in range(count):
            yield synthetic_product(index)
        return
    if source == "fashion200k":
        yield from _fashion200k_rows(
            manifest_path, artifact_dir, backend=backend, limit=count
        )
        return
    raise ValueError(
        f"unknown catalog source {source!r}; expected 'synthetic-v1' or 'fashion200k'"
    )
