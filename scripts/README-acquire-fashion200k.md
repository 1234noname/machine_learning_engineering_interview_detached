# scripts/acquire-fashion200k.py — operator runbook

Re-runnable acquisition of a deterministic Fashion200k subset into the
local `LocalStorageBackend`. The manifest (IDs + provenance) is
committed; the bytes are not (`./data/` is gitignored).

The workflow is two stages:

1. **Prepare metadata** — `scripts/prepare-fashion200k-metadata.py` joins
   the raw Fashion200k label files + URL list into `metadata.jsonl`.
2. **Acquire bytes** — `scripts/acquire-fashion200k.py` reads
   `metadata.jsonl`, picks a deterministic subset, writes the manifest
   (committed), and fetches images via each row's `source_url`
   (gitignored).

## Getting the dataset

Fashion200k is a third-party research dataset; this repo does not
redistribute its bytes (see ADR-0007 for license posture). To obtain it:

- **Upstream:** <https://github.com/xthan/fashion200k> — the author's
  README links to a Google Drive download for the labels + URL list, and
  to a separate Drive for thumbnail images.
- **Community mirrors:** various academic groups host re-builds; check
  the upstream issue tracker for current pointers when the Drive link
  rotates.

Download the dataset and extract it into `data/fashion200k/`:

```
data/fashion200k/
├── labels/
│   ├── dress_test_detect_all.txt
│   ├── dress_train_detect_all.txt
│   ├── jacket_test_detect_all.txt
│   ├── jacket_train_detect_all.txt
│   ├── pants_test_detect_all.txt
│   ├── pants_train_detect_all.txt
│   ├── skirt_test_detect_all.txt
│   ├── skirt_train_detect_all.txt
│   ├── top_test_detect_all.txt
│   └── top_train_detect_all.txt
├── image_urls.txt
├── LICENSE             (verbatim from upstream)
└── README.md           (verbatim from upstream)
```

Schema of the raw files:

- `labels/<category>_<split>_detect_all.txt` — 3 tab-delimited columns:
  `image_path \t detection_score \t description`. The filename encodes
  the category (`dress` / `jacket` / `pants` / `skirt` / `top`) and the
  split (`train` / `test`).
- `image_urls.txt` — 2 tab-delimited columns:
  `image_path \t source_url`.

`data/` is gitignored — nothing under `data/fashion200k/` is committed.

