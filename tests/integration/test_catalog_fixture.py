"""Integration tests for the  catalog fixture.

Requires a live Postgres + pgvector instance with migrations applied
. Skipped automatically when DATABASE_URL is unset or
catalog.products does not exist — the `seeded_catalog_db` fixture
handles the skip logic.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


class TestCatalogFixture:
    def test_fixture_seeds_100_rows(self, seeded_catalog_db: object) -> None:
        from psycopg import Connection

        assert isinstance(seeded_catalog_db, Connection)
        with seeded_catalog_db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM catalog.products")
            row = cur.fetchone()
        assert row is not None
        count = row[0]
        assert count >= 100, f"expected ≥100 rows in catalog.products, got {count}"

    def test_fixture_completes_under_two_seconds(
        self, catalog_seed_timing: float
    ) -> None:
        assert catalog_seed_timing < 2.0, (
            f"100-row fixture seed took {catalog_seed_timing:.2f}s; "
            "must complete in < 2s on CI hardware ( DoD)"
        )

    def test_fixture_rows_have_768d_embeddings(self, seeded_catalog_db: object) -> None:
        from psycopg import Connection

        assert isinstance(seeded_catalog_db, Connection)
        with seeded_catalog_db.cursor() as cur:
            cur.execute("SELECT vector_dims(embedding) FROM catalog.products LIMIT 1")
            row = cur.fetchone()
        assert row is not None, "no rows in catalog.products after fixture"
        assert row[0] == 768, f"embedding dimension should be 768, got {row[0]}"
