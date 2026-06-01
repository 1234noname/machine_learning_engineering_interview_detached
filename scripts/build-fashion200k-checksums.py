#!/usr/bin/env python3
# AVSA — Fashion200k input-files checksum builder ( rev2 Fix B).
#
# Emits SHA-256 digests for every file the acquisition pipeline reads as
# input:
#   - data/fashion200k/labels/*.txt          (10 files — 5 categories x 2 splits)
#   - data/fashion200k/image_urls.txt        (1 file)
#
# Output format is the standard ``sha256sum`` line format:
#
#     <64-hex>  <relative-path>
#
# sorted by path so two runs against the same inputs produce a
# byte-identical file (commit-friendly).
#
# The resulting file lives at
# ``evals/catalog/fashion200k/inputs-sha256.txt`` and is committed.
# A reproducer can verify their upstream download matches our snapshot
# with:
#
#     (cd data/fashion200k && sha256sum -c ../../evals/catalog/fashion200k/inputs-sha256.txt)
#
# Or, on macOS without GNU coreutils:
#
#     (cd data/fashion200k && shasum -a 256 -c ../../evals/catalog/fashion200k/inputs-sha256.txt)
#
# Pure stdlib — no avsa_api import is needed; this file's surface is
# narrow enough that an in-tree script is the right home.

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = REPO_ROOT / "data" / "fashion200k"
DEFAULT_OUT = REPO_ROOT / "evals" / "catalog" / "fashion200k" / "inputs-sha256.txt"

# Stream files in 1 MiB chunks; image_urls.txt is ~70 MB so we avoid
# loading the whole thing into memory at once.
_CHUNK_BYTES = 1 << 20


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=(
            "Fashion200k data root containing labels/ and image_urls.txt "
            "(default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="Output path for the checksum file (default: %(default)s).",
    )
    return parser.parse_args()


def _sha256(path: Path) -> str:
    """Stream ``path`` through SHA-256; return the lowercase hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _collect_inputs(data_root: Path) -> list[Path]:
    """Find the files this build hashes.

    The set is deliberately closed: 10 label files + image_urls.txt. We
    do not glob the whole directory because that would also pick up
    ``metadata.jsonl`` (a derived artefact, not an input) and the
    ``images/`` tree (404 GB of fetched bytes we never want to hash).
    """
    labels_dir = data_root / "labels"
    if not labels_dir.is_dir():
        raise FileNotFoundError(
            f"labels directory not found at {labels_dir}; "
            "expected the Fashion200k labels/ subdirectory under --data-root."
        )
    label_files = sorted(
        p for p in labels_dir.iterdir() if p.is_file() and p.suffix == ".txt"
    )
    if not label_files:
        raise FileNotFoundError(
            f"no .txt label files found under {labels_dir}; "
            "did you extract the Fashion200k upstream into data/fashion200k/?"
        )

    image_urls = data_root / "image_urls.txt"
    if not image_urls.is_file():
        raise FileNotFoundError(
            f"image_urls.txt not found at {image_urls}; "
            "did you extract the Fashion200k upstream into data/fashion200k/?"
        )

    return [*label_files, image_urls]


def _build_lines(data_root: Path, inputs: list[Path]) -> list[str]:
    """Build the ``<sha256>  <relpath>`` lines, sorted by relpath."""
    lines: list[str] = []
    for path in inputs:
        rel = path.relative_to(data_root).as_posix()
        digest = _sha256(path)
        # Two spaces between digest and path — what sha256sum / shasum -c expect.
        lines.append(f"{digest}  {rel}")
    lines.sort(key=lambda line: line.split("  ", 1)[1])
    return lines


def main() -> int:
    args = _parse_args()
    try:
        inputs = _collect_inputs(args.data_root)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    lines = _build_lines(args.data_root, inputs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"==> wrote {len(lines)} checksum lines to {args.out} "
        f"(data-root={args.data_root})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
