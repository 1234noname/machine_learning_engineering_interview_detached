"""Fashion200k acquisition — pure functions and async fetch.

This module's surface:

- AcquisitionResult — enum with fetched / skipped_existing /
  failed outcomes.
- select_subset(seed, criteria, available_ids) — deterministic subset
  selection (the manifest's ID list).
- write_manifest(path, items, seed, criteria, dataset_version) — writes
  the self-describing JSON manifest used to reproduce a subset. Each
  item carries id, category, title, source_url and
  split; the manifest is thus enough on its own for a reproducer to
  fetch the bytes without going back to the upstream label files.
- acquire_image(item_id, source_url, backend) — idempotent async
  fetch via httpx; writes to the supplied StorageBackend.

No network calls happen at import time. The CLI entrypoint is
scripts/acquire-fashion200k.py.
"""

from __future__ import annotations

import enum
import json
import random
from pathlib import Path
from typing import Any

import httpx
from avsa_core.storage import StorageBackend

DEFAULT_SUBSET_COUNT = 15_000
_FETCH_TIMEOUT_S = 30.0


class AcquisitionResult(enum.Enum):
    """Three terminal outcomes of acquiring a single image."""

    fetched = "fetched"
    skipped_existing = "skipped_existing"
    failed = "failed"


def select_subset(
    seed: int,
    criteria: dict[str, Any],
    available_ids: list[str],
) -> list[str]:
    """Deterministically pick a subset of available_ids."""
    count = int(criteria.get("count", DEFAULT_SUBSET_COUNT))
    if count < 1:
        raise ValueError(f"select_subset: count must be >= 1; got {count}")
    if count > len(available_ids):
        return sorted(available_ids)
    rng = random.Random(seed)
    chosen = rng.sample(available_ids, k=count)
    return sorted(chosen)


def write_manifest(
    path: Path,
    items: list[dict[str, object]],
    seed: int,
    criteria: dict[str, object],
    dataset_version: str,
) -> None:
    """Write the self-describing subset manifest JSON to path.

    Schema (rev2):

        {seed, criteria, dataset_version, items: [{id, category, title,
        source_url, split}, ...]}

    Items are sorted at write time by id so two runs with the same
    inputs produce byte-identical files (important for git diffs and
    reproducibility checks). Every item must carry an id; missing
    that, the sort key is undefined and we raise ValueError rather
    than silently producing a corrupt manifest.

    UTF-8 is preserved literally (ensure_ascii=False) so titles
    containing non-ASCII characters round-trip exactly through the
    manifest.
    """
    for entry in items:
        if "id" not in entry:
            raise ValueError(
                "write_manifest: every item must contain an 'id' field; "
                f"got entry with keys {sorted(entry.keys())}"
            )
    sorted_items = sorted(items, key=lambda r: str(r["id"]))
    payload: dict[str, Any] = {
        "seed": seed,
        "criteria": criteria,
        "dataset_version": dataset_version,
        "items": sorted_items,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")


async def acquire_image(
    item_id: str,
    source_url: str,
    backend: StorageBackend,
) -> AcquisitionResult:
    """Idempotently fetch a single Fashion200k image into backend.

    Returns skipped_existing without making a network call when the
    backend already has an object under fashion200k/images/<item_id>.
    On a miss, issues a GET; writes the bytes verbatim on 2xx and
    returns fetched. Any non-2xx response yields failed.
    """
    existing = list(backend.list_objects(f"fashion200k/images/{item_id}"))
    if existing:
        return AcquisitionResult.skipped_existing

    target_path = f"fashion200k/images/{item_id}.jpg"
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
        try:
            resp = await client.get(source_url)
        except httpx.HTTPError:
            return AcquisitionResult.failed

    if not (200 <= resp.status_code < 300):
        return AcquisitionResult.failed

    backend.put_object(target_path, resp.content)
    return AcquisitionResult.fetched
