"""Attribute-head application for the model service.

The model service applies the trained linear attribute heads to the SAME
L2-normalised 768-d embedding /embed already returns, yielding
category/colour predictions in a single backbone pass. This module is
the model-side applier; it deliberately does NOT import avsa_api (the
architecture decision: apps/model must not depend on avsa_api). The
inference is trivial and linear, so it is reimplemented here and reads the
shared head .npz artifact directly.

The artifact layout (written by scripts/train-attribute-heads.py through
avsa_data.attribute_heads.write_head_artifact) is, per attribute:

- <attribute>.npz - weights (768, n_classes) + bias
  (n_classes,) float arrays.
- <attribute>.labels.json - the {class_index: class_name} label map.

Inference matches avsa_data.attribute_heads.evaluate EXACTLY:
argmax(vec @ weights + bias) mapped through the label map. The .npz
fully captures the head - there is no extra feature preprocessing (the vector
fed in is already the L2-normalised 768-d embedding). The added softmax only
turns the winning score into a confidence in [0, 1]; it does not change the
argmax.

The real inference path is real-mode only (the heads are loaded/applied when
AVSA_MODEL_STUB=0); it depends on numpy (already present in the resolved
real-mode env via torch). numpy is imported lazily INSIDE the functions that use
it (load/predict/_apply_head/_softmax/_load_head) - mirroring how vit.py
keeps its heavy backends out of the stub import path - so this module (and the
config-driven resolve_attribute_heads_dir) imports cleanly with numpy absent
(e.g. the stub CI env, which never reaches the inference functions).
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

# Feature dimension of the frozen ViT-b-16 image embedding the heads were
# trained on. Pinned so a malformed artifact / wrong-dim vector
# fails fast at the boundary rather than skewing the matmul.
IMAGE_DIM = 768

_ATTRIBUTES = ("category", "colour")


class AttributeHeadError(Exception):
    """Raised when a head artifact is missing/garbled or a vector is malformed.

    A typed domain error so the model service sees a clear failure at the
    head-loading / prediction boundary rather than a bare numpy/KeyError.
    """


@dataclass(frozen=True)
class AttributePrediction:
    """One image's predicted attributes plus per-head softmax confidences."""

    category: str
    colour: str
    category_confidence: float
    colour_confidence: float


@dataclass(frozen=True)
class _Head:
    """A single loaded linear head: argmax(vec @ weights + bias) → label.

    Mirrors avsa_data.attribute_heads.LinearHead (weights (768, n_classes),
    bias (n_classes,), {class_index: class_name}) without importing it.
    """

    weights: NDArray[np.float64]
    bias: NDArray[np.float64]
    label_map: dict[int, str]


def resolve_attribute_heads_dir(config: dict[str, Any]) -> Path:
    """Resolve the head-artifact directory from [model] attribute_heads_dir.

    Config-driven, not hardcoded: reads config["model"]["attribute_heads_dir"]
    so the location is set in config/avsa.toml (and overridable in tests).
    Raises :class:AttributeHeadError if the key is absent so a misconfigured
    service fails fast rather than silently loading from a guessed path.
    """
    model_cfg = config.get("model", {})
    heads_dir = model_cfg.get("attribute_heads_dir")
    if not heads_dir:
        raise AttributeHeadError(
            "resolve_attribute_heads_dir: config is missing [model] attribute_heads_dir; "
            "the head-artifact location must be config-driven"
        )
    return Path(str(heads_dir))


