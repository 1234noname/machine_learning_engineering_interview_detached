"""Integration test for the  recall@5 retrieval eval runner.

Runs the recall@5 runner against the REAL seeded fashion200k catalog and
asserts the measured mean recall@5 is in [0, 1] and >= the committed baseline.
Covers both modalities: image and text
embedding).

Skip discipline (mirrors tests/fixtures/catalog.py + test_fashion200k_seed.py):
the test SKIPs — never errors at collection — when

* ``psycopg`` is not importable,
* ``DATABASE_URL`` does not point at a reachable Postgres,
* ``catalog.products`` is missing,
* the catalog has not been seeded with fashion200k rows (the eval is
  meaningless against the synthetic-v1 100-row fixture), or
* the runner module is not implemented yet (2A-i pre-implementation).

This test is READ-ONLY against the DB. Unlike the ``seeded_catalog_db``
fixture (which TRUNCATEs and reseeds synthetic data), it must run against the
already-seeded real catalog, so it deliberately does NOT use that fixture and
opens its own read-only connection.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from psycopg import Connection

pytestmark = pytest.mark.integration

_DEFAULT_DATABASE_URL = "postgresql://avsa:avsa@localhost:5434/avsa"

# tests/integration/test_recall_at_5_eval.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RETRIEVAL_DIR = _REPO_ROOT / "evals" / "retrieval"
_IMAGE_FIXTURES = _RETRIEVAL_DIR / "image" / "fixtures.jsonl"
_IMAGE_BASELINE = _RETRIEVAL_DIR / "image" / "baseline.toml"
_TEXT_FIXTURES = _RETRIEVAL_DIR / "text" / "fixtures.jsonl"
_TEXT_BASELINE = _RETRIEVAL_DIR / "text" / "baseline.toml"

# The runner maps a manifest item id to its DB row via this image_url shape
# (see avsa_data.catalog_fashion200k._image_url_for): a fashion200k row has
# image_url == /images/fashion200k/images/{item_id}.jpg.
_FASHION200K_URL_PREFIX = "/images/fashion200k/images/"


def _skip_unless_db_ready() -> str:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover — environmental
        pytest.skip(f"psycopg not installed: {exc}")
    database_url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    try:
        with (
            psycopg.connect(database_url, connect_timeout=2) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT to_regclass('catalog.products') IS NOT NULL")
            row = cur.fetchone()
    except psycopg.Error as exc:
        pytest.skip(f"Postgres unavailable at {database_url}: {exc}")
    if row is None or not row[0]:
        pytest.skip(
            "catalog.products does not exist — apply specs/db/catalog.sql "
            "before running the recall@5 eval."
        )
    return database_url


def _skip_unless_fashion200k_seeded(database_url: str) -> None:
    """Skip when the catalog has not been seeded with fashion200k rows.

    The recall@5 eval is only meaningful against the real fashion200k catalog;
    the synthetic-v1 100-row fixture has no product groupings.
    """
    import psycopg

    with (
        psycopg.connect(database_url, connect_timeout=2) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT count(*) FROM catalog.products WHERE image_url LIKE %s",
            (f"{_FASHION200K_URL_PREFIX}%",),
        )
        row = cur.fetchone()
    count = row[0] if row is not None else 0
    if count < 2:
        pytest.skip(
            "catalog.products is not seeded with fashion200k rows "
            f"(found {count} with image_url LIKE '{_FASHION200K_URL_PREFIX}%'); "
            "seed via scripts/seed-catalog.py before running the recall@5 eval."
        )


def _require_runner() -> Any:
    """Import the not-yet-implemented runner via ``importlib``, or SKIP (2A-i
    pre-impl). Using ``importlib`` keeps the pre-implementation state clean
    under mypy (a static ``from`` import of a missing submodule would be flagged
    ``attr-defined``)."""
    import importlib

    try:
        return importlib.import_module("evals.retrieval.run_recall_at_k")
    except ImportError as exc:
        pytest.skip(
            f"evals.retrieval.run_recall_at_k not implemented yet ({exc}) — "
            "expected during 2A-i pre-implementation."
        )


@pytest.fixture
def fashion200k_db() -> Iterator[Connection]:
    """Open a READ-ONLY connection to the already-seeded real catalog.

    Unlike ``seeded_catalog_db``, this does NOT truncate or reseed — the
    recall eval must read the real fashion200k embeddings.
    """
    database_url = _skip_unless_db_ready()
    _skip_unless_fashion200k_seeded(database_url)
    import psycopg

    conn = psycopg.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _committed_baseline(path: Path) -> float:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return float(data["recall_at_5"])


class TestRecallRunnerAgainstSeededCatalog:
    """The runner reproduces the kNN read-side directly and computes recall@5
    against the real fashion200k product groupings."""

    def test_recall_runner_image(self, fashion200k_db: Connection) -> None:
        """Image-modality recall@5 over image fixtures: in [0,1] and >= baseline."""
        runner = _require_runner()
        baseline = _committed_baseline(_IMAGE_BASELINE)

        result = runner.run_recall_at_k(
            fashion200k_db,
            fixtures_path=_IMAGE_FIXTURES,
            modality="image",
            k=5,
        )

        assert 0.0 <= result.mean_recall_at_5 <= 1.0, (
            f"image recall@5 must be in [0, 1]; got {result.mean_recall_at_5}"
        )
        assert result.num_queries > 0, (
            "runner must evaluate at least one query from the image fixtures"
        )
        assert result.mean_recall_at_5 >= baseline, (
            f"image recall@5 {result.mean_recall_at_5} regressed below the "
            f"committed baseline {baseline} (image/baseline.toml)"
        )

    def test_recall_runner_text(self, fashion200k_db: Connection) -> None:
        """Text-modality recall@5 over text fixtures: in [0,1] and >= baseline.

        The query vector is each product's text_embedding (512-d); kNN runs over
        catalog.products.text_embedding via pgvector `<=>`.
        """
        runner = _require_runner()
        baseline = _committed_baseline(_TEXT_BASELINE)

        result = runner.run_recall_at_k(
            fashion200k_db,
            fixtures_path=_TEXT_FIXTURES,
            modality="text",
            k=5,
        )

        assert 0.0 <= result.mean_recall_at_5 <= 1.0, (
            f"text recall@5 must be in [0, 1]; got {result.mean_recall_at_5}"
        )
        assert result.num_queries > 0, (
            "runner must evaluate at least one query from the text fixtures"
        )
        assert result.mean_recall_at_5 >= baseline, (
            f"text recall@5 {result.mean_recall_at_5} regressed below the "
            f"committed baseline {baseline} (text/baseline.toml)"
        )

    def test_recall_runner_excludes_query_self_from_knn(
        self, fashion200k_db: Connection
    ) -> None:
        """The runner must exclude the query row itself from the top-k results.

        If the query row were included, every query would trivially match
        itself at rank 0 and recall@5 would be a meaningless 1.0. The per-query
        diagnostics the runner returns must never list the query id among the
        retrieved top-k ids.
        """
        runner = _require_runner()
        result = runner.run_recall_at_k(
            fashion200k_db,
            fixtures_path=_IMAGE_FIXTURES,
            modality="image",
            k=5,
        )
        for q in result.per_query:
            assert q.query_id not in q.retrieved_ids, (
                f"query {q.query_id} appeared in its own top-k results — "
                "the runner must exclude the query row from the kNN scan"
            )
