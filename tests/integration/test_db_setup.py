"""Integration test for the  local database substrate.

Runs the migration runner (`infra/migrations/migrate.sh`) against a live
Postgres + pgvector instance and asserts the contract the rest of the system
compiles against: the `catalog` and `conversations` schemas exist and the
`vector` extension is enabled. A second migration run must be a
clean no-op — the spec SQL re-used by each migration is not internally
idempotent (`CREATE TABLE` / `CREATE INDEX` have no `IF NOT EXISTS`), so the
runner's `schema_migrations` ledger is what makes re-runs safe.

Skipped unless `DATABASE_URL` is set, so the default unit-test run (and the
`test-unit` CI job, which has no database) collect it without failing. The
`test-integration` CI job sets `DATABASE_URL` and provides the service
container; `psql` is on PATH there. Written before implementation (test-first,
).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_SCRIPT = REPO_ROOT / "infra" / "migrations" / "migrate.sh"

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL unset; integration test requires a live Postgres",
)


def _run_migrations() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(MIGRATE_SCRIPT)],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )


def _psql_scalar(sql: str) -> str:
    result = subprocess.run(
        ["psql", DATABASE_URL, "-tA", "-c", sql],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    _run_migrations()


class TestDBSetup:
    @pytest.mark.parametrize("schema", ["catalog", "conversations"])
    def test_migration_creates_schema(self, schema: str) -> None:
        present = _psql_scalar(
            f"SELECT 1 FROM information_schema.schemata WHERE schema_name = '{schema}'"
        )
        assert present == "1", f"schema {schema!r} missing after migration"

    def test_vector_extension_enabled(self) -> None:
        present = _psql_scalar("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        assert present == "1", "vector extension not enabled after migration"

    def test_migrations_are_idempotent(self) -> None:
        second = _run_migrations()
        assert second.returncode == 0, (
            "re-running migrations must be a clean no-op:\n"
            f"stdout:\n{second.stdout}\nstderr:\n{second.stderr}"
        )
