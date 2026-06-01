-- Migration 004 — rename price_pence to price_cents.
-- ZAR (South African Rand) uses cents as its minor unit; "pence" was a
-- British holdover from the initial schema draft.
-- Guard: fresh databases built from the updated spec already have price_cents;
-- the rename is only needed when upgrading a schema that was created before
-- this migration existed.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'catalog'
      AND table_name  = 'products'
      AND column_name = 'price_pence'
  ) THEN
    ALTER TABLE catalog.products RENAME COLUMN price_pence TO price_cents;
  END IF;
END $$;
