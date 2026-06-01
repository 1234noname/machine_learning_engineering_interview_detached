#!/usr/bin/env python3
# AVSA — Fashion200k subset acquisition.
#
# Re-runnable CLI that:
#   1. Loads the Fashion200k metadata (--metadata-file; produced by
#      scripts/prepare-fashion200k-metadata.py).
#   2. Picks a deterministic ID subset (select_subset) over the metadata's
#      ID universe.
#   3. Concurrently fetches each image into a StorageBackend (acquire_image),
#      using each row's source_url (or --source-url-template override).
#   4. Writes a self-describing JSON manifest of the chosen items
#      ({id, category, title, source_url, split} per row; sorted by id) via
#      write_manifest, overwriting --out. Schema is the rev2 self-describing
#      shape — a reproducer can fetch every URL from the manifest alone,
#      without re-downloading the Fashion200k labels.
#
# Idempotent: re-running skips images already present in the backend.
# Bytes land under ./data/fashion200k/images/<id>.jpg (gitignored). The
# manifest at --out IS committed; the bytes are not.
#
# Provenance + license: see STAKEHOLDERS.md § "Source: fashion200k" and
# docs/adr/0007-catalog-dataset-fashion200k.md. The Fashion200k images are
# non-redistributable; this script never publishes them, only fetches into
# a private local store gated behind an HMAC-signed proxy.
#
# Pre-requisites:
#   - $AVSA_STORAGE_HMAC_SECRET set (consumed by LocalStorageBackend.signed_url
#     downstream; acquire_image itself does not sign, but the integrated path
#     does — fail fast if absent so operators do not discover this at request
#     time).
#   - data/fashion200k/metadata.jsonl exists. Build it via
#     scripts/prepare-fashion200k-metadata.py; see
#     scripts/README-acquire-fashion200k.md.

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

from avsa_core.storage.local import LocalStorageBackend  # noqa: E402
from avsa_data.acquisition import (  # noqa: E402
    DEFAULT_SUBSET_COUNT,
    AcquisitionResult,
    acquire_image,
    select_subset,
    write_manifest,
)
from avsa_data.fashion200k_metadata import load_metadata  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "evals" / "catalog" / "fashion200k" / "manifest.json"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"
DEFAULT_METADATA = REPO_ROOT / "data" / "fashion200k" / "metadata.jsonl"
DEFAULT_SEED = 17
DATASET_VERSION = "fashion200k-v1.0"

# Concurrency limit for parallel image fetches. 8 is a polite default that
# keeps the third-party origin happy and matches the existing batcher
# default in config/avsa.toml [batcher.max_batch_size].
DEFAULT_CONCURRENCY = 8


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="RNG seed for deterministic subset selection (default: %(default)s).",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_SUBSET_COUNT,
        help="Number of items to acquire (default: %(default)s).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Manifest output path (default: %(default)s).",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=(
            "Backend root under which fashion200k/images/<id>.jpg lands "
            "(default: %(default)s; gitignored)."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Max parallel HTTP fetches (default: %(default)s).",
    )
    parser.add_argument(
        "--metadata-file",
        type=Path,
        default=DEFAULT_METADATA,
        help=(
            "Path to Fashion200k metadata.jsonl (default: %(default)s). "
            "Build it via scripts/prepare-fashion200k-metadata.py."
        ),
    )
    parser.add_argument(
        "--source-url-template",
        default=None,
        help=(
            "Optional override for per-row source_url. Contains '{id}' (e.g. "
            "'http://localhost:8765/{id}'). When unset (default), the URL "
            "for each item comes from its metadata row's source_url."
        ),
    )
    return parser.parse_args()


def _load_metadata_rows(path: Path) -> list[dict[str, object]]:
    """Read every row from the metadata JSONL into memory.

    ~200k rows x ~350 bytes/row → ~70 MB; comfortable for the operator
    laptop this CLI runs on. Streaming is unnecessary here because the
    subsequent ``select_subset`` and ID→URL lookup both need random access.
    """
    return list(load_metadata(path))


