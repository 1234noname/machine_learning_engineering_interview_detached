#!/usr/bin/env python3
# AVSA — offline embedding pre-compute pipeline.
#
# Re-runnable CLI that:
#   1. Loads the Fashion200k subset manifest (--subset-manifest).
#   2. Reads each item's image bytes from the LocalStorageBackend
#      (path: fashion200k/images/<id>.jpg per Phase 1's on-disk reality;
#      because <id> already ends ".jpeg", the full filename has the
#      Phase 1 double-extension ".jpeg.jpg").
#   3. Computes 768-d image embeddings + 512-d text embeddings via the
#      model service (POST /embed + POST /embed_text, batched).
#   4. Writes a self-identifying artifact under
#      <data_root>/embeddings/<content_hash>/ — embeddings.jsonl +
#      manifest.json.
#
# The content_hash is a SHA-256 over (model versions, subset count,
# dataset version, batch size) — so re-running the same subset against
# the same models produces the same artifact path. Different inputs →
# different hash → different directory, with no manual versioning.
#
# Pre-requisites:
#   - The subset manifest exists (build via scripts/acquire-fashion200k.py;
#     see scripts/README-acquire-fashion200k.md).
#   - The model service is running at --model-url (typically
#     http://localhost:8000 with AVSA_MODEL_STUB=0 for real embeddings).
#   - Image bytes are present under <data_root>/fashion200k/images/.

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import math
import sys
import tomllib
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]

from avsa_core.storage import NotFound  # noqa: E402
from avsa_core.storage.local import LocalStorageBackend  # noqa: E402
from avsa_data.embedding_pipeline import (  # noqa: E402
    _as_bytes,
    _text_for,
    compute_content_hash,
    compute_embeddings,
    write_embedding_artifact,
)

DEFAULT_SUBSET_MANIFEST = (
    REPO_ROOT / "evals" / "catalog" / "fashion200k" / "manifest.json"
)
DEFAULT_DATA_ROOT = REPO_ROOT / "data"
DEFAULT_MODEL_URL = "http://localhost:8000"
DEFAULT_CONCURRENCY = 4
DEFAULT_BATCH_SIZE = 16
DEFAULT_VERIFY_SAMPLE_SIZE = 5
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "avsa.toml"

# Hardcoded model-version sentinels — the model service exposes neither
# a /version nor a /healthz that reports model identity today. When that
# endpoint lands (tracked as a follow-up under ), swap these
# constants for a GET {model_url}/version call in _resolve_model_versions
# below. The strings flow into the content_hash, so changing them is a
# rebuild signal — exactly what the cache layer wants.
_MODEL_VERSION_IMAGE = "vit-b-16"
_MODEL_VERSION_TEXT = "text-encoder-v1"

# Dataset-version sentinel for items whose subset manifest is missing
# the dataset_version field (older manifests). Matches the rev2
# default emitted by scripts/acquire-fashion200k.py today.
_DEFAULT_DATASET_VERSION = "fashion200k-v1.0"

