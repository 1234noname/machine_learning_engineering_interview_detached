"""The classification analogue of test_vit_optimisation.py's embedding-equivalence
(cosine) gate: every  model-optimisation step (fp16 / torch.compile / INT8)
must keep attribute-classification top-1 within a config-driven tolerance of the
fp32 baseline. This module measures only - it does NOT touch the verifier or the
RetrievalTool (anti-collusion).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

_STUB_MODE = os.environ.get("AVSA_MODEL_STUB") == "1"


def _find_repo_root() -> Path:
    """Walk up from this file until config/avsa.toml is found (repo root)."""
    path = Path(__file__).resolve()
    for parent in [path, *path.parents]:
        if (parent / "config" / "avsa.toml").exists():
            return parent
    raise FileNotFoundError("config/avsa.toml not found walking up from tests/")


def _load_config() -> dict[str, Any]:
    root = _find_repo_root()
    with (root / "config" / "avsa.toml").open("rb") as f:
        return tomllib.load(f)


def test_config_has_accuracy_tolerance() -> None:
    """avsa.toml must carry [evals.attributes] accuracy_tolerance as a float in (0, 1].

    The optimisation gate's tolerance must be config-driven (mirrors
    [model] equivalence_cosine_threshold), never a hardcoded constant.
    """
    config = _load_config()
    attributes_cfg = config.get("evals", {}).get("attributes", {})
    tolerance = attributes_cfg.get("accuracy_tolerance")
    assert tolerance is not None, (
        "accuracy_tolerance missing from [evals.attributes] in config/avsa.toml - "
        "the max allowed absolute top-1 drop of an optimised config vs the fp32 "
        f"baseline; got section {attributes_cfg!r}"
    )
    assert isinstance(tolerance, float), (
        f"accuracy_tolerance must be a float, got {type(tolerance).__name__}"
    )
    assert 0.0 < tolerance <= 1.0, f"accuracy_tolerance out of range (0, 1]: {tolerance}"
