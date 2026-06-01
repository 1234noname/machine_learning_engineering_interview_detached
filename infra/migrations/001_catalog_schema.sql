-- Migration 001 — catalog schema.
-- Re-uses specs/db/catalog.sql verbatim via psql include-relative (\ir); the
-- spec is the single source of truth and must not be duplicated here.
-- Idempotency across re-runs is provided by the migrate.sh ledger, not by
-- this file (the spec's CREATE TABLE / CREATE INDEX are not IF NOT EXISTS).
\ir ../../specs/db/catalog.sql
