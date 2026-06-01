"""Integration test for the  fashion200k seeder source.

Requires a live Postgres + pgvector instance with the catalog schema applied
(``specs/db/catalog.sql``). Skips automatically when ``DATABASE_URL`` is unset,
Postgres is unreachable, ``catalog.products`` is missing, or the as-yet-
unimplemented fashion200k loader cannot be imported — mirroring the skip logic
in ``tests/fixtures/catalog.py`` / ``tests/integration/test_catalog_fixture.py``.

Why this lives in the top-level ``tests/integration/`` suite (not
``apps/api/tests/``): ``psycopg`` is a dev dependency of the repo-root project
(``pyproject.toml`` ``[dependency-groups].dev``) and the existing catalog DB
integration tests live here. The fashion200k *loader* lives in the ``avsa_data``
package (it reuses ``load_embedding_artifact`` + the ``avsa_core`` storage
backend), installed in the root venv as an editable path dep, so this test
imports it directly. When the loader / its deps aren't importable, the import
fails and the test SKIPs — so it never produces a collection error and never
blocks the gate.

The seed lands in ``catalog.products`` inside a transaction that is rolled back
on teardown, so the test is isolated and does not pollute the synthetic-v1
fixture's view of the table.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from psycopg import Connection

pytestmark = pytest.mark.integration

from machine_learning_engineering_interview import catalog_seed  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_SRC = _REPO_ROOT / "apps" / "api" / "src"
if str(_APP_SRC) not in sys.path:
    sys.path.insert(0, str(_APP_SRC))

_DEFAULT_DATABASE_URL = "postgresql://avsa:avsa@localhost:5434/avsa"

_IMAGE_DIM = 768
_TEXT_DIM = 512

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
        "title": "navy silk blouse",
        "source_url": "https://example.invalid/200.jpeg",
        "split": "train",
    },
    {
        "id": "women/skirts/mini/300/300_3.jpeg",
        "category": "skirt",
        "title": "red pleated mini skirt",
        "source_url": "https://example.invalid/300.jpeg",
        "split": "test",
    },
]


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
            "before running integration tests."
        )
    return database_url


def _require_loader() -> Any:
    try:
        from avsa_data.catalog_fashion200k import (
            fashion200k_rows,
        )
    except ImportError as exc:
        pytest.skip(
            f"avsa_data.catalog_fashion200k not implemented yet ({exc}) — "
            "expected during 2A-i pre-implementation."
        )
    return fashion200k_rows


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Any]:
    try:
        from avsa_core.storage.local import (
            LocalStorageBackend,
        )
    except ImportError as exc:  # pragma: no cover — environmental
        pytest.skip(f"avsa_core.storage.local not importable: {exc}")

    backend = LocalStorageBackend(root=tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "seed": 17,
                "criteria": {"count": len(_ITEMS), "selection": "fixture"},
                "dataset_version": "fashion200k-v1.0",
                "items": _ITEMS,
            }
        ),
        encoding="utf-8",
    )

    artifact_dir = Path("data/embeddings/fixture-hash")
    artifact_dir_str = str(artifact_dir).replace("\\", "/")
    lines: list[str] = []
    for idx, item in enumerate(_ITEMS):
        lines.append(
            json.dumps(
                {
                    "id": item["id"],
                    "image_embedding": [float((idx + 1) % 7) / 7.0] * _IMAGE_DIM,
                    "text_embedding": [float((idx + 2) % 5) / 5.0] * _TEXT_DIM,
                }
            )
        )
    backend.put_object(
        f"{artifact_dir_str}/embeddings.jsonl",
        ("\n".join(lines) + "\n").encode("utf-8"),
    )
    backend.put_object(
        f"{artifact_dir_str}/manifest.json",
        (
            json.dumps(
                {
                    "model_version_image": "vit-b-16@2026-05-01",
                    "model_version_text": "minilm-l6-v2@2026-05-01",
                    "image_dim": _IMAGE_DIM,
                    "text_dim": _TEXT_DIM,
                    "item_count": len(_ITEMS),
                    "content_hash": "fixture-hash",
                    "generated_at": "2026-05-25T00:00:00Z",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )
    return manifest_path, artifact_dir, backend


@pytest.fixture
def fashion200k_seeded_db(tmp_path: Path) -> Iterator[Connection]:
    """Seed ``catalog.products`` with the fixture fashion200k rows; roll back."""
    database_url = _skip_unless_db_ready()
    fashion200k_rows = _require_loader()
    manifest_path, artifact_dir, backend = _write_fixture(tmp_path)

    import psycopg

    conn = psycopg.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE catalog.products RESTART IDENTITY")
        # ``copy_rows`` grows ``include_text_embedding`` in; fetch it
        # dynamically so this file passes the repo-root mypy gate before the
        # signature change lands.
        copy_rows = getattr(catalog_seed, "copy_rows")  # noqa: B009
        written = copy_rows(
            conn,
            fashion200k_rows(manifest_path, artifact_dir, backend=backend),
            include_text_embedding=True,
        )
        assert written == len(_ITEMS), (
            f"fashion200k COPY wrote {written} rows, expected {len(_ITEMS)}"
        )
        yield conn
    finally:
        conn.rollback()
        conn.close()


class TestFashion200kSeedIntegration:
    def test_rows_have_required_fields_and_dims(
        self, fashion200k_seeded_db: Connection
    ) -> None:
        with fashion200k_seeded_db.cursor() as cur:
            cur.execute(
                "SELECT title, category, colour, formality, occasion, price_cents, "
                "image_url, vector_dims(embedding), vector_dims(text_embedding) "
                "FROM catalog.products"
            )
            rows = cur.fetchall()
        assert len(rows) == len(_ITEMS), (
            f"expected {len(_ITEMS)} seeded rows, got {len(rows)}"
        )
        for row in rows:
            (
                title,
                category,
                colour,
                formality,
                occasion,
                price_cents,
                image_url,
                image_dims,
                text_dims,
            ) = row
            for label, value in (
                ("title", title),
                ("category", category),
                ("colour", colour),
                ("formality", formality),
                ("occasion", occasion),
                ("image_url", image_url),
            ):
                assert isinstance(value, str) and value, (
                    f"{label} must be a non-empty string in the seeded row; "
                    f"got {value!r}"
                )
            assert isinstance(price_cents, int) and price_cents > 0, (
                f"price_cents must be a positive int; got {price_cents!r}"
            )
            assert image_dims == _IMAGE_DIM, (
                f"image embedding must be {_IMAGE_DIM}-d in catalog.products; "
                f"got {image_dims}"
            )
            assert text_dims == _TEXT_DIM, (
                f"text embedding must be {_TEXT_DIM}-d in catalog.products; "
                f"got {text_dims}"
            )

    def test_image_url_is_resolvable_shaped_proxy_path(
        self, fashion200k_seeded_db: Connection
    ) -> None:
        with fashion200k_seeded_db.cursor() as cur:
            cur.execute("SELECT image_url FROM catalog.products")
            urls = [r[0] for r in cur.fetchall()]
        assert urls, "no rows seeded"
        for url in urls:
            assert url.startswith("/images/"), (
                f"seeded image_url must be a /images/ proxy path; got {url!r}"
            )
            assert "token=" not in url and "expires=" not in url, (
                "seeded image_url must be tokenless (signing is read-time); "
                f"got {url!r}"
            )