> **Reproducing the committed Phase-1 subset?** See the
> [Reproducing the committed Phase-1 subset](#reproducing-the-committed-phase-1-subset)
> section below for the verification + re-build recipe.

## Pre-requisites (acquisition stage)

- `AVSA_STORAGE_HMAC_SECRET` exported in your shell (any non-empty
  string; treat it like a credential — never commit). The script
  fails fast with exit-code 2 if it is missing.
- `data/fashion200k/metadata.jsonl` present (built by the prepare step
  below). The acquisition script fails fast with a pointer to the
  prepare step if absent.
- Network access to the source hosts listed in `metadata.jsonl`
  (per-row `source_url`).
- ~5–10 GB free disk under `./data/` for the 15,000-item default
  subset. Bytes land at `./data/fashion200k/images/<id>.jpg`.

## Worked example

```bash
# Stage 1 — build metadata.jsonl from the raw label files + URL list.
# Defaults read from data/fashion200k/labels and data/fashion200k/image_urls.txt
# and write to data/fashion200k/metadata.jsonl.
python scripts/prepare-fashion200k-metadata.py
# ==> wrote 201824 rows to /…/avsa/data/fashion200k/metadata.jsonl

# Stage 2 — pick the deterministic subset, write the manifest, fetch bytes.
export AVSA_STORAGE_HMAC_SECRET=<your-local-secret>
python scripts/acquire-fashion200k.py \
  --seed 17 \
  --count 15000 \
  --out evals/catalog/fashion200k/manifest.json
# ==> metadata=…/metadata.jsonl universe=201824 selected=15000 manifest=…/manifest.json (seed=17)
# ==> outcomes: fetched=15000 skipped=0 failed=0 in 312.4s (metadata=…/metadata.jsonl, universe=201824, selected=15000)
```

## Override: point at a local mirror

When developing offline (or when the upstream hosts rate-limit you), run
a local HTTP server that serves the IDs and pass `--source-url-template`
to override each row's URL:

```bash
python scripts/acquire-fashion200k.py \
  --source-url-template 'http://localhost:8765/{id}' \
  --count 100
```

The `{id}` placeholder is substituted with the metadata row's `id`
(e.g. `women/dresses/casual_and_day_dresses/56037632/56037632_0.jpeg`).
When `--source-url-template` is unset, each row's `source_url` is used
verbatim.

## Expected duration

- Prepare: seconds (single pass over ~200k label rows + ~338k URL rows).
- Acquisition (full 15,000-item fetch): minutes to hours depending on
  bandwidth and the third-party hosts' rate limits. Concurrency
  defaults to 8 (tune with `--concurrency`).

## Re-running

Safe and idempotent. The acquisition script:

- Re-derives the same ID subset for a given `--seed` / `--count` /
  universe; the manifest is overwritten byte-for-byte if inputs
  haven't changed.
- Asks the backend whether each image is already present and skips
  the HTTP fetch if so (no double-download, no overwrite of existing
  bytes).

A re-run after a partial failure resumes — only the previously-failed
IDs incur a fetch.

The prepare script is also idempotent — running it twice produces a
byte-identical `metadata.jsonl` (modulo upstream changes to the raw
files).

## Where things land

| Artefact | Location | Committed? |
|---|---|---|
| Raw label files | `./data/fashion200k/labels/*_detect_all.txt` | No (gitignored) |
| Raw URL list | `./data/fashion200k/image_urls.txt` | No (gitignored) |
| `metadata.jsonl` | `./data/fashion200k/metadata.jsonl` | No (gitignored) |
| Bytes (`<id>.jpg`) | `./data/fashion200k/images/<id>.jpg` | No (gitignored) |
| Subset manifest | `--out` path (default `evals/catalog/fashion200k/manifest.json`) | **Yes** (overwrites the template) |
| HMAC secret | `$AVSA_STORAGE_HMAC_SECRET` (process env) | **Never** |

The committed manifest is regenerated in place on each run; the rev2
schema is `{seed, criteria, dataset_version, items: [{id, category,
title, source_url, split}, …]}` (sorted by id). Changing `--seed` /
`--count` overwrites the file with the new selection.

## Provenance and license

See [`STAKEHOLDERS.md` § Source: `fashion200k`](../STAKEHOLDERS.md)
and [`docs/adr/0007-catalog-dataset-fashion200k.md`](../docs/adr/0007-catalog-dataset-fashion200k.md)
for license posture: Fashion200k is **non-redistributable**, so the
bytes never enter git and the production-serving proxy is
HMAC-signed. Any redistribution requires a license review.

## Reproducing the committed Phase-1 subset

The 15,000-item subset materialised at
`evals/catalog/fashion200k/manifest.json` is fully determined by
`(seed=17, count=15000, available_ids)`. The first two are constants on
the CLI; the third is whatever set of IDs the upstream Fashion200k labels
happen to expose on the day you run the build. That last piece is the
load-bearing variable for reproducibility.

To stand the same subset up on another machine:

### 1. Acquire the dataset

Download Fashion200k from <https://github.com/xthan/fashion200k>. The
upstream is unmirrored under licence (see
[ADR-0007](../docs/adr/0007-catalog-dataset-fashion200k.md)); if the
Drive links are dead, ask the project owner for access or check
community mirrors (Kaggle / HuggingFace Datasets — verify provenance
before trusting the bytes).

Extract into `data/fashion200k/` with the layout described in
[Getting the dataset](#getting-the-dataset) above.

### 2. Verify your download matches our reference

The committed file
`evals/catalog/fashion200k/inputs-sha256.txt` records SHA-256 digests
for the exact label files + URL list this build was made against (10
`labels/*_detect_all.txt` + `image_urls.txt`). Verify your download:

```bash
cd /Users/<you>/avsa
(cd data/fashion200k && sha256sum -c ../../evals/catalog/fashion200k/inputs-sha256.txt)
```

On macOS without GNU coreutils, the equivalent is:

```bash
(cd data/fashion200k && shasum -a 256 -c ../../evals/catalog/fashion200k/inputs-sha256.txt)
```

You should see one `: OK` line per input (11 total).

If any line reports `FAILED`, the upstream you downloaded has drifted
from the one we built against. Even one differing byte means the 15k
subset will not be byte-identical to ours under the same seed — because
`select_subset` runs `random.sample` over the full universe, a single
added or removed ID shifts the entire chosen set. Your options:

- Get the same upstream snapshot we used (ask the owner).
- Accept that your subset will diverge — your regenerated
  `manifest.json` will differ from the committed one — and treat that
  divergence as a deliberate project decision (note it in a follow-up).

### 3. Run the build

Once the inputs check out, regenerate the subset:

```bash
export AVSA_STORAGE_HMAC_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
python3 scripts/prepare-fashion200k-metadata.py
python3 scripts/acquire-fashion200k.py \
  --seed 17 \
  --count 15000 \
  --out evals/catalog/fashion200k/manifest.json
```

If `sha256sum -c` was clean and `git diff evals/catalog/fashion200k/manifest.json`
shows no change, you have a byte-identical reproduction.

### 4. URL rot

The Fashion200k image URLs live on retailer CDNs (mostly `lystit.com`).
A 9-year-old dataset has rot — our own run already saw 1/15,000 dead.
If your run yields significantly more `failed` outcomes, the per-item
`source_url` recorded in the manifest tells you exactly which IDs were
intended (so you can quantify the gap rather than silently end up with
a smaller subset).

### 5. Known reproducibility limits

- **Upstream availability.** We do not mirror the Fashion200k dataset
  (ADR-0007 non-redistribution). If `xthan/fashion200k` is unreachable
  and you have no community mirror, the build is not reproducible by
  you.
- **URL rot.** As above — a fraction of URLs die each year.
- **Universe drift.** Even one ID added to or removed from the upstream
  labels shifts the `select_subset(seed=17, ...)` output across the
  entire 15k subset (because `random.sample` partitions over the full
  universe). The `inputs-sha256.txt` file makes drift *detectable* but
  not *recoverable*.
- **No private snapshot.** A future issue may stand up a private byte
  snapshot (private GCS bucket, signed-URL contract) for trusted
  collaborators. Out of scope for this branch — the project currently
  runs local-only, no production hosting.

## Troubleshooting

- **Exit code 2 — "AVSA_STORAGE_HMAC_SECRET is not set"**: export the
  env var before running. The script enforces this so a downstream
  request to the image proxy does not crash at request time.
- **Exit code 2 — "metadata file not found"**: run
  `scripts/prepare-fashion200k-metadata.py` first to build
  `data/fashion200k/metadata.jsonl` from the raw label files + URL
  list (see Getting the dataset above).
- **Many `failed` results**: the source hosts may be rate-limiting;
  lower `--concurrency` and re-run (the skip-existing path means the
  retry is cheap).
- **Manifest churns on every run**: confirm `--seed`, `--count`, and
  the underlying ID universe (metadata file) are stable;
  `write_manifest` sorts the IDs before serialising so any churn
  signals an upstream change.
