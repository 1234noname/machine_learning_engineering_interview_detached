# scripts/precompute-embeddings.py — operator runbook

Re-runnable, offline pre-compute of the Fashion200k subset's image + text
embeddings. Reads the subset manifest, embeds every item
through the live model service, and writes a self-identifying artifact under
`data/embeddings/<content_hash>/`. The downstream seeder consumes that
artifact.

The artifact directory is named by a SHA-256 **content hash** over
`(model versions, subset count, dataset version, batch size)` — so re-running
the same subset against the same models lands in the same directory (no manual
versioning), and any input change forks a fresh directory.

## Pre-requisites

- **Model service up.** The avsa-model service must be reachable at
  `--model-url` (default `http://localhost:8000`) and serving `POST /embed`
  (image → 768-d) + `POST /embed_text` (text → 512-d). For *real* embeddings
  run it with `AVSA_MODEL_STUB=0`; the stub returns shape-faithful fakes.
- **Subset manifest present.** `evals/catalog/fashion200k/manifest.json`,
  built by `scripts/acquire-fashion200k.py` (see
  `scripts/README-acquire-fashion200k.md`). The script fails fast (exit 2)
  with a pointer if it is missing.
- **Image bytes present.** Raw bytes under
  `<data-root>/fashion200k/images/<id>.jpg` (landed by the acquisition
  script). Items with no bytes on disk are skipped with a warning.
- **Storage backend.** Uses the local `LocalStorageBackend` rooted at
  `--data-root` (config `[storage] backend = "local"`). `AVSA_STORAGE_HMAC_SECRET`
  is **not** needed for embedding — it gates only `signed_url()` on the image
  proxy, which this pipeline never calls.

## Invocation

```bash
python scripts/precompute-embeddings.py \
  --subset-manifest evals/catalog/fashion200k/manifest.json \
  --verify
```

Flags:

| Flag | Default | Purpose |
|---|---|---|
| `--subset-manifest` | `evals/catalog/fashion200k/manifest.json` | subset manifest to embed. |
| `--data-root` | `./data` | Backend root: reads `fashion200k/images/`, writes `embeddings/<hash>/`. |
| `--model-url` | `http://localhost:8000` | Base URL of the avsa-model service. |
| `--batch-size` | `16` | Items per `/embed` + `/embed_text` request. Flows into the content hash. |
| `--concurrency` | `4` | Reserved for future batch-level parallelism. |
| `--verify` | off | Run the sample-equivalence gate after writing (see below). |
| `--verify-sample-size` | `5` | Rows to re-embed for `--verify` (capped at item count). |
| `--config` | `config/avsa.toml` | Config file carrying the `--verify` cosine floor. |

## The artifact

Lands under `<data-root>/embeddings/<content_hash>/`:

- **`embeddings.jsonl`** — one JSON object per row,
  `{"id", "image_embedding": [f32; 768], "text_embedding": [f32; 512]}`,
  in input order so consumers can index by position.
- **`manifest.json`** — pretty-printed sidecar pinning
  `model_version_image`, `model_version_text`, `image_dim` (768),
  `text_dim` (512), `item_count`, `content_hash`, and `generated_at`.

`data/` is gitignored — embeddings never enter git (Fashion200k is
non-redistributable; see ADR-0007 and STAKEHOLDERS.md).

## Reproducibility

The output directory is `data/embeddings/<content_hash>/`, where the hash is a
deterministic SHA-256 over `(model_version_image, model_version_text,
subset_count, dataset_version, batch_size)`. Same inputs → same hash → same
directory (idempotent re-run). Bump any input (a new model version, a
different subset, a different batch size) and the artifact forks into a new
directory automatically — no manual version bookkeeping.

## The `--verify` equivalence gate (DoD)

`--verify` is a post-write quality gate. After the artifact lands, it:

1. Takes the first `--verify-sample-size` rows (default 5) from the
   just-written artifact.
2. Re-embeds each sampled row through the **live** model service —
   `POST /embed` for the image bytes, `POST /embed_text` for the text.
3. Computes cosine similarity between each freshly-computed vector and the
   vector stored in the artifact, for **both** modalities.
4. Asserts every sample/modality is at or above the **config-driven** floor
   `[evals.embedding] equivalence_min_cosine` in `config/avsa.toml`
   (default `0.9999`). The threshold is never hardcoded.

On all-pass it prints a confirmation line with the minimum observed cosine and
exits 0. On any sample below the floor it prints the offending sample id,
modality, and cosine to stderr and exits **non-zero** — catching **model-output
divergence**: model-version drift mid-run or non-determinism, before the seeder
ingests the artifact.

> Scope note: the comparison is against the vectors pre-compute produced in this
> run (held in memory, identical to what was persisted), not against the bytes
> re-read from `embeddings.jsonl`. So `--verify` gates model-output equivalence,
> not on-disk serialization integrity. Re-reading the artifact before comparison
> would extend it to catch a JSONL float-truncation/round-trip bug — tracked as a
> future improvement in `issues/072-061-housekeeping.md`.

`--verify` requires the live model service. CI exercises it against a mocked
model (`apps/api/tests/test_precompute_cli.py`).

## Worked example

```bash
# Real embeddings (model service running with AVSA_MODEL_STUB=0):
python scripts/precompute-embeddings.py \
  --subset-manifest evals/catalog/fashion200k/manifest.json \
  --model-url http://localhost:8000 \
  --batch-size 16 \
  --verify
# ==> embeddings written: item_count=15000 image_dim=768 text_dim=512 \
#     content_hash=<sha256> path=…/data/embeddings/<sha256>
# ==> verify OK: 5 sample(s) x 2 modalities all >= min cosine 0.9999 \
#     (min observed cosine=1.000000).
```

## Troubleshooting

- **Exit 2 — "subset manifest not found"**: build it first with
  `scripts/acquire-fashion200k.py` (see its README).
- **"skipped N item(s) with no image bytes"**: re-run the acquisition script
  to backfill `data/fashion200k/images/`.
- **`--verify` exits non-zero**: the named sample's live re-embed diverged
  from the stored vector below `equivalence_min_cosine`. Check the model
  service is the same version recorded in `manifest.json`, then re-run the
  pre-compute to rebuild the artifact.