# httpx timeout for /embed + /embed_text calls. ViT-b-16 on CPU is
# ~250ms/image; a 16-item batch under load can take ~5s. 60s gives
# generous headroom without leaving the connection pool open on a
# wedged server.
_MODEL_TIMEOUT_S = 60.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subset-manifest",
        type=Path,
        default=DEFAULT_SUBSET_MANIFEST,
        help="Subset manifest JSON (default: %(default)s).",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=(
            "Backend root holding fashion200k/images/<id>.jpg and "
            "receiving embeddings/<hash>/ (default: %(default)s; gitignored)."
        ),
    )
    parser.add_argument(
        "--model-url",
        type=str,
        default=DEFAULT_MODEL_URL,
        help="Base URL for the avsa-model service (default: %(default)s).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Reserved for future batch-level parallelism (default: %(default)s).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Items per /embed + /embed_text request (default: %(default)s).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "After writing, re-embed a sample of rows through the LIVE model "
            "service and assert cosine similarity to the stored vectors stays "
            "at/above [evals.embedding] equivalence_min_cosine (config-driven). "
            "Exits non-zero if any sample falls below the floor."
        ),
    )
    parser.add_argument(
        "--verify-sample-size",
        type=int,
        default=DEFAULT_VERIFY_SAMPLE_SIZE,
        help=(
            "Number of rows to re-embed for the --verify equivalence check "
            "(default: %(default)s; capped at the artifact's item count)."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=(
            "Path to config/avsa.toml for the --verify threshold "
            "(default: %(default)s)."
        ),
    )
    return parser.parse_args()


def _load_subset_manifest(path: Path) -> dict[str, object]:
    """Read the subset manifest JSON into a dict."""
    import json

    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _image_path_for(item_id: str) -> str:
    """Storage path for an item's raw image bytes.

    Phase 1 lands bytes under ``fashion200k/images/<id>.jpg``. The
    Fashion200k IDs already end ``.jpeg`` upstream, so the on-disk
    filename has the double-extension ``.jpeg.jpg`` — see
    ``scripts/acquire-fashion200k.py``'s ``acquire_image`` for the
    landing path.
    """
    return f"fashion200k/images/{item_id}.jpg"


def _load_equivalence_min_cosine(config_path: Path) -> float:
    """Read [evals.embedding] equivalence_min_cosine from config/avsa.toml.

    No hardcoded threshold: the floor lives in config so a change is a
    config edit, not a code edit. Falls back to a strict default only if
    the key is genuinely absent (older config), and that default is itself
    near-1.0 so a missing key can never silently weaken the gate.
    """
    with config_path.open("rb") as f:
        raw = tomllib.load(f)
    section = raw.get("evals", {}).get("embedding", {})
    value = section.get("equivalence_min_cosine", 0.9999)
    return float(value)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length float vectors.

    Pure Python (``dot / (||a|| * ||b||)``) — no numpy dependency in
    apps/api. A zero-norm vector yields 0.0 (treated as maximally
    dissimilar) rather than raising, so the verify gate fails loudly on a
    degenerate vector instead of crashing.
    """
    if len(a) != len(b):
        raise ValueError(f"_cosine: vector length mismatch {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _as_float_vector(value: object) -> list[float]:
    """Narrow a JSON-loaded embedding value to ``list[float]`` for cosine.

    The artifact rows are typed ``object`` (json round-trip), so the
    embedding lists need explicit narrowing before the float math. Raises
    on a non-list so a malformed row fails loudly rather than silently
    skewing the equivalence check.
    """
    if not isinstance(value, list):
        raise TypeError(f"embedding must be a list; got {type(value).__name__}")
    return [float(x) for x in value]


async def _verify_equivalence(
    *,
    embeddings: list[dict[str, object]],
    items_by_id: dict[str, dict[str, object]],
    model_url: str,
    client: httpx.AsyncClient,
    sample_size: int,
    min_cosine: float,
) -> int:
    """Re-embed a sample of written rows live and gate on cosine similarity.

    For each sampled row, posts the item's image bytes to ``/embed`` and its
    text to ``/embed_text``, then compares the fresh vectors against the
    vectors stored in the artifact (``embeddings``). Returns 0 if every
    sample (both modalities) meets ``min_cosine``; otherwise prints the
    offending sample/modality/cosine and returns 1.
    """
    sample = embeddings[: max(0, sample_size)]
    if not sample:
        print("==> verify: nothing to check (empty artifact).", flush=True)
        return 0

    min_observed = 1.0
    failures: list[str] = []
    for row in sample:
        item_id = str(row["id"])
        source = items_by_id.get(item_id)
        if source is None:
            failures.append(
                f"sample id={item_id}: no source item found to re-embed (modality=both)"
            )
            continue

        image_b64 = base64.b64encode(_as_bytes(source["image_bytes"])).decode("ascii")
        image_resp = await client.post(
            f"{model_url}/embed", json={"images": [image_b64]}
        )
        image_resp.raise_for_status()
        fresh_image = _as_float_vector(image_resp.json()["embeddings"][0])

        text_resp = await client.post(
            f"{model_url}/embed_text", json={"texts": [_text_for(source)]}
        )
        text_resp.raise_for_status()
        fresh_text = _as_float_vector(text_resp.json()["embeddings"][0])

        stored_image = _as_float_vector(row["image_embedding"])
        stored_text = _as_float_vector(row["text_embedding"])

        cos_image = _cosine(fresh_image, stored_image)
        cos_text = _cosine(fresh_text, stored_text)
        min_observed = min(min_observed, cos_image, cos_text)

        if cos_image < min_cosine:
            failures.append(
                f"sample id={item_id} modality=image cosine={cos_image:.6f} "
                f"< min={min_cosine}"
            )
        if cos_text < min_cosine:
            failures.append(
                f"sample id={item_id} modality=text cosine={cos_text:.6f} "
                f"< min={min_cosine}"
            )

    if failures:
        print(
            f"ERROR: verify FAILED — {len(failures)} sample/modality check(s) "
            f"below min cosine {min_cosine}:",
            file=sys.stderr,
        )
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print(
        f"==> verify OK: {len(sample)} sample(s) x 2 modalities all >= "
        f"min cosine {min_cosine} (min observed cosine={min_observed:.6f}).",
        flush=True,
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    if not args.subset_manifest.exists():
        print(
            f"ERROR: subset manifest not found at {args.subset_manifest}. "
            "Build it via scripts/acquire-fashion200k.py; see "
            "scripts/README-acquire-fashion200k.md.",
            file=sys.stderr,
        )
        return 2

    subset = _load_subset_manifest(args.subset_manifest)
    items_meta = subset.get("items", [])
    if not isinstance(items_meta, list) or not items_meta:
        print(
            f"ERROR: subset manifest {args.subset_manifest} contains no items.",
            file=sys.stderr,
        )
        return 2
    dataset_version = str(subset.get("dataset_version", _DEFAULT_DATASET_VERSION))

    backend = LocalStorageBackend(root=args.data_root)

    # Load image bytes inline so compute_embeddings can stay pure
    # (no storage dependency) and respx-mockable.
    items: list[dict[str, object]] = []
    missing: list[str] = []
    for entry in items_meta:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry["id"])
        try:
            image_bytes = backend.get_object(_image_path_for(item_id))
        except NotFound:
            missing.append(item_id)
            continue
        items.append(
            {
                "id": item_id,
                "title": str(entry.get("title", "")),
                "description": str(entry.get("description", entry.get("title", ""))),
                "image_bytes": image_bytes,
            }
        )

    if missing:
        print(
            f"WARNING: skipped {len(missing)} item(s) with no image bytes under "
            f"{args.data_root}/fashion200k/images/ — re-run "
            "scripts/acquire-fashion200k.py to backfill.",
            file=sys.stderr,
        )
    if not items:
        print(
            "ERROR: no items had image bytes available; nothing to embed.",
            file=sys.stderr,
        )
        return 2

    content_hash = compute_content_hash(
        {
            "model_version_image": _MODEL_VERSION_IMAGE,
            "model_version_text": _MODEL_VERSION_TEXT,
            "subset_count": len(items),
            "dataset_version": dataset_version,
            "batch_size": args.batch_size,
        }
    )

    # Single AsyncClient — re-uses the connection pool across every
    # batch, the natural shape for this new code (Finding 6 fold-in for
    # apps/api/src/avsa_api/acquisition.py is out of scope per the
    # 2A-iii brief).
    async with httpx.AsyncClient(timeout=_MODEL_TIMEOUT_S) as client:
        embeddings = await compute_embeddings(
            items=items,
            model_url=args.model_url,
            batch_size=args.batch_size,
            client=client,
        )

    image_dim = len(embeddings[0]["image_embedding"]) if embeddings else 0  # type: ignore[arg-type]
    # The TypedDict guarantees a list at the value; len() is well-defined.
    text_dim = len(embeddings[0]["text_embedding"]) if embeddings else 0  # type: ignore[arg-type]

    manifest = {
        "model_version_image": _MODEL_VERSION_IMAGE,
        "model_version_text": _MODEL_VERSION_TEXT,
        "image_dim": image_dim,
        "text_dim": text_dim,
        "item_count": len(embeddings),
        "content_hash": content_hash,
        "generated_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
    }

    out_dir = Path("embeddings") / content_hash
    write_embedding_artifact(
        out_dir=out_dir,
        embeddings=embeddings,
        manifest=manifest,  # type: ignore[arg-type]
        # The dict above matches EmbeddingArtifactManifest's keys; mypy
        # can't see TypedDict structural conformance from a literal here.
        backend=backend,
    )

    print(
        f"==> embeddings written: item_count={len(embeddings)} "
        f"image_dim={image_dim} text_dim={text_dim} "
        f"content_hash={content_hash} "
        f"path={args.data_root / out_dir}",
        flush=True,
    )

    if args.verify:
        min_cosine = _load_equivalence_min_cosine(args.config)
        items_by_id = {str(item["id"]): item for item in items}
        async with httpx.AsyncClient(timeout=_MODEL_TIMEOUT_S) as client:
            return await _verify_equivalence(
                embeddings=embeddings,
                items_by_id=items_by_id,
                model_url=args.model_url,
                client=client,
                sample_size=args.verify_sample_size,
                min_cosine=min_cosine,
            )

    return 0


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
