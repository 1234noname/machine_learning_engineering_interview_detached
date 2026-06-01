# scripts/train-attribute-heads.py — operator runbook

Re-runnable, offline training of the `category` + `colour` linear attribute
probes over the **frozen** 768-d ViT-b-16 image features ( /
). Reads the embedding artifact, derives per-image labels from
the manifest, fits one ridge least-squares head per attribute on a
product-level held-out split, writes a versioned head-weights artifact under
`data/attribute_heads/<config_hash>/`, and emits the accuracy report that the
committed `evals/attributes/baseline/baseline.toml` gate floors are derived
from. The downstream dual-head model service loads the head artifact.

No backbone is loaded or fine-tuned: the probe is a single linear layer over
the pre-computed features, so training is CPU-cheap and numpy-only.

The artifact directory is named by a SHA-256 **config hash** over
`(image model version, source embedding-artifact hash, attributes, seed,
test_frac, probe method)` — so re-running the same training config against the
same features lands in the same directory (no manual versioning), and any input
change forks a fresh one. The split is seeded and the ridge solve is
closed-form, so **re-runs reproduce identical accuracy, row counts, and hashes**.

## Pre-requisites

- **Embedding artifact present.** A directory
  `data/embeddings/<content_hash>/` (gitignored) holding `embeddings.jsonl` +
  `manifest.json`, built by `scripts/precompute-embeddings.py` (see
  `scripts/README-precompute-embeddings.md`). Pass its directory as
  `--artifact`; it is read through the sanctioned
  `avsa_data.embedding_pipeline.load_embedding_artifact` reader.
- **Subset manifest present.** `evals/catalog/fashion200k/manifest.json`,
  carrying `category` + `title` per image id. The script fails fast (exit 2)
  with a pointer if it is missing.
- **Storage backend.** Uses the local `LocalStorageBackend`. The backend is
  rooted at `--data-root`; when `--artifact` sits under `--data-root` the head
  artifact lands beside it under `attribute_heads/<config_hash>/`. No
  `AVSA_STORAGE_HMAC_SECRET` is needed — this path never signs URLs.

## Invocation

```bash
python scripts/train-attribute-heads.py \
  --artifact data/embeddings/<content_hash>/ \
  --data-root ./data \
  --manifest evals/catalog/fashion200k/manifest.json
```

Flags:

| Flag | Default | Purpose |
|---|---|---|
| `--artifact` | (required) | embedding-artifact directory to train on. |
| `--manifest` | `evals/catalog/fashion200k/manifest.json` | subset manifest (category + title). |
| `--data-root` | `./data` | Backend root receiving `attribute_heads/<config_hash>/`. |
| `--seed` | `17` | Seed for the product-level split (flows into the config hash). |
| `--test-frac` | `0.2` | Held-out fraction by product (flows into the config hash). |

## What it does

1. Loads the embedding artifact (frozen 768-d `image_embedding` rows +
   manifest) via `load_embedding_artifact`.
2. Derives labels from the manifest: `category` verbatim; `colour` via the
   catalog colour vocabulary (`catalog_fashion200k._colour_from_title` — reused,
   never re-implemented, so the seeder's and the probe's colour labels cannot
   drift). Colour is description-derived and therefore noisier — see the caveat
   in `baseline.toml` and the provenance note in `STAKEHOLDERS.md`.
3. Splits **by product** (`split_by_product`, seeded): the leakage boundary is
   the numeric-ID directory in each image id, so every image of one product
   lands wholly in train or wholly in test.
4. Fits both heads (`train_linear_probe`, ridge least-squares closed form) and
   evaluates held-out top-1 accuracy (`evaluate`).
5. Writes the head artifact (`write_head_artifact`) — `<attribute>.npz`
   (weights + bias), `<attribute>.labels.json` (class-index → name), and
   `manifest.json` — under `data/attribute_heads/<config_hash>/`.
6. Prints a one-line summary and the full accuracy report (the body of
   `baseline.toml`) to stdout.

## The head artifact

Lands under `<data-root>/attribute_heads/<config_hash>/`:

- **`<attribute>.npz`** — the head's weight matrix `(768, n_classes)` + bias,
  via `numpy.savez`. Read back with `allow_pickle=False` (explicit no-pickle
  posture; the model service loads these from dataset-derived bytes).
- **`<attribute>.labels.json`** — `{class_index: class_name}`.
- **`manifest.json`** — model version, image_dim (768), per-attribute class
  counts, the `<config_hash>`, and `generated_at`.

`data/` is gitignored — head weights are private derived data (derived from the
non-redistributable Fashion200k embeddings; see ADR-0007 and STAKEHOLDERS.md)
and **never** enter git. Only the accuracy **metrics**
(`evals/attributes/baseline/baseline.toml`) are committed.

## Reproducibility + the committed baseline

`baseline.toml` is **regenerated from this script's stdout**, not hand-written.
The committed values are regression-gate floors: each floor = the observed
held-out top-1 minus a `0.05` slack (clamped `>= 0`). The category floor is the
hard quality gate for ; colour is reported-only because its
labels are description-derived.

To reproduce / recalibrate the baseline:

```bash
python scripts/train-attribute-heads.py \
  --artifact data/embeddings/7decaeecdc769a1a4ab2e758684f740c5607bef0c07e9ac3cc027936f25cb899/ \
  --data-root ./data
# ==> attribute heads written: attributes=['category', 'colour'] train_rows=4000 \
#     test_rows=1000 category_top1=0.8690 colour_top1=0.5240 \
#     config_hash=e8bcf7394f30b59124167d70b14a7283e28aa614e40e567284c0a8295c2db2de ...
# ==> accuracy report (paste into evals/attributes/baseline/baseline.toml):
#     <the TOML body follows on stdout>
```

Same artifact + same seed + same test_frac → byte-identical numbers and the
same `<config_hash>`. If the observed numbers move, paste the new report body
into `baseline.toml` (the floors fall out of the `0.05` slack automatically).

## Troubleshooting

- **Exit 2 — "subset manifest not found"**: build it first with
  `scripts/acquire-fashion200k.py` (see its README).
- **"image id … is in the embedding artifact but has no label in the
  manifest"**: the `--artifact` and `--manifest` are out of sync (different
  subsets). Re-run `precompute-embeddings.py` against the same manifest, or pass
  the manifest the embeddings were built from.
- **A different `<config_hash>` than expected**: an input changed (image model
  version, source artifact hash, attributes, seed, or test_frac). That is the
  intended fork-on-change behaviour — the hash is the rebuild signal.
