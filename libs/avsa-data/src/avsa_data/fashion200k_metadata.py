"""Fashion200k metadata pathway — pure functions.

Joins Fashion200k's ``labels/*_detect_all.txt`` files (3 tab-delimited columns
``image_path \\t detection_score \\t description``) with ``image_urls.txt``
(2 tab-delimited columns ``image_path \\t source_url``) into a single
``metadata.jsonl`` with the canonical row schema:

    {id, category, title, description, source_url, detection_score, split}

``category`` and ``split`` are derived from the labels filename — e.g.
``dress_train_detect_all.txt`` → category ``"dress"`` / split ``"train"``.
The five known categories are ``dress``, ``jacket``, ``pants``, ``skirt``,
``top``; the two splits are ``train`` and ``test``.

No network calls; no storage-backend round-trip. The output file can be
~64 MB (~200k rows), so ``build_metadata`` writes line-by-line directly to
disk rather than buffering through ``LocalStorageBackend.put_object`` (which
takes ``bytes`` and would need the whole file in memory).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

# Filenames follow ``{category}_{split}_detect_all.txt`` — the suffix below
# is sliced off to extract the (category, split) pair from the filename.
_LABEL_FILENAME_SUFFIX = "_detect_all.txt"


class MetadataRow(TypedDict):
    """Canonical Fashion200k metadata row.

    Frozen by the on-disk ``metadata.jsonl`` the orchestrator already built;
    downstream consumers ( text embeddings seeder recall
    eval) read these keys.
    """

    id: str
    category: str
    title: str
    description: str
    source_url: str
    detection_score: float
    split: str


def _parse_label_filename(name: str) -> tuple[str, str] | None:
    """Return ``(category, split)`` for ``dress_train_detect_all.txt`` style names.

    Returns ``None`` for any filename that does not match the
    ``{category}_{split}_detect_all.txt`` pattern (caller should skip).
    """
    if not name.endswith(_LABEL_FILENAME_SUFFIX):
        return None
    stem = name[: -len(_LABEL_FILENAME_SUFFIX)]
    parts = stem.split("_")
    if len(parts) != 2:
        return None
    category, split = parts
    return category, split


def parse_labels(labels_dir: Path) -> dict[str, dict[str, object]]:
    """Read every ``*_detect_all.txt`` file under ``labels_dir`` into a dict.

    Returns ``{image_path: {category, title, description, detection_score, split}}``.

    First-occurrence wins: filenames are visited in lexicographic order so
    the result is deterministic; if the same ``image_path`` appears in two
    files, the row from the alphabetically-earlier filename wins.

    Malformed rows (wrong column count, unparsable score) are silently
    skipped rather than crashing the join — same posture as the existing
    on-disk reality (14 of 201,838 source rows drop today).
    """
    rows: dict[str, dict[str, object]] = {}
    for path in sorted(labels_dir.iterdir()):
        if not path.is_file():
            continue
        parsed = _parse_label_filename(path.name)
        if parsed is None:
            continue
        category, split = parsed
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                cols = line.split("\t")
                if len(cols) != 3:
                    continue
                image_path, score_str, description = cols
                # Some upstream rows carry trailing whitespace in the
                # description (e.g. "white a-line dress "); strip so the
                # joined metadata matches the canonical on-disk file
                # byte-for-byte.
                description = description.strip()
                try:
                    score = float(score_str)
                except ValueError:
                    continue
                if image_path in rows:
                    # First occurrence wins; later duplicates are ignored.
                    continue
                rows[image_path] = {
                    "category": category,
                    "title": description,
                    "description": description,
                    "detection_score": score,
                    "split": split,
                }
    return rows


def parse_image_urls(urls_file: Path) -> dict[str, str]:
    """Read ``image_urls.txt`` into ``{image_path: source_url}``.

    Two tab-delimited columns. Malformed rows are skipped. First-occurrence
    wins on duplicate image paths.
    """
    out: dict[str, str] = {}
    with urls_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            cols = line.split("\t")
            if len(cols) != 2:
                continue
            image_path, url = cols
            if image_path in out:
                continue
            out[image_path] = url
    return out


def build_metadata(labels_dir: Path, urls_file: Path, out_path: Path) -> int:
    """Join labels + URLs into a JSONL file at ``out_path``; return row count.

    Label rows with no matching URL are dropped (the contract is explicit even
    though it's a no-op for the real Fashion200k corpus today).

    Output ordering matches ``parse_labels`` insertion order (alphabetic by
    filename, then file-row order within each file). The key order per row
    matches the on-disk ``metadata.jsonl`` produced out-of-band by the
    orchestrator, so a freshly-built file diffs cleanly against the existing
    one.

    Writes line-by-line; safe for ~200k rows / ~64 MB outputs.
    """
    labels = parse_labels(labels_dir)
    urls = parse_image_urls(urls_file)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    n = 0
    with out_path.open("w", encoding="utf-8") as f:
        for image_path, label in labels.items():
            url = urls.get(image_path)
            if url is None:
                continue
            # Key order matches the on-disk metadata.jsonl: id, category,
            # title, description, source_url, detection_score, split.
            row = {
                "id": image_path,
                "category": label["category"],
                "title": label["title"],
                "description": label["description"],
                "source_url": url,
                "detection_score": label["detection_score"],
                "split": label["split"],
            }
            # ensure_ascii=False preserves UTF-8 characters (é, ñ, …) literally
            # rather than escaping them as é sequences. Matches the
            # on-disk metadata.jsonl the orchestrator built; downstream
            # diff-based reproducibility checks rely on byte-equality.
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def load_metadata(path: Path) -> Iterator[dict[str, object]]:
    """Stream rows from a metadata JSONL file as dicts.

    Yields one dict per line; skips blank lines. Caller is responsible for
    type-narrowing the value types per key.
    """
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)
