-- Migration 005 — add text_embedding column + HNSW index to catalog.products.
-- Fresh databases built from the current specs/db/catalog.sql already have both.
-- This migration only acts when upgrading a database that was initialised before
-- the text_embedding column was added to the spec.
-- Guard pattern mirrors migration 004 (DO $$ BEGIN … END $$).

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'catalog'
      AND table_name   = 'products'
      AND column_name  = 'text_embedding'
  ) THEN
    ALTER TABLE catalog.products ADD COLUMN text_embedding vector(512);
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'catalog'
      AND tablename  = 'products'
      AND indexname  = 'products_text_embedding_hnsw'
  ) THEN
    CREATE INDEX products_text_embedding_hnsw
        ON catalog.products
        USING hnsw (text_embedding vector_cosine_ops);
  END IF;
END $$;
