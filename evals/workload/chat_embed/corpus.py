"""Held-out Fashion200k query corpus.

The manifest ``evals/catalog/fashion200k/manifest.json`` contains 5,000 items
split into ``train`` (4,267) and ``test`` (733) subsets.  Only the
**``test``-split items** are used as the held-out query corpus: they are
**disjoint** from the seeded catalog (which uses the ``train`` split), so
querying with a test image against the catalog cannot trivially return that
same image.  This mirrors the held-out evaluation convention established by
``evals/retrieval/groundtruth.py`` (product-level kNN).

Per ADR-0007: only IDs and paths are committed here — image bytes live under
``data/fashion200k/images/`` which is gitignored.  The corpus helper resolves
the local disk path at runtime from the repo-root ``data/`` tree.

Image path convention (established by ``avsa_data.catalog_fashion200k``
and the acquisition script):

    data/fashion200k/images/{item_id}.jpg

where ``{item_id}`` is the manifest ``id`` field, e.g.::

    women/dresses/casual_and_day_dresses/87669967/87669967_4.jpeg

So the full local path is::

    data/fashion200k/images/women/dresses/casual_and_day_dresses/87669967/87669967_4.jpeg.jpg

(the double extension arises because the manifest id already ends in ``.jpeg``
and the acquisition script appends ``.jpg``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Resolved once at import time so callers need not re-derive it.
_REPO_ROOT: Path = next(
    p for p in Path(__file__).resolve().parents if (p / "config" / "avsa.toml").exists()
)
_MANIFEST_PATH: Path = (
    _REPO_ROOT / "evals" / "catalog" / "fashion200k" / "manifest.json"
)
_DATA_ROOT: Path = _REPO_ROOT / "data" / "fashion200k" / "images"


@dataclass(frozen=True)
class CorpusItem:
    """One held-out query item.

    Attributes
    ----------
    item_id:
        The manifest ``id`` field (e.g.
        ``women/dresses/casual_and_day_dresses/87669967/87669967_4.jpeg``).
    category:
        Fashion category label (``dress``, ``top``, ``jacket``, ``pants``,
        ``skirt``).
    title:
        Human-readable product title used as the text-query phrase (e.g.
        ``"white linen shirt dress"``).
    local_path:
        Absolute path to the JPEG on disk (resolved from ``data/``).
        The file exists only on machines where images have been acquired.
    """

    item_id: str
    category: str
    title: str
    local_path: Path


def load_test_corpus(
    manifest_path: Path | None = None,
    data_root: Path | None = None,
) -> list[CorpusItem]:
    """Return only the ``split=="test"`` items from the manifest.

    Parameters
    ----------
    manifest_path:
        Path to ``manifest.json``.  Defaults to the repo-canonical location
        ``evals/catalog/fashion200k/manifest.json``.
    data_root:
        Root directory under which image files are stored.  Defaults to
        ``data/fashion200k/images/`` relative to the repo root.  Image bytes
        must exist here for runtime tasks; the list is still valid without
        them for corpus-selection tests.

    Returns
    -------
    list[CorpusItem]
        One entry per ``split=="test"`` manifest item, in manifest order.
        Guaranteed to contain exactly the test-split items — no train items.
    """
    resolved_manifest = manifest_path or _MANIFEST_PATH
    resolved_data = data_root or _DATA_ROOT

    with resolved_manifest.open() as fh:
        raw: dict[str, object] = json.load(fh)

    items: list[dict[str, str]] = raw["items"]  # type: ignore[assignment]
    return [
        CorpusItem(
            item_id=item["id"],
            category=item["category"],
            title=item["title"],
            local_path=resolved_data / f"{item['id']}.jpg",
        )
        for item in items
        if item["split"] == "test"
    ]


# ---------------------------------------------------------------------------
# Corpus size constant — pinned to the manifest's test split (733 items).
# A test that imports this constant will fail if the manifest changes.
# ---------------------------------------------------------------------------
EXPECTED_CORPUS_SIZE: int = 733
