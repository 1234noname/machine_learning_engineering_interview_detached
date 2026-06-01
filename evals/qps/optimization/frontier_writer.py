"""measured-frontier writer.

Turns measured per-device sweep rows (from
``evals.qps.optimization.optimization_sweep.run_optimization_sweep``) into the
``frontier.json`` document — replacing the ``estimated:true`` fp16/compile rows
with MEASURED, per-device, config-hash-labelled rows. Each row records its QPS
delta, the embedding/attribute quality the step was gated on, and whether the
step was ACCEPTED (quality held) or REJECTED (silently-degraded → never a win).

Pure dict assembly + a JSON write. No model load, no torch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.qps.optimization.optimization_sweep import SweepStepResult


def build_device_frontier(
    *,
    device: str,
    config_hash: str,
    rows: list[SweepStepResult],
) -> dict[str, Any]:
    """Assemble one device's measured ablation block.

    Args:
        device: the device tier (``cpu``/``mps``/``cuda``) these rows ran on.
        config_hash: the reproducibility hash of the config the sweep ran under.
        rows: the per-step results from :func:`run_optimization_sweep`.

    Returns:
        A dict ``{"device", "config_hash", "ablation": [<row>, ...]}`` where each
        row is a JSON-serialisable view of a :class:`SweepStepResult`.
    """
    return {
        "device": device,
        "config_hash": config_hash,
        "ablation": [_row_to_dict(r) for r in rows],
    }


def write_frontier(
    path: Path,
    *,
    device_blocks: list[dict[str, Any]],
    roofline_note: str,
) -> Path:
    """Write the per-device measured frontier document to ``path``.

    Args:
        path: destination ``frontier.json`` path (parents created if needed).
        device_blocks: one block per device from :func:`build_device_frontier`.
        roofline_note: the honest roofline summary — must state that CPU/MPS
            gains are often flat/negative and that GPU is the
            authoritative win.

    Returns:
        The :class:`Path` written.
    """
    doc: dict[str, Any] = {
        "generated": datetime.now(tz=UTC).isoformat(),
        "measured": True,
        "roofline_note": roofline_note,
        "devices": device_blocks,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    return path


def _row_to_dict(row: SweepStepResult) -> dict[str, Any]:
    return {
        "step": row.step,
        "device": row.device,
        "estimated": row.estimated,
        "qps": row.qps,
        "p50_ms": row.p50_ms,
        "p95_ms": row.p95_ms,
        "delta_qps": row.delta_qps,
        "delta_pct": row.delta_pct,
        "min_cosine": row.min_cosine,
        "mean_cosine": row.mean_cosine,
        "category_top1": row.category_top1,
        "cosine_ok": row.cosine_ok,
        "accuracy_ok": row.accuracy_ok,
        "accepted": row.accepted,
        "reason": row.reason,
    }
