-- specs/db/catalog.sql
-- ---------------------------------------------------------------------------
--  catalog schema. Defines the products table that pgvector kNN
-- queries against, plus the two HNSW indexes that make those queries fast.
--
-- Contract — every catalog ingest job and retrieval query must agree on:
--   - 768-dim image embedding (ViT-base output)
--   - 512-dim text embedding (optional; ingest items without usable text
--     descriptions still land — the column is nullable on purpose)
--   - HNSW + vector_cosine_ops for both embedding columns
--
-- pgvector ≥ 0.5.0 is required for HNSW (the CI spec-validate-sql job
-- pins a pgvector-enabled Postgres image that satisfies this).
-- ---------------------------------------------------------------------------

-- The vector extension must load before any CREATE TABLE that references
-- the vector(N) type, otherwise the type resolver errors out.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS catalog;

CREATE TABLE catalog.products (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    category        TEXT NOT NULL,
    colour          TEXT NOT NULL,
    formality       TEXT NOT NULL,
    occasion        TEXT NOT NULL,
    price_cents     INTEGER NOT NULL,
    image_url       TEXT NOT NULL,
    embedding       vector(768) NOT NULL,
    text_embedding  vector(512),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HNSW + cosine for the primary (image) embedding. Required for
-- 's find_similar tool to meet its 150 ms p95 retrieval budget.
CREATE INDEX products_embedding_hnsw
    ON catalog.products
    USING hnsw (embedding vector_cosine_ops);

-- Second HNSW index over the text tower. Used by text-driven discovery
-- () and the hybrid scorer planned in .
CREATE INDEX products_text_embedding_hnsw
    ON catalog.products
    USING hnsw (text_embedding vector_cosine_ops);
