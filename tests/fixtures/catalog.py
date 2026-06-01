"""Pytest fixture: seed `catalog.products` with 100 deterministic rows.

Tests under ``tests/integration/`` opt in by depending on the
``seeded_catalog_db`` fixture. Each test sees an isolated 100-row
catalog — the fixture wraps its inserts in a transaction and rolls
back on teardown, so concurrent tests don't see each other's writes.

The fixture skips cleanly when:

* psycopg cannot be imported (the seeder's runtime dep isn't pinned
  into ``[dependency-groups].dev`` yet, or the local env hasn't synced).
* ``DATABASE_URL`` does not point at a reachable Postgres.
* The ``catalog.products`` table is missing — i.e. the schema
  migrations haven't been applied to the target database.

That keeps per-PR CI green today (no Postgres service) and turns the
fixture into a real DoD check once the schema migrations land.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

from machine_learning_engineering_interview import catalog_seed

if TYPE_CHECKING:
    from collections.abc import Iterator

    from psycopg import Connection


_FIXTURE_ROW_COUNT = 100
_DEFAULT_DATABASE_URL = "postgresql://avsa:avsa@localhost:5434/avsa"


def _skip_unless_db_ready() -> str:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover — environmental
        pytest.skip(f"psycopg not installed: {exc}")
    database_url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
    try:
        # Two `with` blocks combined per SIM117 — the inner cursor scope
        # closes before the outer connection scope.
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


@pytest.fixture
def catalog_seed_timing(request: pytest.FixtureRequest) -> float:
    # Re-resolves `seeded_catalog_db` so the timing test can read the
    # wall-clock seconds the insert took without re-seeding.
    request.getfixturevalue("seeded_catalog_db")
    timing = getattr(request.node, "_avsa_catalog_seed_seconds", None)
    if timing is None:
        pytest.skip("seed timing was not recorded (fixture skipped)")
    return float(timing)


@pytest.fixture
def seeded_catalog_db(
    request: pytest.FixtureRequest,
) -> Iterator[Connection]:
    """Yield a Postgres connection whose `catalog.products` table holds
    exactly 100 deterministic rows. Rolls back on teardown so tests stay
    isolated.
    """
    database_url = _skip_unless_db_ready()
    import psycopg

    conn = psycopg.connect(database_url)
    try:
        with conn.cursor() as cur:
            # Wipe and re-seed inside a single transaction. TRUNCATE is
            # ~100x faster than DELETE for full-table resets — the
            # fixture is allowed to be destructive against
            # `catalog.products` in a test DB.
            cur.execute("TRUNCATE catalog.products RESTART IDENTITY")
        started = time.perf_counter()
        written = catalog_seed.copy_rows(
            conn,
            (catalog_seed.synthetic_product(i) for i in range(_FIXTURE_ROW_COUNT)),
        )
        elapsed = time.perf_counter() - started
        assert written == _FIXTURE_ROW_COUNT, (
            f"fixture COPY wrote {written} rows, expected {_FIXTURE_ROW_COUNT}"
        )
        request.node._avsa_catalog_seed_seconds = elapsed
        yield conn
    finally:
        # Roll back the transaction so the test DB returns to its
        # pre-fixture state. The next test that depends on the fixture
        # truncates + re-seeds from scratch.
        conn.rollback()
        conn.close()
