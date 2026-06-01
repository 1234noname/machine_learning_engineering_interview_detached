"""TextEncoder - real CLIP text encoder via sentence-transformers.

Model choice: clip-ViT-B-32 text encoder.
It natively produces 512-dim vectors that are in the same CLIP space as the
image ViT embedder, which fits a multimodal retrieval
system where image and text embeddings must be compared by cosine similarity.

This module is only imported when AVSA_MODEL_STUB=0.  In stub mode (all CI
runs) this file is never imported, so sentence-transformers and torch are not
required.
"""

from __future__ import annotations

import logging
import math
import pathlib
import tomllib
from typing import Any

_log = logging.getLogger(__name__)


def _load_text_encoder_config() -> dict[str, Any]:
    """Read [text_encoder] section from config/avsa.toml. Returns {} if not found."""
    for parent in [pathlib.Path(__file__).resolve(), *pathlib.Path(__file__).resolve().parents]:
        candidate = parent / "config" / "avsa.toml"
        if candidate.exists():
            with open(candidate, "rb") as f:
                return tomllib.load(f).get("text_encoder", {})
    return {}


class TextEncoder:
    """Loads the CLIP text encoder once and serves embed_text requests.

    The model is loaded in __init__ - call this once at application startup,
    not per-request.

    Args:
        model_name: HuggingFace model ID. When None, reads from
            config/avsa.toml [text_encoder].model (default "clip-ViT-B-32").
        dim: Expected output dimension. When None, reads from
            config/avsa.toml [text_encoder].dim (default 512).
    """

    def __init__(
        self,
        model_name: str | None = None,
        dim: int | None = None,
    ) -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]

        cfg = _load_text_encoder_config()
        self._model_name: str = model_name or str(cfg.get("model", "clip-ViT-B-32"))
        self._dim: int = dim if dim is not None else int(cfg.get("dim", 512))

        _log.info("Loading text encoder: %s", self._model_name)
        self._model: SentenceTransformer = SentenceTransformer(self._model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Encode texts; return one self._dim-dim L2-normalised embedding per text."""
        vectors = self._model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        result: list[list[float]] = []
        for vec in vectors:
            raw: list[float] = vec.tolist()
            if len(raw) != self._dim:
                raise RuntimeError(
                    f"Model {self._model_name!r} produced {len(raw)}-dim vectors; "
                    f"expected {self._dim}-dim as configured in [text_encoder].dim"
                )
            result.append(_l2_normalise(raw))
        return result


def _l2_normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]
