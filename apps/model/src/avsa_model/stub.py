"""Stub embedder - deterministic 768-dim embeddings + attributes via SHA-256.

No model weights, no network, no head artifact: everything is derived from the
SHA-256 digest of the image bytes so model-ci stays green and the output is
deterministic (same bytes → same embedding AND same attributes).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

# The category set mirrors the head's
# trained classes; colour is a small fixed subset of the fashion colour vocab.
# They let stub mode return well-formed, deterministic
# attributes (a function of the image bytes) without any weights. The real-mode
# vocabularies come from the head artifact's label maps (avsa_model.heads).
_STUB_CATEGORIES = ("dress", "jacket", "pants", "skirt", "top")
_STUB_COLOURS = ("black", "blue", "green", "red", "white", "multicolour")


@dataclass(frozen=True)
class StubAttribute:
    """A deterministic stub attribute prediction (shape matches the real heads)."""

    category: str
    colour: str
    category_confidence: float
    colour_confidence: float


class StubEmbedder:
    """Returns deterministic unit-normalised 768-dim embeddings + attributes.

    Embedding algorithm (unchanged, the backward-compat contract):
      1. Compute sha256(image_bytes) → 32 raw bytes.
      2. Expand to 768 floats: raw[i % 32] / 255.0 for i in range(768).
      3. L2-normalise the vector.

    Attribute algorithm (no weights/network): index the fixed vocabularies by
    digest bytes, and derive confidences from a digest byte scaled into a
    plausible [0.5, 1.0) range. Same bytes always produce the same vector
    AND the same attributes.
    """

    def embed_with_attributes(
        self, images: list[bytes]
    ) -> tuple[list[list[float]], list[StubAttribute]]:
        """Return embeddings and parallel deterministic attributes per image.

        The embeddings use the documented sha256 → 768-float → L2-normalise
        algorithm; attributes are added alongside. One backbone pass
        conceptually, though stub mode does no real inference.
        """
        embeddings: list[list[float]] = []
        attributes: list[StubAttribute] = []
        for img in images:
            digest = hashlib.sha256(img).digest()
            embeddings.append(_l2_normalise([digest[i % 32] / 255.0 for i in range(768)]))
            attributes.append(_stub_attribute(digest))
        return embeddings, attributes


def _stub_attribute(digest: bytes) -> StubAttribute:
    """Derive a deterministic attribute prediction from a SHA-256 digest.

    Category/colour are selected by indexing the fixed vocabularies with digest
    bytes; confidences are a digest byte mapped into [0.5, 1.0) so they are
    valid probabilities that vary across distinct inputs.
    """
    category = _STUB_CATEGORIES[digest[0] % len(_STUB_CATEGORIES)]
    colour = _STUB_COLOURS[digest[1] % len(_STUB_COLOURS)]
    category_confidence = 0.5 + (digest[2] / 255.0) * 0.5
    colour_confidence = 0.5 + (digest[3] / 255.0) * 0.5
    return StubAttribute(
        category=category,
        colour=colour,
        category_confidence=category_confidence,
        colour_confidence=colour_confidence,
    )


def _l2_normalise(vector: list[float]) -> list[float]:
    """Return *vector* scaled to unit L2 norm.

    If the norm is zero (all-zero vector) the vector is returned unchanged to
    avoid division by zero - pgvector will treat a zero vector as-is.
    """
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]
