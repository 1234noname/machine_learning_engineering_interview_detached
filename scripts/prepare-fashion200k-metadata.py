#!/usr/bin/env python3
# AVSA — Fashion200k metadata builder.
#
# Joins Fashion200k's labels/*_detect_all.txt files with image_urls.txt into
# a single metadata.jsonl that downstream phases (text embeddings, seeder,
# recall eval) consume. The output file is gitignored (per .gitignore /data/);
# only the IDs that select_subset chooses end up in the committed manifest.
#
# Provenance: see STAKEHOLDERS.md § "Source: fashion200k" and ADR-0007.
# Bytes on disk under data/fashion200k/ are never redistributed.

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

from avsa_data.fashion200k_metadata import build_metadata  # noqa: E402

DEFAULT_LABELS_DIR = REPO_ROOT / "data" / "fashion200k" / "labels"
DEFAULT_URLS_FILE = REPO_ROOT / "data" / "fashion200k" / "image_urls.txt"
DEFAULT_OUT = REPO_ROOT / "data" / "fashion200k" / "metadata.jsonl"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--labels-dir",
        type=Path,
        default=DEFAULT_LABELS_DIR,
        help=(
            "Directory containing *_detect_all.txt label files (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--urls-file",
        type=Path,
        default=DEFAULT_URLS_FILE,
        help="image_urls.txt path (default: %(default)s).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Output metadata.jsonl path (default: %(default)s).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.labels_dir.is_dir():
        print(
            f"ERROR: --labels-dir not a directory: {args.labels_dir}. "
            "See scripts/README-acquire-fashion200k.md § Getting the dataset.",
            file=sys.stderr,
        )
        return 2
    if not args.urls_file.is_file():
        print(
            f"ERROR: --urls-file not a file: {args.urls_file}. "
            "See scripts/README-acquire-fashion200k.md § Getting the dataset.",
            file=sys.stderr,
        )
        return 2

    n = build_metadata(
        labels_dir=args.labels_dir,
        urls_file=args.urls_file,
        out_path=args.out,
    )
    print(f"==> wrote {n} rows to {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
