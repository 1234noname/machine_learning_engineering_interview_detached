#!/usr/bin/env python3
# AVSA — offline attribute-head training ( / ).
#
# Reproducible training step that fits linear `category` + `colour` probes on
# the FROZEN 768-d ViT-b-16 image features emitted by the #062 embedding
# artifact — no backbone is loaded or fine-tuned. It:
#
#   1. Loads the #062 embedding artifact via the sanctioned reader
#      `avsa_data.embedding_pipeline.load_embedding_artifact` (--artifact).
#   2. Derives per-image labels from the #061 manifest (--manifest):
#      `category` verbatim, `colour` via the catalog colour vocab
#      (`_colour_from_title`) — reused, never re-implemented, so the seeder's
#      and the probe's colour labels cannot drift.
#   3. Splits product-wise (`split_by_product`, seeded) so a product never
#      straddles train/test (no image-level leakage).
#   4. Fits both heads (ridge least-squares closed form — deterministic) and
#      evaluates held-out top-1 accuracy.
#   5. Writes a versioned head-weights artifact (+ label maps) under
#      `data/attribute_heads/<config-hash>/` (private, gitignored — derived from
#      non-redistributable Fashion200k embeddings; see STAKEHOLDERS.md).
#   6. Emits the accuracy report the committed `evals/attributes/story-020/
#      baseline.toml` gate floors are derived from.
#
# The artifact directory is named by a SHA-256 **config hash** over
# (image model version, source embedding-artifact hash, attributes, seed,
# test_frac, probe method) — so re-running the same training config against the
# same features lands in the same directory, and any input change forks a fresh
# one. The split is seeded and the ridge solve is closed-form, so re-runs
# reproduce identical accuracy / row counts / hashes.
#
# License: trains on the Fashion200k-derived #062 embeddings — research-use,
# non-redistributable image rights (Lyst ToS); see STAKEHOLDERS.md and
# ADR-0007. Head weights are private derived data and never committed; only the
# accuracy metrics (baseline.toml) enter git.
#
# Pre-requisites:
#   - The #062 embedding artifact exists (build via
#     scripts/precompute-embeddings.py; see scripts/README-precompute-embeddings.md).
#     Pass its directory as --artifact (gitignored, under data/embeddings/<hash>/).
#   - The #061 subset manifest exists at --manifest (carries category + title
#     per image id).

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

import numpy as np  # noqa: E402
from avsa_core.storage.local import LocalStorageBackend  # noqa: E402
from avsa_data.attribute_heads import (  # noqa: E402
    IMAGE_DIM,
    AttributeHeadError,
    LinearHead,
    compute_head_config_hash,
    evaluate,
    extract_labels,
    split_by_product,
    train_linear_probe,
    write_head_artifact,
)
from avsa_data.embedding_pipeline import load_embedding_artifact  # noqa: E402

DEFAULT_MANIFEST = REPO_ROOT / "evals" / "catalog" / "fashion200k" / "manifest.json"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"

# The attributes trained, in a fixed order — flows into the config hash.
_ATTRIBUTES = ["category", "colour"]

# Image-backbone version pinned into the head config hash. The #062 manifest
# records "vit-b-16"; the head config hash is what names the artifact directory,
# so this string is a rebuild signal exactly like precompute-embeddings.py's
# model-version sentinels.
_MODEL_VERSION_IMAGE = "vit-b-16"