class AttributeHeads:
    """The linear heads, applied to a 768-d embedding for category/colour.

    Loaded once at startup from the configured directory; predict is a cheap
    matmul over the same L2-normalised embedding /embed returns.
    """

    def __init__(self, category: _Head, colour: _Head) -> None:
        self._category = category
        self._colour = colour

    @classmethod
    def load(cls, heads_dir: Path) -> AttributeHeads:
        """Load the category + colour heads from heads_dir.

        Reads each <attribute>.npz (weights + bias) and
        <attribute>.labels.json (the index→label map). Uses
        np.load(..., allow_pickle=False) so a head derived from untrusted
        dataset bytes can never deserialize a pickled object array.
        """
        heads_dir = Path(heads_dir)
        return cls(
            category=_load_head(heads_dir, "category"),
            colour=_load_head(heads_dir, "colour"),
        )

    def predict_batch(self, embeddings: list[list[float]]) -> list[AttributePrediction]:
        """Predict category/colour for a batch in one matrix multiply per head.

        Replaces N calls to :meth:predict with a single (N, 768) @ (768,
        n_classes) matmul, then a fully-vectorised softmax. Called by
        :meth:~avsa_model.vit.VitEmbedder.embed_with_attributes instead of
        the per-vector loop.
        """
        import numpy as np

        vecs = np.asarray(embeddings, dtype=np.float64)  # (N, 768)
        if vecs.ndim != 2 or vecs.shape[1] != IMAGE_DIM:
            raise AttributeHeadError(
                f"AttributeHeads.predict_batch: embeddings must be (N, {IMAGE_DIM}); "
                f"got shape {vecs.shape}"
            )
        cat_labels, cat_confs = _apply_head_batch(self._category, vecs)
        col_labels, col_confs = _apply_head_batch(self._colour, vecs)
        return [
            AttributePrediction(
                category=cat_labels[i],
                colour=col_labels[i],
                category_confidence=cat_confs[i],
                colour_confidence=col_confs[i],
            )
            for i in range(len(vecs))
        ]

    def predict(self, vec: list[float] | NDArray[np.float64]) -> AttributePrediction:
        """Predict category/colour for one L2-normalised 768-d embedding.

        For each head: scores = vec @ weights + bias; the label is
        label_map[argmax(scores)]; the confidence is softmax(scores) at
        the winning index.
        """
        import numpy as np

        vector = np.asarray(vec, dtype=np.float64)
        if vector.ndim != 1 or vector.shape[0] != IMAGE_DIM:
            raise AttributeHeadError(
                f"AttributeHeads.predict: vector must be length-{IMAGE_DIM} 1-D; "
                f"got shape {vector.shape}"
            )
        cat_label, cat_conf = _apply_head(self._category, vector)
        col_label, col_conf = _apply_head(self._colour, vector)
        return AttributePrediction(
            category=cat_label,
            colour=col_label,
            category_confidence=cat_conf,
            colour_confidence=col_conf,
        )


def _apply_head_batch(head: _Head, vectors: NDArray[np.float64]) -> tuple[list[str], list[float]]:
    """Return (labels, confidences) for a batch via one matmul + vectorised softmax."""
    import numpy as np

    scores = vectors @ head.weights + head.bias  # (N, n_classes)
    idxs = np.argmax(scores, axis=1)  # (N,)
    shifted = scores - scores.max(axis=1, keepdims=True)
    exp = np.exp(shifted)
    probs = exp / exp.sum(axis=1, keepdims=True)  # (N, n_classes)
    confs: list[float] = probs[np.arange(len(idxs)), idxs].tolist()
    labels: list[str] = [head.label_map[int(i)] for i in idxs]
    return labels, confs


def _apply_head(head: _Head, vector: NDArray[np.float64]) -> tuple[str, float]:
    """Return (label, confidence) for one head applied to vector.

    argmax(vec @ weights + bias) selects the label; softmax
    of the same scores gives the confidence at the winning index.
    """
    import numpy as np

    scores = vector @ head.weights + head.bias
    idx = int(np.argmax(scores))
    label = head.label_map[idx]
    confidence = float(_softmax(scores)[idx])
    return label, confidence


def _softmax(scores: NDArray[np.float64]) -> NDArray[np.float64]:
    """Numerically-stable softmax (subtract the max before exponentiating).

    Confidence only: the argmax is taken on the raw scores, so softmax never
    changes the predicted label - it just normalises the winning score into a
    probability in [0, 1].
    """
    import numpy as np

    shifted = scores - np.max(scores)
    exp = np.exp(shifted)
    return exp / np.sum(exp)


def _load_head(heads_dir: Path, attribute: str) -> _Head:
    """Load one attribute head (.npz weights/bias + .labels.json map)."""
    import numpy as np

    npz_path = heads_dir / f"{attribute}.npz"
    labels_path = heads_dir / f"{attribute}.labels.json"
    if not npz_path.exists() or not labels_path.exists():
        raise AttributeHeadError(
            f"_load_head: head artifact for {attribute!r} is incomplete under {heads_dir}; "
            f"expected {npz_path.name} and {labels_path.name}"
        )

    with np.load(io.BytesIO(npz_path.read_bytes()), allow_pickle=False) as data:
        weights = np.asarray(data["weights"], dtype=np.float64)
        bias = np.asarray(data["bias"], dtype=np.float64)

    if weights.ndim != 2 or weights.shape[0] != IMAGE_DIM:
        raise AttributeHeadError(
            f"_load_head: {attribute!r} weights must be ({IMAGE_DIM}, n_classes); "
            f"got {weights.shape}"
        )

    labels_obj: dict[str, str] = json.loads(labels_path.read_text(encoding="utf-8"))
    label_map = {int(idx): str(name) for idx, name in labels_obj.items()}
    return _Head(weights=weights, bias=bias, label_map=label_map)
