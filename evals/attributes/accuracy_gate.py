"""Classification-accuracy quality gate.

The classification analogue of the embedding-equivalence (cosine) gate in
``apps/model/tests/test_vit_optimisation.py``: where that gate asserts an
optimised model config (fp16 / ``torch.compile`` / INT8) produces embeddings
cosine-equivalent to the fp32 baseline, this gate asserts attribute-
classification top-1 accuracy is not silently traded for speed. Both must pass
for an optimisation step to be accepted.

This module *measures only* — it does not touch the verifier or the
``RetrievalTool``. The tolerance is config-driven (read from
``[evals.attributes] accuracy_tolerance`` in ``config/avsa.toml``), never a
hardcoded constant.
"""

from __future__ import annotations

from typing import Any


def accuracy_within_tolerance(
    baseline_top1: float,
    optimised_top1: float,
    tolerance: float,
) -> bool:
    """Return whether an optimised config's top-1 stays within ``tolerance``.

    The gate passes when ``optimised_top1 >= baseline_top1 - tolerance`` — i.e.
    a drop of at most ``tolerance`` (in absolute top-1) is allowed; equality at
    the floor passes (inclusive), and any improvement over the baseline passes.

    Args:
        baseline_top1: fp32 baseline top-1 accuracy, in [0, 1].
        optimised_top1: optimised config top-1 accuracy, in [0, 1].
        tolerance: maximum allowed absolute top-1 drop, in (0, 1]. Pass the
            config value via :func:`load_accuracy_tolerance` — do not hardcode.

    Returns:
        ``True`` if the optimised accuracy is within tolerance of the baseline.

    Raises:
        ValueError: if any input is out of its valid range.
    """
    if not 0.0 <= baseline_top1 <= 1.0:
        raise ValueError(f"baseline_top1 must be in [0, 1]; got {baseline_top1!r}")
    if not 0.0 <= optimised_top1 <= 1.0:
        raise ValueError(f"optimised_top1 must be in [0, 1]; got {optimised_top1!r}")
    if not 0.0 < tolerance <= 1.0:
        raise ValueError(f"tolerance must be in (0, 1]; got {tolerance!r}")

    return optimised_top1 >= baseline_top1 - tolerance


def load_accuracy_tolerance(config: dict[str, Any]) -> float:
    """Read the config-driven accuracy tolerance from a parsed config dict.

    Reads ``config["evals"]["attributes"]["accuracy_tolerance"]`` — the single
    source of truth for the gate's tolerance, proving it is config-driven rather
    than a hardcoded constant.

    Args:
        config: a parsed ``config/avsa.toml`` mapping (e.g. from ``tomllib``).

    Returns:
        The configured ``accuracy_tolerance`` as a float.

    Raises:
        KeyError: if the ``[evals.attributes] accuracy_tolerance`` key is absent.
        ValueError: if the configured value is out of range (0, 1].
    """
    tolerance = float(config["evals"]["attributes"]["accuracy_tolerance"])
    if not 0.0 < tolerance <= 1.0:
        raise ValueError(
            "[evals.attributes] accuracy_tolerance must be in (0, 1]; "
            f"got {tolerance!r}"
        )
    return tolerance
