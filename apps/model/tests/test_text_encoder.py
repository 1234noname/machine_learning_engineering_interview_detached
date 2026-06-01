"""Real-mode tests for the CLIP text encoder (avsa_model.text_encoder).

Symmetric to test_vit_optimisation.py for the image ViT: these exercise the
REAL TextEncoder (sentence-transformers / clip-ViT-B-32) and are skipped when
AVSA_MODEL_STUB=1 (every CI/stub run), where torch + sentence-transformers
are not installed. The stub text path is covered by test_embed_text.py.

Run against the real model (downloads / loads clip-ViT-B-32 on first use):

    cd apps/model && AVSA_MODEL_STUB=0 uv run --extra model pytest tests/test_text_encoder.py

The encoder is loaded inside each test (not at import) so collection stays clean
in the stub env where sentence-transformers is absent. If the model extra is not
installed the import fails and the test SKIPs (never errors).
"""

from __future__ import annotations

import math
import os

import pytest

_STUB_MODE = os.environ.get("AVSA_MODEL_STUB") == "1"

_DIM = 512

_SKIP_REASON = "real CLIP text encoder unavailable in CI (AVSA_MODEL_STUB=1)"


@pytest.mark.skipif(_STUB_MODE, reason=_SKIP_REASON)
def test_text_encoder_returns_512_dim_l2_normalised() -> None:
    """The real encoder returns one 512-dim, L2-normalised vector per input text."""
    try:
        from avsa_model.text_encoder import TextEncoder
    except ImportError as exc:
        pytest.skip(f"text-encoder deps (sentence-transformers/torch) not installed: {exc}")

    encoder = TextEncoder()
    vectors = encoder.embed(["a red floral summer dress", "blue denim jacket"])

    assert len(vectors) == 2, f"expected one vector per input text; got {len(vectors)}"
    for vec in vectors:
        assert len(vec) == _DIM, f"expected {_DIM}-dim CLIP text embedding; got {len(vec)}"
        norm = math.sqrt(sum(x * x for x in vec))
        assert math.isclose(norm, 1.0, abs_tol=1e-3), (
            f"text embedding must be L2-normalised (matches the image embedder); got norm {norm}"
        )


@pytest.mark.skipif(_STUB_MODE, reason=_SKIP_REASON)
def test_text_encoder_is_deterministic() -> None:
    """Encoding identical text twice yields identical vectors (stable retrieval keys)."""
    try:
        from avsa_model.text_encoder import TextEncoder
    except ImportError as exc:
        pytest.skip(f"text-encoder deps (sentence-transformers/torch) not installed: {exc}")

    encoder = TextEncoder()
    first = encoder.embed(["a red floral summer dress"])[0]
    second = encoder.embed(["a red floral summer dress"])[0]
    assert first == second, "CLIP text encoder must be deterministic for identical input"