# Probe method recorded in the config hash + report. The library fits a ridge
# least-squares linear probe (closed-form normal equations); naming it here
# keeps the artifact self-describing.
_PROBE = "ridge"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact",
        type=Path,
        required=True,
        help=(
            "Directory of the #062 embedding artifact "
            "(data/embeddings/<content_hash>/, gitignored). Read via the "
            "sanctioned load_embedding_artifact reader."
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="#061 subset manifest JSON with category + title (default: %(default)s).",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=(
            "Backend root receiving attribute_heads/<config_hash>/ "
            "(default: %(default)s; gitignored)."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=17,
        help="Seed for the product-level split (default: %(default)s).",
    )
    parser.add_argument(
        "--test-frac",
        type=float,
        default=0.2,
        help="Held-out fraction (by product) for the report (default: %(default)s).",
    )
    return parser.parse_args()


def _resolve_artifact_dir(artifact: Path, data_root: Path) -> tuple[Path, Path]:
    """Split --artifact into (backend_root, key relative to that root).

    ``load_embedding_artifact`` reads through a ``StorageBackend`` rooted at
    some directory and keyed by a relative path. The real artifact lives at
    ``<data_root>/embeddings/<hash>/``; when --artifact sits under --data-root
    we root the read backend at --data-root and key by the relative remainder.
    Otherwise we root the read backend at the artifact's parent and key by its
    final component — so an artifact moved outside data/ still loads. This read
    root is used ONLY for reading the embeddings; the head artifact is written
    to the canonical --data-root regardless (see ``_run``).
    """
    artifact = artifact.resolve()
    data_root = data_root.resolve()
    try:
        relative = artifact.relative_to(data_root)
        return data_root, relative
    except ValueError:
        return artifact.parent, Path(artifact.name)


def _features_and_ids(
    embeddings: list[dict[str, object]],
) -> tuple[np.ndarray, list[str]]:
    """Stack the frozen 768-d image features + their ids, in artifact order.

    The #062 rows carry ``image_embedding`` (the frozen ViT feature) keyed by
    ``id``. We narrow each to a float vector and assert the 768-d contract so a
    malformed artifact fails fast at the boundary rather than skewing the fit.
    """
    ids: list[str] = []
    feats: list[list[float]] = []
    for row in embeddings:
        vec = row["image_embedding"]
        if not isinstance(vec, list) or len(vec) != IMAGE_DIM:
            got = len(vec) if isinstance(vec, list) else type(vec).__name__
            raise AttributeHeadError(
                f"embedding row id={row.get('id')!r} has a bad image_embedding "
                f"shape: expected a length-{IMAGE_DIM} list, got {got!r}"
            )
        ids.append(str(row["id"]))
        feats.append([float(x) for x in vec])
    return np.asarray(feats, dtype=np.float64), ids


def _select(
    matrix: np.ndarray,
    ids: list[str],
    keep: list[str],
    labels: dict[str, dict[str, str]],
    attribute: str,
) -> tuple[np.ndarray, list[str]]:
    """Slice (features, attribute-labels) to the rows whose id is in ``keep``.

    ``keep`` is a split's id list (train or test); ``labels`` maps id →
    per-attribute label. Preserves ``keep`` order. An id with no label entry is
    a manifest/artifact mismatch and fails fast.
    """
    id_to_row = {image_id: idx for idx, image_id in enumerate(ids)}
    rows: list[int] = []
    attr_labels: list[str] = []
    for image_id in keep:
        if image_id not in labels:
            raise AttributeHeadError(
                f"image id {image_id!r} is in the embedding artifact but has no "
                f"label in the manifest — manifest/artifact mismatch"
            )
        rows.append(id_to_row[image_id])
        attr_labels.append(labels[image_id][attribute])
    return matrix[rows], attr_labels


def _format_report(
    *,
    config_hash: str,
    embedding_hash: str,
    seed: int,
    test_frac: float,
    train_rows: int,
    test_rows: int,
    observed: dict[str, float],
    n_classes: dict[str, int],
    chance: dict[str, float],
    slack: float,
) -> str:
    """Render the baseline.toml body from the CLI's observed numbers.

    Gate floors = observed - slack, clamped >= 0 (category is the hard gate;
    colour is reported-only because its labels are description-derived). The
    committed baseline is regenerated from THIS output so it is reproducible +
    falsifiable.
    """
    cat = observed["category"]
    col = observed["colour"]
    cat_floor = max(0.0, round(cat - slack, 4))
    col_floor = max(0.0, round(col - slack, 4))
    cat_n, col_n = n_classes["category"], n_classes["colour"]
    cat_chance, col_chance = chance["category"], chance["colour"]
    caveat = (
        "Colour labels are description-derived (first colour word in the title "
        "via the catalog_fashion200k keyword vocab, with a 'multicolour' "
        "fallback when no colour word is present) — they are NOT ground truth. "
        "Colour top-1 is reported for visibility and is expected to be noisier "
        "and lower than category; it is not a hard quality gate."
    )
    # Assembled line-by-line (rather than one triple-quoted block) so each long
    # provenance/caveat value is an individually lintable string literal; the
    # joined output is plain TOML, parsed by test_accuracy_report_committed_shape.
    lines = [
        "# — ViT attribute-head held-out accuracy baseline",
        "#",
        "# CALIBRATED against the trained head artifact, regenerated by",
        "# scripts/train-attribute-heads.py (see scripts/README-train-attribute-heads.md).",
        "# The values below are the committed *regression-gate floors* (gate: measured",
        f"# accuracy >= floor). Each floor = observed held-out top-1 minus a {slack}",
        "# slack (clamped >= 0), so the gate passes today against the real heads and",
        "# trips only on a meaningful regression.",
        "#",
        "# Trained against the #062 embedding artifact",
        f"#   content_hash = {embedding_hash}",
        "# (frozen 768-d ViT-b-16 image embeddings). Head-weights artifact (private,",
        "# gitignored under data/) at:",
        f"#   data/attribute_heads/{config_hash}/",
        "#",
        "# Probe: ridge least-squares linear probe (numpy normal eqns, lambda=1.0),",
        "# one weight matrix + bias per attribute, argmax at inference. No backbone",
        "# loaded or fine-tuned.",
        "#",
        "# The split is product-level (whole products to train/test, seeded) to avoid",
        "# image-level leakage; the source features are the frozen 768-d ViT image",
        f"# embeddings from the #062 artifact. Train rows = {train_rows}, "
        f"held-out test rows = {test_rows}.",
        "",
        "recorded = true",
        "",
        f"# Held-out top-1 GATE FLOORS (observed - {slack} slack, clamped >= 0).",
        "# Category labels are clean (verbatim from the manifest); the category",
        "# baseline is the committed quality gate for .",
        f"#   category: observed held-out top-1 = {cat:.4f} ({cat_n} classes; "
        f"chance {cat_chance:.4f}) -> floor {cat_floor:.4f}.",
        f"category_top1 = {cat_floor}",
        "",
        "# Colour top-1 — reported, NOT a hard gate (colour labels are noisier).",
        f"#   colour: observed held-out top-1 = {col:.4f} ({col_n} classes; "
        f"chance {col_chance:.4f}) -> floor {col_floor:.4f}.",
        f"colour_top1 = {col_floor}",
        "",
        "# REQUIRED caveat: colour is description-derived (first colour word in the",
        "# title, multicolour fallback) — not ground truth — so colour accuracy is",
        "# expected to trail category and must be read with this noise in mind.",
        f'colour_caveat = "{caveat}"',
        "",
        "[observed]",
        "# Raw measured held-out top-1 accuracies the floors above derive from.",
        f"category_top1 = {round(cat, 4)}",
        f"colour_top1 = {round(col, 4)}",
        f"slack = {slack}",
        f"category_n_classes = {cat_n}",
        f"colour_n_classes = {col_n}",
        f"category_chance = {round(cat_chance, 4)}",
        f"colour_chance = {round(col_chance, 4)}",
        "",
        "[split]",
        f"seed = {seed}",
        f"test_frac = {test_frac}",
        'strategy = "by-product"  # whole products (numeric-ID directory) to one split',
        f"train_rows = {train_rows}",
        f"test_rows = {test_rows}",
        "",
        "[provenance]",
        'feature_source = "frozen ViT-b-16 image embeddings (768-d) from the '
        '#062 embedding artifact"',
        f'embedding_artifact_content_hash = "{embedding_hash}"',
        f'head_artifact_content_hash = "{config_hash}"',
        'head_artifact_location = "data/attribute_heads/<head_artifact_content_hash>/ '
        '(private, gitignored — derived from non-redistributable Fashion200k embeddings)"',
        'backbone = "frozen — not loaded or modified by #067 (linear probe only)"',
        'probe = "ridge least-squares (numpy normal equations, lambda=1.0)"',
    ]
    return "\n".join(lines) + "\n"


def _run(args: argparse.Namespace) -> int:
    if not args.manifest.exists():
        print(
            f"ERROR: subset manifest not found at {args.manifest}. Build it via "
            "scripts/acquire-fashion200k.py; see its README.",
            file=sys.stderr,
        )
        return 2

    read_root, artifact_key = _resolve_artifact_dir(args.artifact, args.data_root)
    read_backend = LocalStorageBackend(root=read_root)
    embeddings, embed_manifest = load_embedding_artifact(artifact_key, read_backend)
    embedding_hash = embed_manifest["content_hash"]

    # The head artifact is written to the CANONICAL data root (--data-root,
    # default ./data) under attribute_heads/<config-hash>/, INDEPENDENT of where
    # --artifact points. Previously the write backend was rooted at the artifact
    # input path, so an --artifact outside data/ (e.g. data/embeddings/<hash>/
    # read with a backend rooted at data/embeddings) leaked the heads under
    # data/embeddings/attribute_heads/<hash>/. Decoupling the write root keeps
    # the head-artifact location canonical and config-driven (:
    # [model] attribute_heads_dir points at data/attribute_heads/<hash>/).
    write_backend = LocalStorageBackend(root=args.data_root.resolve())

    features, ids = _features_and_ids(embeddings)
    labels = extract_labels(args.manifest)

    train_ids, test_ids = split_by_product(
        ids, seed=args.seed, test_frac=args.test_frac
    )

    # The head config hash names the artifact directory; same construction as
    # the #062 content hash, so the artifact is self-identifying.
    config = {
        "model_version_image": _MODEL_VERSION_IMAGE,
        "embedding_artifact_hash": embedding_hash,
        "attributes": _ATTRIBUTES,
        "seed": args.seed,
        "test_frac": args.test_frac,
        "probe": _PROBE,
    }
    config_hash = compute_head_config_hash(config)

    heads: dict[str, LinearHead] = {}
    observed: dict[str, float] = {}
    n_classes: dict[str, int] = {}
    chance: dict[str, float] = {}
    for attribute in _ATTRIBUTES:
        train_feats, train_labels = _select(features, ids, train_ids, labels, attribute)
        test_feats, test_labels = _select(features, ids, test_ids, labels, attribute)
        head = train_linear_probe(train_feats, train_labels)
        heads[attribute] = head
        observed[attribute] = evaluate(head, test_feats, test_labels)
        n_classes[attribute] = len(head.label_map)
        chance[attribute] = 1.0 / len(head.label_map)

    out_dir = Path("attribute_heads") / config_hash
    write_head_artifact(
        out_dir=out_dir,
        heads=heads,
        manifest={
            "model_version_image": _MODEL_VERSION_IMAGE,
            "image_dim": IMAGE_DIM,
            "class_counts": {a: n_classes[a] for a in _ATTRIBUTES},
            "content_hash": config_hash,
            "generated_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        },
        backend=write_backend,
    )

    report = _format_report(
        config_hash=config_hash,
        embedding_hash=embedding_hash,
        seed=args.seed,
        test_frac=args.test_frac,
        train_rows=len(train_ids),
        test_rows=len(test_ids),
        observed=observed,
        n_classes=n_classes,
        chance=chance,
        slack=0.05,
    )

    print(
        f"==> attribute heads written: attributes={_ATTRIBUTES} "
        f"train_rows={len(train_ids)} test_rows={len(test_ids)} "
        f"category_top1={observed['category']:.4f} "
        f"colour_top1={observed['colour']:.4f} "
        f"config_hash={config_hash} "
        f"path={args.data_root.resolve() / out_dir}",
        flush=True,
    )
    print(
        "==> accuracy report (paste into evals/attributes/story-020/baseline.toml):",
        flush=True,
    )
    print(report, flush=True)
    return 0


def main() -> int:
    return _run(_parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
