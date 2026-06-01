"""embedding-equivalence gate.

The optimisation sweep accepts an fp16/``torch.compile`` step only if its
embeddings stay cosine-equivalent to the fp32 baseline — ``cosine >= [model]
equivalence_cosine_threshold`` (0.999). This is the embedding-quality analogue
of ``evals/qps/perf_gate.py`` and ``evals/attributes/accuracy_gate.py``: a pure
decision over already-computed numbers, with a config-driven threshold (never a
hardcoded constant).

The gate uses the **minimum** cosine over the batch of (fp32, optimised) pairs:
a single broken embedding fails the step, so an optimisation can't hide a
degraded row behind a high average. ``mean_cosine`` is reported for visibility.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EquivalenceResult:
    """Outcome of an embedding-equivalence evaluation over a batch of pairs."""

    passed: bool
    min_cosine: float
    mean_cosine: float


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity between two equal-length, non-zero vectors.

    Args:
        a: first vector.
        b: second vector (same length as ``a``).

    Returns:
        ``dot(a, b) / (||a|| * ||b||)`` in ``[-1, 1]``.

    Raises:
        ValueError: if the vectors differ in length or either has zero norm
            (cosine is undefined for a zero vector — fail fast rather than
            return a misleading 0.0).
    """
    if len(a) != len(b):
        raise ValueError(
            f"cosine_similarity: vectors must be the same length; "
            f"got {len(a)} and {len(b)}"
        )
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        raise ValueError("cosine_similarity: cannot compare a zero vector")
    return dot / (norm_a * norm_b)


def load_equivalence_threshold(config: dict[str, Any]) -> float:
    """Read ``[model] equivalence_cosine_threshold`` from a parsed config dict.

    Proves the threshold is config-driven rather than a hardcoded constant.

    Args:
        config: a parsed ``config/avsa.toml`` mapping (e.g. from ``tomllib``).

    Returns:
        The configured cosine threshold as a float.

    Raises:
        KeyError: if ``[model] equivalence_cosine_threshold`` is absent.
        ValueError: if the configured value is out of range (0, 1].
    """
    threshold = float(config["model"]["equivalence_cosine_threshold"])
    if not 0.0 < threshold <= 1.0:
        raise ValueError(
            f"[model] equivalence_cosine_threshold must be in (0, 1]; got {threshold!r}"
        )
    return threshold


def embeddings_equivalent(
    fp32: list[list[float]],
    optimised: list[list[float]],
    *,
    threshold: float,
) -> EquivalenceResult:
    """Decide whether optimised embeddings stay cosine-equivalent to fp32.

    The step PASSES iff the **minimum** per-pair cosine ``>= threshold`` — a
    single degraded embedding rejects the step. Equality at the threshold passes
    (``>=`` is inclusive).

    Args:
        fp32: the fp32 baseline embeddings (the reference).
        optimised: the optimised-config embeddings, pairwise-aligned with
            ``fp32`` (same row order, same count).
        threshold: the config-driven cosine floor (see
            :func:`load_equivalence_threshold`).

    Returns:
        An :class:`EquivalenceResult` with the pass/fail decision, the minimum
        cosine (the gated quantity), and the mean cosine (reported).

    Raises:
        ValueError: if the batches are empty or differ in length.
    """
    if not fp32 or not optimised:
        raise ValueError("embeddings_equivalent: cannot gate an empty batch")
    if len(fp32) != len(optimised):
        raise ValueError(
            "embeddings_equivalent: fp32 and optimised must have the same number "
            f"of embeddings; got {len(fp32)} and {len(optimised)}"
        )

    cosines = [
        cosine_similarity(ref, opt) for ref, opt in zip(fp32, optimised, strict=True)
    ]
    min_cosine = min(cosines)
    mean_cosine = sum(cosines) / len(cosines)
    return EquivalenceResult(
        passed=min_cosine >= threshold,
        min_cosine=min_cosine,
        mean_cosine=mean_cosine,
    )
