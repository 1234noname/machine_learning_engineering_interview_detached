#!/usr/bin/env bash
# Apply pending AVSA database migrations against $DATABASE_URL.
#
# Each infra/migrations/NNN_*.sql re-uses a spec from specs/db/ verbatim. The
# spec SQL is not internally idempotent (CREATE TABLE / CREATE INDEX have no
# IF NOT EXISTS), so re-running a file would error. This runner records each
# applied file in public.schema_migrations and skips files already recorded —
# that ledger is what makes `just db-migrate` safe to run repeatedly.
#
# Requires psql on PATH (libpq). Used by `just db-migrate`, `just db-reset`,
# the CI test-integration job, and tests/integration/test_db_setup.py.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must be set (see config/avsa.toml [db].url)}"
MIGRATIONS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c "
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);"

shopt -s nullglob
for migration in "$MIGRATIONS_DIR"/[0-9]*.sql; do
    version="$(basename "$migration")"
    applied="$(psql "$DATABASE_URL" -tA \
        -c "SELECT 1 FROM public.schema_migrations WHERE version = '${version}'")"
    if [[ "$applied" == "1" ]]; then
        echo "  skip   ${version}"
        continue
    fi
    echo "  apply  ${version}"
    # --single-transaction wraps the migration AND its ledger insert in one
    # transaction, so a failed migration is never recorded as applied.
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 --single-transaction \
        -f "$migration" \
        -c "INSERT INTO public.schema_migrations (version) VALUES ('${version}');"
done
echo "migrations: up to date"
