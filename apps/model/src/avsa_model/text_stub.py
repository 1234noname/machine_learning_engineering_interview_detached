"""Stub text embedder - deterministic 512-dim embeddings via SHA-256. No model weights."""

import hashlib
import math


class StubTextEmbedder:
    """Returns deterministic unit-normalised 512-dim embeddings from SHA-256.

    Algorithm (mirrors StubEmbedder in stub.py, adapted for text and 512-dim):
      1. Compute sha256(text.encode('utf-8')) → 32 raw bytes.
      2. Expand to 512 floats: raw[i % 32] / 255.0 for i in range(512).
      3. L2-normalise the vector.

    Same text always produces the same vector. No network calls; no model weights.
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one 512-dim unit-normalised embedding per text."""
        return [self._embed_one(t) for t in texts]

    @staticmethod
    def _embed_one(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()  # 32 bytes
        raw: list[float] = [digest[i % 32] / 255.0 for i in range(512)]
        return _l2_normalise(raw)


def _l2_normalise(vector: list[float]) -> list[float]:
    """Return *vector* scaled to unit L2 norm.

    If the norm is zero (all-zero vector) the vector is returned unchanged to
    avoid division by zero - pgvector will treat a zero vector as-is.
    """
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]
