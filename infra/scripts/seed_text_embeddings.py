"""Seed catalog.products.text_embedding for rows where it is NULL.

Usage
-----
    uv run python infra/scripts/seed_text_embeddings.py [--batch-size N]

Environment variables
---------------------
DATABASE_URL
    Postgres connection string.
    Default: postgresql://avsa:avsa@localhost:5432/avsa

MODEL_URL
    URL of the model service (POST /embed_text).
    Default: http://localhost:8001
    Ignored when AVSA_MODEL_STUB=1.

AVSA_MODEL_STUB
    Set to "1" to use StubTextEmbedder directly (no HTTP call).
    Required in CI where the model service is not running.

Exit codes: 0 on success, 1 on error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.request import Request, urlopen

_DEFAULT_DB_URL = "postgresql://avsa:avsa@localhost:5432/avsa"
_DEFAULT_MODEL_URL = "http://localhost:8001"
_DEFAULT_BATCH_SIZE = 64


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed catalog.products.text_embedding for NULL rows."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        metavar="N",
        help=f"Number of rows to process per batch (default: {_DEFAULT_BATCH_SIZE})",
    )
    return parser.parse_args()


def _embed_via_http(texts: list[str], model_url: str) -> list[list[float]]:
    """POST texts to the model service and return 512-dim embeddings."""
    payload = json.dumps({"texts": texts}).encode("utf-8")
    req = Request(
        f"{model_url}/embed_text",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req) as resp:
        body: dict[str, Any] = json.loads(resp.read())
    embeddings: list[list[float]] = body["embeddings"]
    return embeddings


def _vector_literal(vector: list[float]) -> str:
    """Format a Python float list as a pgvector literal string ``[x, y, …]``."""
    return "[" + ",".join(str(v) for v in vector) + "]"


def main() -> int:
    args = _parse_args()

    database_url = os.environ.get("DATABASE_URL", _DEFAULT_DB_URL)
    model_url = os.environ.get("MODEL_URL", _DEFAULT_MODEL_URL)
    use_stub = os.environ.get("AVSA_MODEL_STUB", "") == "1"

    try:
        import psycopg
    except ImportError:
        print(
            "ERROR: psycopg is not installed. Run: pip install 'psycopg[binary]'",
            file=sys.stderr,
        )
        return 1

    embedder = None
    if use_stub:
        # Add the apps/model src tree so we can import avsa_model without
        # installing the package.
        import pathlib

        _model_src = (
            pathlib.Path(__file__).resolve().parents[2] / "apps" / "model" / "src"
        )
        if str(_model_src) not in sys.path:
            sys.path.insert(0, str(_model_src))

        from avsa_model.text_stub import StubTextEmbedder

        embedder = StubTextEmbedder()

    try:
        conn: psycopg.Connection[Any] = psycopg.connect(database_url)
    except psycopg.Error as exc:
        print(f"ERROR: cannot connect to database: {exc}", file=sys.stderr)
        return 1

    try:
        # Count total NULL rows upfront for progress reporting.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM catalog.products WHERE text_embedding IS NULL"
            )
            count_row = cur.fetchone()
            total: int = count_row[0] if count_row else 0

        if total == 0:
            print("No products with NULL text_embedding found — nothing to do.")
            return 0

        seeded = 0
        batch_size = args.batch_size

        while True:
            # Fetch the next batch of NULL rows.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id,
                           title || ' ' || category || ' ' || colour AS text_content
                    FROM catalog.products
                    WHERE text_embedding IS NULL
                    LIMIT %s
                    """,
                    (batch_size,),
                )
                rows = cur.fetchall()

            if not rows:
                break

            ids = [row[0] for row in rows]
            texts = [row[1] for row in rows]

            # Produce embeddings.
            if use_stub and embedder is not None:
                embeddings = embedder.embed(texts)
            else:
                try:
                    embeddings = _embed_via_http(texts, model_url)
                except Exception as exc:
                    print(f"ERROR: model service call failed: {exc}", file=sys.stderr)
                    return 1

            # Update in a single transaction per batch.
            with conn.cursor() as cur:
                for row_id, vector in zip(ids, embeddings, strict=False):
                    cur.execute(
                        """
                        UPDATE catalog.products
                        SET    text_embedding = %s::vector
                        WHERE  id = %s
                        """,
                        (_vector_literal(vector), row_id),
                    )
            conn.commit()

            seeded += len(ids)
            print(f"Seeded {seeded}/{total} products")

        print("Done.")
        return 0

    except psycopg.Error as exc:
        print(f"ERROR: database error: {exc}", file=sys.stderr)
        conn.rollback()
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
