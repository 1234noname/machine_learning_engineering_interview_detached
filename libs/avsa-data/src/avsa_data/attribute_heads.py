"""Linear attribute-probe heads over frozen ViT features ( / ).

A *linear probe* fits a single linear layer on top of the frozen 768-d ViT-b-16
image features emitted by the embedding artifact — no backbone is loaded or
fine-tuned here. We fit one head per attribute (``category``, ``colour``):

- **Probe method — ridge least-squares.** Each class is one-hot encoded into a
  target matrix ``Y`` (shape ``(N, n_classes)``) and we solve the L2-regularised
  normal equations ``(XᵀX + λI) W = XᵀY`` for the weight matrix ``W`` (shape
  ``(768, n_classes)``) plus a bias term. Closed-form, deterministic, CPU-cheap,
  and numpy-only — no torch, no iterative optimiser, no random init. Inference is
  ``argmax(X @ W + b)``. The L2 term keeps the normal equations well-conditioned
  on a wide (768-d) feature space and is the standard guard against an
  ill-posed / overfit least-squares fit.

- **Labels.** ``category`` is taken verbatim from the manifest; ``colour``
  is derived from the title via the *existing* catalog colour vocabulary
  (``catalog_fashion200k._colour_from_title`` — reused, not re-implemented, so
  the two label derivations cannot drift). Colour is description-derived and
  therefore noisier than category — the committed accuracy report carries that
  caveat.

- **Split-by-product.** The leakage boundary is the numeric-ID directory in each
  image id (``women/dresses/.../56037632/56037632_0.jpeg`` → product
  ``56037632``): every image of one product lands wholly in train or wholly in
  test, so a probe cannot memorise a product in train and be scored on a sibling
  image in test. The split is seeded for reproducibility.

- **Artifact.** ``write_head_artifact`` persists the per-attribute weights +
  label maps + a manifest through the sanctioned ``StorageBackend``, under
  ``data/`` (gitignored). Head weights are derived from the non-redistributable
  Fashion200k embeddings, so they are private derived data — never committed.
  The accuracy *metrics* (``evals/attributes/baseline/baseline.toml``) ARE
  committed.

- **Reproducibility.** ``compute_head_config_hash`` is a deterministic,
  key-order-independent SHA-256 of the training config (source artifact hash,
  attributes, seed, test_frac, probe method) so an artifact directory is
  self-identifying — same posture as ``embedding_pipeline.compute_content_hash``.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from avsa_data.catalog_fashion200k import _colour_from_title

if TYPE_CHECKING:
    from pathlib import Path

    from avsa_core.storage import StorageBackend
    from numpy.typing import NDArray


# L2 ridge penalty for the normal equations. A small positive value
# regularises the wide (768-d) least-squares fit so ``XᵀX + λI`` is
# well-conditioned and the probe generalises rather than memorising. Not a
# user-facing quality threshold (those live in baseline.toml / config), so it
# stays a module constant rather than a config key — it is an internal
# numerical-stability knob of the solver, per code-quality "pull complexity
# downward".
_RIDGE_LAMBDA = 1.0

# Feature dimension of the frozen ViT-b-16 image embedding.
IMAGE_DIM = 768


class AttributeHeadError(Exception):
    """Raised when probe training / label derivation hits an unrecoverable state.

    A domain error (per the operating contract: errors as domain types) so
    callers see a typed failure at the boundary rather than a bare numpy or
    KeyError leaking through.
    """


@dataclass(frozen=True)
class LinearHead:
    """A frozen linear probe: ``argmax(features @ weights + bias)`` → class name.

    - ``weights`` — ``(768, n_classes)`` float matrix (the only learned params
      besides bias; no backbone).
    - ``bias`` — length-``n_classes`` float vector.
    - ``label_map`` — ``{class_index: class_name}``; the column order of
      ``weights`` is the class-index order, so ``label_map[argmax(...)]`` is the
      predicted class name.
    """

    weights: NDArray[np.float64]
    bias: NDArray[np.float64]
    label_map: dict[int, str]


def split_by_product(
    ids: list[str],
    *,
    seed: int,
    test_frac: float,
) -> tuple[list[str], list[str]]:
    """Partition image ``ids`` into (train, test) at the *product* boundary.

    The product key is the numeric-ID directory component of an id
    (``women/dresses/.../56037632/56037632_0.jpeg`` → ``56037632``). All images
    of one product land in the same split, so a product never straddles
    train/test (no leakage). Products are shuffled with a seeded RNG and the
    first ``test_frac`` fraction go to test; the partition is therefore
    deterministic for a fixed ``(ids, seed, test_frac)``.

    Returns ``(train_ids, test_ids)`` preserving the input order within each
    split.
    """
    if not 0.0 <= test_frac <= 1.0:
        raise AttributeHeadError(f"split_by_product: test_frac must be in [0, 1]; got {test_frac}")

    products = sorted({_product_of(image_id) for image_id in ids})
    rng = np.random.default_rng(seed)
    rng.shuffle(products)
    n_test = round(len(products) * test_frac)
    test_products = set(products[:n_test])

    train_ids = [i for i in ids if _product_of(i) not in test_products]
    test_ids = [i for i in ids if _product_of(i) in test_products]
    return train_ids, test_ids


def extract_labels(manifest_path: Path) -> dict[str, dict[str, str]]:
    """Derive per-image attribute labels from the self-describing manifest.

    Returns ``{image_id: {"category": <verbatim>, "colour": <derived>}}``:

    - ``category`` — taken verbatim from the manifest entry.
    - ``colour`` — derived from the title via the catalog colour vocabulary
      (``_colour_from_title``: first vocab word in the title, ``multicolour``
      fallback). Reused from ``catalog_fashion200k`` so the seeder's and the
      probe's colour labels cannot drift.
    """
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest.get("items")
    if not isinstance(items, list):
        raise AttributeHeadError(
            f"extract_labels: manifest at {manifest_path} has no 'items' list; "
            f"got top-level keys {sorted(manifest)!r}"
        )

    labels: dict[str, dict[str, str]] = {}
    for entry in items:
        image_id = str(entry["id"])
        title = str(entry["title"])
        labels[image_id] = {
            "category": str(entry["category"]),
            "colour": _colour_from_title(title),
        }
    return labels


def train_linear_probe(
    features: NDArray[np.float64],
    string_labels: list[str],
) -> LinearHead:
    """Fit a ridge least-squares linear probe mapping features → class scores.

    ``features`` is ``(N, 768)``; ``string_labels`` is the length-``N`` list of
    per-row class names. Classes are indexed in sorted order (deterministic),
    one-hot encoded into ``Y`` (shape ``(N, n_classes)``), and we solve the
    L2-regularised normal equations ``(XᵀX + λI) W = XᵀY`` in closed form. A
    bias term is fit by augmenting ``X`` with a constant column.

    Returns a :class:`LinearHead` whose ``weights`` are ``(768, n_classes)`` and
    ``label_map`` is ``{class_index: class_name}``.
    """
    feats = np.asarray(features, dtype=np.float64)
    if feats.ndim != 2 or feats.shape[1] != IMAGE_DIM:
        raise AttributeHeadError(
            f"train_linear_probe: features must be (N, {IMAGE_DIM}); got {feats.shape}"
        )
    if len(string_labels) != feats.shape[0]:
        raise AttributeHeadError(
            "train_linear_probe: label count must match feature rows; "
            f"got {len(string_labels)} labels for {feats.shape[0]} rows"
        )
    if feats.shape[0] == 0:
        raise AttributeHeadError("train_linear_probe: cannot train on zero rows")

    classes = sorted(set(string_labels))
    class_index = {name: idx for idx, name in enumerate(classes)}
    n_classes = len(classes)

    # One-hot targets.
    targets = np.zeros((feats.shape[0], n_classes), dtype=np.float64)
    for row, name in enumerate(string_labels):
        targets[row, class_index[name]] = 1.0

    # Augment with a constant column so the bias is fit jointly with the
    # weights, then solve the ridge normal equations. The penalty is NOT
    # applied to the bias column (its diagonal entry is left at 0) so a class
    # prior is not shrunk toward zero.
    augmented = np.hstack([feats, np.ones((feats.shape[0], 1), dtype=np.float64)])
    gram = augmented.T @ augmented
    penalty = _RIDGE_LAMBDA * np.eye(IMAGE_DIM + 1, dtype=np.float64)
    penalty[IMAGE_DIM, IMAGE_DIM] = 0.0
    solution = np.linalg.solve(gram + penalty, augmented.T @ targets)

    weights = solution[:IMAGE_DIM, :].astype(np.float64)
    bias = solution[IMAGE_DIM, :].astype(np.float64)
    label_map = {idx: name for name, idx in class_index.items()}
    return LinearHead(weights=weights, bias=bias, label_map=label_map)


def evaluate(
    head: LinearHead,
    features: NDArray[np.float64],
    string_labels: list[str],
) -> float:
    """Return top-1 accuracy of ``head`` on ``features`` vs ``string_labels``.

    Computes ``argmax(features @ weights + bias)`` per row, maps the winning
    column index to a class name via ``head.label_map``, and reports the
    fraction that match the truth label.
    """
    feats = np.asarray(features, dtype=np.float64)
    if len(string_labels) != feats.shape[0]:
        raise AttributeHeadError(
            "evaluate: label count must match feature rows; "
            f"got {len(string_labels)} labels for {feats.shape[0]} rows"
        )
    if feats.shape[0] == 0:
        raise AttributeHeadError("evaluate: cannot evaluate on zero rows")

    scores = feats @ head.weights + head.bias
    predicted_idx = np.argmax(scores, axis=1)
    predicted = [head.label_map[int(idx)] for idx in predicted_idx]
    correct = sum(1 for pred, truth in zip(predicted, string_labels, strict=True) if pred == truth)
    return correct / int(feats.shape[0])


def write_head_artifact(
    *,
    out_dir: Path,
    heads: dict[str, LinearHead],
    manifest: dict[str, object],
    backend: StorageBackend,
) -> None:
    """Persist per-attribute head weights + label maps + a manifest via ``backend``.

    Layout under ``out_dir`` (which lives under ``data/`` — gitignored private
    derived data, same posture as the embedding artifact):

    - ``<attribute>.npz`` — the head's weight matrix + bias, serialized with
      ``numpy.savez`` (a zip archive of named arrays, round-trippable via
      ``numpy.load`` — the model service's ``heads._load_head`` reads it back).
    - ``<attribute>.labels.json`` — the ``{class_index: class_name}`` label map.
    - ``manifest.json`` — the supplied manifest (model version, image_dim,
      per-attribute class counts, content_hash, generated_at).

    Bytes go through ``StorageBackend.put_object`` so the same code path works
    against local disk today and a future object store. ``out_dir`` keys use
    forward slashes regardless of platform (matching the embedding pipeline).
    """
    out_dir_str = str(out_dir).replace("\\", "/")

    for attribute, head in heads.items():
        buffer = io.BytesIO()
        np.savez(buffer, weights=head.weights, bias=head.bias)
        backend.put_object(f"{out_dir_str}/{attribute}.npz", buffer.getvalue())

        labels_bytes = (
            json.dumps({str(idx): name for idx, name in head.label_map.items()}, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        backend.put_object(f"{out_dir_str}/{attribute}.labels.json", labels_bytes)

    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    backend.put_object(f"{out_dir_str}/manifest.json", manifest_bytes)


def compute_head_config_hash(config: dict[str, object]) -> str:
    """Return a deterministic, key-order-independent SHA-256 of ``config``.

    Sorts keys before hashing so a config built in a different insertion order
    produces the same hash; any change to a key OR value flips the digest. This
    makes the head-artifact directory ``data/attribute_heads/<hash>/``
    self-identifying — same construction as
    ``embedding_pipeline.compute_content_hash``.
    """
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _product_of(image_id: str) -> str:
    """Return the numeric-ID directory component of an image id (the product key).

    Fashion200k ids are ``women/<cat>/<subcat>/<numeric-id>/<numeric-id>_<n>.jpeg``;
    the numeric directory groups all images of one product. Raises if no numeric
    component is present so a malformed id fails fast at the split boundary
    rather than silently collapsing distinct products together.
    """
    for part in image_id.split("/"):
        if part.isdigit():
            return part
    raise AttributeHeadError(
        f"split_by_product: image id {image_id!r} has no numeric product directory; "
        "cannot determine the product boundary"
    )
