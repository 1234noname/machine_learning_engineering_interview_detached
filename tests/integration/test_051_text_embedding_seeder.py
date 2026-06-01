"""Integration test for — text-embedding migration + seeder.

Verifies that:
1. Migration 005 adds `text_embedding vector(512)` and the HNSW index to
   `catalog.products` (idempotently — re-running is safe).
2. The seeder script ``infra/scripts/seed_text_embeddings.py`` fills
   ``text_embedding`` for rows where it was NULL, using
   ``AVSA_MODEL_STUB=1`` so no model weights are required.

Skipped when ``DATABASE_URL`` is unset (same pattern as the other
integration tests — CI sets it via the Postgres service container).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_SCRIPT = REPO_ROOT / "infra" / "migrations" / "migrate.sh"
SEEDER_SCRIPT = REPO_ROOT / "infra" / "scripts" / "seed_text_embeddings.py"

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://avsa:avsa@localhost:5434/avsa"
)

# Apply integration marker and skip when DATABASE_URL is unset.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL unset; integration test requires a live Postgres",
    ),
]


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    """Apply all migrations (including 005) before any test in this module."""
    subprocess.run(
        ["bash", str(MIGRATE_SCRIPT)],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def db_conn() -> Any:
    """Yield a fresh psycopg connection; roll back on teardown."""
    conn: psycopg.Connection[Any] = psycopg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


class TestMigration005:
    """Migration 005 must add the column + HNSW index, idempotently."""

    def test_text_embedding_column_exists(
        self, db_conn: psycopg.Connection[Any]
    ) -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'catalog'
                  AND table_name   = 'products'
                  AND column_name  = 'text_embedding'
                """
            )
            row = cur.fetchone()
        assert row is not None, (
            "catalog.products.text_embedding column is missing after migration 005"
        )

    def test_text_embedding_hnsw_index_exists(
        self, db_conn: psycopg.Connection[Any]
    ) -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM pg_indexes
                WHERE schemaname = 'catalog'
                  AND tablename  = 'products'
                  AND indexname  = 'products_text_embedding_hnsw'
                """
            )
            row = cur.fetchone()
        assert row is not None, (
            "products_text_embedding_hnsw HNSW index is missing after migration 005"
        )


class TestSeeder:
    """seed_text_embeddings.py must populate text_embedding for NULL rows."""

    def _insert_null_product(self, cur: psycopg.Cursor[Any]) -> uuid.UUID:
        """Insert a minimal product row with text_embedding = NULL; return its id."""
        product_id = uuid.uuid4()
        cur.execute(
            """
            INSERT INTO catalog.products
                (id, title, category, colour, formality, occasion,
                 price_cents, image_url, embedding)
            VALUES
                (%s, 'Test Jacket', 'jackets', 'blue', 'smart-casual',
                 'work', 4999, 'https://example.com/img.jpg',
                 %s::vector)
            """,
            (
                product_id,
                # 768-dim zero vector as a pgvector literal
                "[" + ",".join(["0.0"] * 768) + "]",
            ),
        )
        return product_id

    def test_seeder_fills_null_text_embeddings(
        self, db_conn: psycopg.Connection[Any]
    ) -> None:
        """Run the seeder (stub mode) and confirm text_embedding is non-null
        with exactly 512 dimensions for the inserted test row."""
        with db_conn.cursor() as cur:
            product_id = self._insert_null_product(cur)
        db_conn.commit()

        try:
            env = dict(os.environ)
            env["DATABASE_URL"] = DATABASE_URL
            env["AVSA_MODEL_STUB"] = "1"

            result = subprocess.run(
                [sys.executable, str(SEEDER_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, (
                f"seeder exited with code {result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )

            # Verify the row was updated.
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT vector_dims(text_embedding)"
                    " FROM catalog.products WHERE id = %s",
                    (product_id,),
                )
                row = cur.fetchone()

            assert row is not None, "product row not found after seeder ran"
            assert row[0] == 512, (
                f"expected text_embedding to have 512 dims after seeding; got {row[0]}"
            )
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM catalog.products WHERE id = %s", (product_id,))
            db_conn.commit()

    def test_seeder_prints_progress(self, db_conn: psycopg.Connection[Any]) -> None:
        """Seeder stdout must include 'Seeded' progress text."""
        with db_conn.cursor() as cur:
            product_id = self._insert_null_product(cur)
        db_conn.commit()

        try:
            env = dict(os.environ)
            env["DATABASE_URL"] = DATABASE_URL
            env["AVSA_MODEL_STUB"] = "1"

            result = subprocess.run(
                [sys.executable, str(SEEDER_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
            )

            assert result.returncode == 0, (
                f"seeder failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            assert "Seeded" in result.stdout, (
                f"expected 'Seeded' in seeder stdout; got:\n{result.stdout}"
            )
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM catalog.products WHERE id = %s", (product_id,))
            db_conn.commit()

    def test_seeder_skips_already_seeded_rows(
        self, db_conn: psycopg.Connection[Any]
    ) -> None:
        """Running the seeder twice must leave already-seeded rows unchanged."""
        with db_conn.cursor() as cur:
            product_id = self._insert_null_product(cur)
        db_conn.commit()

        try:
            env = dict(os.environ)
            env["DATABASE_URL"] = DATABASE_URL
            env["AVSA_MODEL_STUB"] = "1"

            # First run — fills the row.
            subprocess.run(
                [sys.executable, str(SEEDER_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )

            # Capture the embedding after the first run.
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT text_embedding::text FROM catalog.products WHERE id = %s",
                    (product_id,),
                )
                first_embedding = cur.fetchone()

            # Second run — should be a no-op (no NULL rows for our product).
            result2 = subprocess.run(
                [sys.executable, str(SEEDER_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
            )
            assert result2.returncode == 0, (
                f"second seeder run failed:\n{result2.stdout}\n{result2.stderr}"
            )

            # Embedding must be unchanged.
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT text_embedding::text FROM catalog.products WHERE id = %s",
                    (product_id,),
                )
                second_embedding = cur.fetchone()

            assert first_embedding == second_embedding, (
                "seeder must not overwrite already-seeded rows"
            )
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM catalog.products WHERE id = %s", (product_id,))
            db_conn.commit()