async def _run(args: argparse.Namespace) -> int:
    # Fail fast on HMAC misconfiguration — the integrated path ('s
    # /images proxy) requires it, and discovering this at request time is
    # worse than discovering it here.
    if not os.environ.get("AVSA_STORAGE_HMAC_SECRET"):
        print(
            "ERROR: AVSA_STORAGE_HMAC_SECRET is not set. The image proxy needs it; "
            "see scripts/README-acquire-fashion200k.md.",
            file=sys.stderr,
        )
        return 2

    if not args.metadata_file.exists():
        print(
            f"ERROR: metadata file not found at {args.metadata_file}. "
            "Run scripts/prepare-fashion200k-metadata.py first; "
            "see scripts/README-acquire-fashion200k.md.",
            file=sys.stderr,
        )
        return 2

    backend = LocalStorageBackend(root=args.data_root)

    rows = _load_metadata_rows(args.metadata_file)
    if not rows:
        print(
            f"ERROR: metadata file {args.metadata_file} is empty; nothing to acquire.",
            file=sys.stderr,
        )
        return 2

    # Index rows by id so we can look up the rich fields after subset
    # selection. String coercion guards against JSONL rows that
    # accidentally hold non-string ids.
    row_by_id: dict[str, dict[str, object]] = {str(r["id"]): r for r in rows}
    universe = list(row_by_id.keys())

    criteria = {"count": args.count}
    chosen = select_subset(seed=args.seed, criteria=criteria, available_ids=universe)

    def _resolve_url(item_id: str) -> str:
        # --source-url-template, when supplied, overrides the per-row URL —
        # operator's escape hatch for pointing at a local mirror during
        # development. The manifest captures whichever URL was actually
        # fetched against, so a reproducer can see exactly which CDN
        # endpoints they need.
        if args.source_url_template is not None:
            return str(args.source_url_template.format(id=item_id))
        return str(row_by_id[item_id]["source_url"])

    # Build the rich items list for the manifest. We drop ``description``
    # (a redundant copy of ``title`` for Fashion200k) and
    # ``detection_score`` (not load-bearing for any downstream phase) to
    # keep the manifest narrow; ``split`` stays because's eval-split
    # logic reads it.
    manifest_items: list[dict[str, object]] = [
        {
            "id": item_id,
            "category": row_by_id[item_id]["category"],
            "title": row_by_id[item_id]["title"],
            "source_url": _resolve_url(item_id),
            "split": row_by_id[item_id]["split"],
        }
        for item_id in chosen
    ]

    write_manifest(
        path=args.out,
        items=manifest_items,
        seed=args.seed,
        criteria=criteria,
        dataset_version=DATASET_VERSION,
    )
    print(
        f"==> metadata={args.metadata_file} universe={len(universe)} "
        f"selected={len(chosen)} manifest={args.out} "
        f"(seed={args.seed}, schema=rev2-self-describing)",
        flush=True,
    )

    semaphore = asyncio.Semaphore(args.concurrency)
    started = time.perf_counter()

    # The fetch loop uses the same per-item URL the manifest recorded,
    # so the manifest is a faithful record of what was actually fetched.
    url_by_id: dict[str, str] = {
        str(entry["id"]): str(entry["source_url"]) for entry in manifest_items
    }

    async def _one(item_id: str) -> AcquisitionResult:
        url = url_by_id[item_id]
        async with semaphore:
            return await acquire_image(item_id=item_id, source_url=url, backend=backend)

    results = await asyncio.gather(*(_one(item_id) for item_id in chosen))
    elapsed = time.perf_counter() - started

    fetched = sum(1 for r in results if r == AcquisitionResult.fetched)
    skipped = sum(1 for r in results if r == AcquisitionResult.skipped_existing)
    failed = sum(1 for r in results if r == AcquisitionResult.failed)
    print(
        f"==> outcomes: fetched={fetched} skipped={skipped} failed={failed} "
        f"in {elapsed:.1f}s (metadata={args.metadata_file}, "
        f"universe={len(universe)}, selected={len(chosen)})",
        flush=True,
    )
    return 0 if failed == 0 else 1


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
