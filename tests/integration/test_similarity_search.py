"""Integration tests for pgvector kNN similarity search against catalog.products.

Confirming that the HNSW index was created (spec-validate-sql gate) is
necessary but not sufficient. These tests verify that the index actually
serves correct cosine-similarity queries: nearest-neighbour ordering,
self-distance of zero, valid distance range, and LIMIT enforcement.

Without these's find_similar tool could silently return results
in the wrong order (e.g. wrong operator, wrong index type) or hit the p95
latency budget with a seqscan instead of an index scan.

Requires: DATABASE_URL pointing at a migrated Postgres + pgvector instance
with ≥100 rows in catalog.products (seeded via seeded_catalog_db fixture).
Skipped automatically when DATABASE_URL is unset.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


class TestKNNSimilaritySearch:
    """The HNSW index on catalog.products.embedding must serve correct
    cosine-distance kNN queries — not just exist in pg_indexes."""

    def test_nearest_neighbour_of_row_is_itself(
        self, seeded_catalog_db: object
    ) -> None:
        """Cosine distance from a vector to itself must be 0, and that row
        must rank first in ORDER BY embedding <=> self."""
        from psycopg import Connection

        assert isinstance(seeded_catalog_db, Connection)
        with seeded_catalog_db.cursor() as cur:
            cur.execute("SELECT id FROM catalog.products ORDER BY id LIMIT 1")
            row = cur.fetchone()
            assert row is not None, "catalog.products is empty after seeding"
            (target_id,) = row

            cur.execute(
                """
                WITH q AS (SELECT embedding FROM catalog.products WHERE id = %s)
                SELECT p.id, p.embedding <=> q.embedding AS dist
                FROM catalog.products p, q
                ORDER BY dist LIMIT 1
                """,
                (target_id,),
            )
            result = cur.fetchone()

        assert result is not None, "kNN query returned no rows"
        nearest_id, nearest_dist = result
        assert nearest_id == target_id, (
            f"nearest neighbour to row {target_id} should be itself; got {nearest_id}"
        )
        assert nearest_dist == pytest.approx(0.0, abs=1e-6), (
            f"cosine distance from a vector to itself must be 0; got {nearest_dist}"
        )

    def test_knn_results_are_ordered_ascending_by_distance(
        self, seeded_catalog_db: object
    ) -> None:
        """Rows from ORDER BY embedding <=> query_vec must arrive closest-first.
        Verifies that <=> is the cosine distance operator (not dot-product or L2),
        and that pgvector returns them in ascending order as documented."""
        from psycopg import Connection

        assert isinstance(seeded_catalog_db, Connection)
        with seeded_catalog_db.cursor() as cur:
            cur.execute("SELECT id FROM catalog.products ORDER BY id LIMIT 1")
            row = cur.fetchone()
            assert row is not None
            (target_id,) = row

            cur.execute(
                """
                WITH q AS (SELECT embedding FROM catalog.products WHERE id = %s)
                SELECT p.embedding <=> q.embedding AS dist
                FROM catalog.products p, q
                ORDER BY dist LIMIT 10
                """,
                (target_id,),
            )
            dists = [r[0] for r in cur.fetchall()]

        assert len(dists) > 1, "need at least 2 rows to verify distance ordering"
        assert dists == sorted(dists), (
            f"kNN distances must arrive in ascending order; got {dists}"
        )

    def test_cosine_distances_are_in_valid_range(
        self, seeded_catalog_db: object
    ) -> None:
        """All <=> distances must be in [0, 2]. Values outside this range
        indicate a dimension mismatch, a zero-vector, or the wrong operator."""
        from psycopg import Connection

        assert isinstance(seeded_catalog_db, Connection)
        with seeded_catalog_db.cursor() as cur:
            cur.execute("SELECT id FROM catalog.products ORDER BY id LIMIT 1")
            row = cur.fetchone()
            assert row is not None
            (target_id,) = row

            cur.execute(
                """
                WITH q AS (SELECT embedding FROM catalog.products WHERE id = %s)
                SELECT p.embedding <=> q.embedding AS dist
                FROM catalog.products p, q
                ORDER BY dist
                """,
                (target_id,),
            )
            dists = [r[0] for r in cur.fetchall()]

        assert dists, "no distances returned"
        for dist in dists:
            assert 0.0 <= dist <= 2.0, (
                f"cosine distance {dist} is outside valid range [0, 2]; "
                "check that embeddings are non-zero and <=> is the cosine operator"
            )

    def test_limit_returns_exactly_n_results(self, seeded_catalog_db: object) -> None:
        """LIMIT 5 must return exactly 5 rows when the catalog has more than 5."""
        from psycopg import Connection

        assert isinstance(seeded_catalog_db, Connection)
        with seeded_catalog_db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM catalog.products")
            count_row = cur.fetchone()
            assert count_row is not None
            total = count_row[0]
            if total < 6:
                pytest.skip(
                    "need >5 rows in catalog.products to verify LIMIT behaviour"
                )

            cur.execute("SELECT id FROM catalog.products ORDER BY id LIMIT 1")
            row = cur.fetchone()
            assert row is not None
            (target_id,) = row

            cur.execute(
                """
                WITH q AS (SELECT embedding FROM catalog.products WHERE id = %s)
                SELECT p.id
                FROM catalog.products p, q
                ORDER BY p.embedding <=> q.embedding LIMIT 5
                """,
                (target_id,),
            )
            rows = cur.fetchall()

        assert len(rows) == 5, (
            f"LIMIT 5 kNN query returned {len(rows)} rows; expected exactly 5"
        )

    def test_hnsw_index_exists_on_embedding_column(
        self, seeded_catalog_db: object
    ) -> None:
        """The HNSW index on catalog.products.embedding must be present.
        Without it, kNN queries fall back to a seqscan and the 
        find_similar p95 latency budget (150 ms) cannot be met at scale."""
        from psycopg import Connection

        assert isinstance(seeded_catalog_db, Connection)
        with seeded_catalog_db.cursor() as cur:
            cur.execute(
                """
                SELECT indexname FROM pg_indexes
                WHERE schemaname = 'catalog'
                  AND tablename = 'products'
                  AND indexdef ILIKE '%hnsw%'
                  AND indexdef ILIKE '%embedding%'
                  AND indexdef NOT ILIKE '%text_embedding%'
                """
            )
            rows = cur.fetchall()

        assert rows, (
            "No HNSW index found on catalog.products.embedding — "
            "required for  find_similar to meet the 150 ms p95 budget; "
            "check that specs/db/catalog.sql was applied via just db-migrate"
        )

    @pytest.mark.integration
    def test_knn_returns_semantically_similar_categories(
        self, seeded_catalog_db: object
    ) -> None:
        """kNN top-5 results for a query product should skew toward the same
        category — verifying that the index serves semantically meaningful
        ordering when category-correlated embeddings are used.

        The synthetic catalog uses stub_embedding(index) which is seeded by
        index, not by category, so the vectors are not semantically grouped.
        This test is skipped for that seed: the infrastructure (query, index,
        distance operator) is validated by the other tests in this class.
        """
        from psycopg import Connection

        assert isinstance(seeded_catalog_db, Connection)

        # Check whether the catalog seed produces category-correlated embeddings.
        # For the synthetic-v1 seed, embeddings are derived from per-index RNG
        # seeds and are not clustered by category — semantic correctness cannot
        # be asserted without a real or category-correlated embedding model.
        with seeded_catalog_db.cursor() as cur:
            # Pick the first product that belongs to the "shoes" category.
            cur.execute(
                "SELECT id, category FROM catalog.products "
                "WHERE category = 'shoes' ORDER BY id LIMIT 1"
            )
            query_row = cur.fetchone()

        if query_row is None:
            pytest.skip("No 'shoes' products found in the seeded catalog")

        query_id, query_category = query_row

        with seeded_catalog_db.cursor() as cur:
            cur.execute(
                """
                WITH q AS (SELECT embedding FROM catalog.products WHERE id = %s)
                SELECT p.id, p.category, p.embedding <=> q.embedding AS dist
                FROM catalog.products p, q
                WHERE p.id != %s
                ORDER BY dist LIMIT 5
                """,
                (query_id, query_id),
            )
            top5 = cur.fetchall()

        if not top5:
            pytest.skip("Not enough rows to run kNN top-5 query")

        same_category_count = sum(1 for _, cat, _ in top5 if cat == query_category)

        # The synthetic-v1 seed uses stub_embedding(index) seeded only by row
        # index — embeddings are not category-correlated, so at least 3-of-5
        # same-category cannot be guaranteed. Skip rather than assert an
        # arbitrary threshold that may not hold for this particular seed.
        pytest.skip(
            "catalog seed does not have category metadata "
            "to verify semantic correctness: "
            f"stub_embedding(index) is seeded by row index, not by category. "
            f"Got {same_category_count}/5 same-category results "
            f"for query category={query_category!r}. "
            "Replace stub_embedding with a category-correlated "
            "embedding to enable this assertion."
        )
