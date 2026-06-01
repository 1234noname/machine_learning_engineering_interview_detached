"""Recall@k eval runner against the seeded Fashion200k catalog.

Reproduces the kNN read-side directly (psycopg + pgvector ``<=>`` cosine) — it
does NOT call ``RetrievalTool`` or the orchestrator (anti-collusion: this
measures retrieval quality, it must not be able to influence it).

For each fixture query the runner:

1. resolves the manifest item id to its ``catalog.products`` row via
   ``image_url = /images/fashion200k/images/{item_id}.jpg`` (the mapping the
   seeder wrote — see ``avsa_data.catalog_fashion200k._image_url_for``);
2. runs a top-k cosine-kNN over the query row's own embedding — ``embedding``
   (768-d) for ``modality="image"`` or ``text_embedding`` (512-d) for
   ``modality="text"`` — EXCLUDING the query row itself;
3. maps the retrieved rows back to manifest item ids (same image_url shape)
   and scores recall@k against the query's expected-relevant (same-product)
   set.

The runner takes an already-open connection (the integration test opens a
READ-ONLY one) and never writes.

Pinned by ``tests/integration/test_recall_at_5_eval.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from evals.retrieval.recall import mean_recall_at_k, recall_at_k

if TYPE_CHECKING:
    from collections.abc import Sequence

    from psycopg import Connection

# The seeder wrote image_url = /images/fashion200k/images/{item_id}.jpg
# (avsa_data.catalog_fashion200k._image_url_for). The runner reproduces both
# directions of that mapping to join manifest ids <-> catalog rows.
_FASHION200K_URL_PREFIX = "/images/fashion200k/images/"
_FASHION200K_URL_SUFFIX = ".jpg"

Modality = Literal["image", "text"]

# A ``catalog.products`` primary-key value as psycopg hands it back. Typed
# ``object`` because the row id crosses the DB boundary opaquely: this eval
# only ever uses it to feed it straight back into a parameterised query
# (``WHERE id = %s``), never inspecting or constructing it, so the concrete
# adapter type (UUID / str / bytes) is deliberately not pinned here.
RowId = object

# Modality -> the embedding column the kNN scans. Keyed by the runner's public
# `modality` argument so the column choice is a lookup, not a branch every
# caller has to reason about.
_EMBEDDING_COLUMN: dict[Modality, str] = {
    "image": "embedding",
    "text": "text_embedding",
}


class RecallEvalError(Exception):
    """Raised when the eval cannot be run as specified.

    Fail-fast at the boundary: an unknown modality, an unreadable fixtures
    file, or a fixture whose item id is absent from the seeded catalog — each
    would otherwise silently shrink or distort the measured recall.
    """


@dataclass(frozen=True)
class PerQueryResult:
    """Diagnostics for a single evaluated query."""

    query_id: str
    expected_relevant_ids: tuple[str, ...]
    retrieved_ids: tuple[str, ...]
    recall_at_k: float


@dataclass(frozen=True)
class RecallResult:
    """Aggregate recall@k over a fixture set, with per-query diagnostics."""

    # This eval is recall@*5* by contract (the committed gate and the frozen
    # integration test both assert on `.mean_recall_at_5`). `k` is a runner
    # argument for generality but is expected to be 5; the field name is
    # k-specific on purpose and must not be renamed without re-freezing the
    # test (tests/integration/test_recall_at_5_eval.py).
    mean_recall_at_5: float
    num_queries: int
    k: int
    modality: Modality
    per_query: tuple[PerQueryResult, ...]


@dataclass(frozen=True)
class _Fixture:
    query_id: str
    expected_relevant_ids: tuple[str, ...]


def _image_url_for(item_id: str) -> str:
    """Manifest item id -> seeded ``catalog.products.image_url``."""
    return f"{_FASHION200K_URL_PREFIX}{item_id}{_FASHION200K_URL_SUFFIX}"


def _item_id_for(image_url: str) -> str | None:
    """Inverse of :func:`_image_url_for`: catalog image_url -> manifest item id.

    Returns ``None`` for a row that is not a fashion200k row (different
    image_url shape) so non-eval rows in the catalog simply never match an
    expected-relevant id rather than corrupting the comparison.
    """
    if not (
        image_url.startswith(_FASHION200K_URL_PREFIX)
        and image_url.endswith(_FASHION200K_URL_SUFFIX)
    ):
        return None
    return image_url[len(_FASHION200K_URL_PREFIX) : -len(_FASHION200K_URL_SUFFIX)]


def _load_fixtures(fixtures_path: Path) -> list[_Fixture]:
    """Parse the JSONL fixtures (one ``{query_id, expected_relevant_ids}`` per
    line). Blank lines are skipped; a malformed line fails fast."""
    try:
        text = Path(fixtures_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RecallEvalError(
            f"cannot read fixtures at {fixtures_path}: {exc}"
        ) from exc

    fixtures: list[_Fixture] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            fixtures.append(
                _Fixture(
                    query_id=str(obj["query_id"]),
                    expected_relevant_ids=tuple(
                        str(x) for x in obj["expected_relevant_ids"]
                    ),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RecallEvalError(
                f"malformed fixture at {fixtures_path}:{lineno}: {exc}"
            ) from exc
    if not fixtures:
        raise RecallEvalError(f"no fixtures found in {fixtures_path}")
    return fixtures


def _resolve_row_ids(
    conn: Connection,
    item_ids: Sequence[str],
    item_id_to_url: Callable[[str], str],
) -> dict[str, RowId]:
    """Map manifest item ids to their ``catalog.products`` row ids in one query.

    Keyed on the seeded image_url; an item id with no matching row is simply
    absent from the result (the caller decides whether that is fatal).
    """
    urls = [item_id_to_url(i) for i in item_ids]
    url_to_item = {item_id_to_url(i): i for i in item_ids}
    rows_by_item: dict[str, RowId] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, image_url FROM catalog.products WHERE image_url = ANY(%s)",
            (urls,),
        )
        for row_id, image_url in cur.fetchall():
            item = url_to_item.get(image_url)
            if item is not None:
                rows_by_item[item] = row_id
    return rows_by_item


def _knn_item_ids(
    conn: Connection,
    *,
    row_id: RowId,
    column: str,
    k: int,
    url_to_item_id: Callable[[str], str | None],
) -> tuple[str, ...]:
    """Top-k same-modality cosine-kNN item ids for a query row, excluding self.

    ``column`` is interpolated into the SQL (not a bind parameter — identifiers
    cannot be parameterised); it is constrained to the trusted
    ``_EMBEDDING_COLUMN`` values by the caller, never user input.
    """
    sql = (
        f"WITH qv AS (SELECT {column} AS vec FROM catalog.products WHERE id = %s) "
        f"SELECT p.image_url FROM catalog.products p, qv "
        f"WHERE p.id <> %s AND p.{column} IS NOT NULL "
        f"ORDER BY p.{column} <=> qv.vec "
        f"LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (row_id, row_id, k))
        retrieved: list[str] = []
        for (image_url,) in cur.fetchall():
            item_id = url_to_item_id(image_url)
            if item_id is not None:
                retrieved.append(item_id)
    return tuple(retrieved)


def run_recall_at_k(
    conn: Connection,
    *,
    fixtures_path: Path,
    modality: Modality,
    k: int,
    item_id_to_url: Callable[[str], str] | None = None,
    url_to_item_id: Callable[[str], str | None] | None = None,
) -> RecallResult:
    """Compute mean recall@k over the fixture set against the seeded catalog.

    Args:
        conn: an open (read-only is fine) psycopg connection to the seeded
            catalog.
        fixtures_path: JSONL of ``{query_id, expected_relevant_ids}`` rows.
        modality: ``"image"`` (kNN over ``embedding``) or ``"text"`` (over
            ``text_embedding``).
        k: cutoff for recall@k (5 for the committed gate).
        item_id_to_url: optional mapping from a manifest item id to the
            ``catalog.products.image_url`` stored for that item. Defaults to
            the Fashion200k local-path convention
            (``/images/fashion200k/images/{id}.jpg``). Pass a CDN-URL resolver
            when the catalog was seeded with original Lyst source URLs.
        url_to_item_id: inverse of ``item_id_to_url`` — maps a retrieved
            ``catalog.products.image_url`` back to a manifest item id, or
            ``None`` for rows that don't correspond to any fixture item.
            Defaults to the local-path inverse of ``_image_url_for``.

    Raises:
        RecallEvalError: unknown modality, unreadable fixtures, or a query item
            id absent from the seeded catalog.
    """
    column = _EMBEDDING_COLUMN.get(modality)
    if column is None:
        raise RecallEvalError(
            f"unknown modality {modality!r}; expected one of "
            f"{sorted(_EMBEDDING_COLUMN)}"
        )

    _id_to_url = item_id_to_url if item_id_to_url is not None else _image_url_for
    _url_to_id = url_to_item_id if url_to_item_id is not None else _item_id_for

    fixtures = _load_fixtures(fixtures_path)
    query_row_ids = _resolve_row_ids(conn, [fx.query_id for fx in fixtures], _id_to_url)

    per_query: list[PerQueryResult] = []
    scored: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for fx in fixtures:
        row_id = query_row_ids.get(fx.query_id)
        if row_id is None:
            raise RecallEvalError(
                f"fixture query id {fx.query_id!r} has no row in catalog.products "
                f"(expected image_url {_id_to_url(fx.query_id)!r}); the "
                "catalog must be seeded with the fixtures' fashion200k items."
            )
        retrieved = _knn_item_ids(
            conn, row_id=row_id, column=column, k=k, url_to_item_id=_url_to_id
        )
        score = recall_at_k(retrieved, fx.expected_relevant_ids, k)
        per_query.append(
            PerQueryResult(
                query_id=fx.query_id,
                expected_relevant_ids=fx.expected_relevant_ids,
                retrieved_ids=retrieved,
                recall_at_k=score,
            )
        )
        scored.append((retrieved, fx.expected_relevant_ids))

    return RecallResult(
        # `k` is expected to be 5: this eval is recall@5 by contract (see the
        # field's docstring note); the field name is pinned by the frozen test.
        mean_recall_at_5=mean_recall_at_k(scored, k),
        num_queries=len(per_query),
        k=k,
        modality=modality,
        per_query=tuple(per_query),
    )
