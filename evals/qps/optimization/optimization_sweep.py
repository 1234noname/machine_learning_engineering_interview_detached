""" optimisation sweep runner.

Runs the model-direct bench across ``{fp32, fp16, compile}`` on one
device tier and turns each step into a MEASURED frontier row. Every non-baseline
step is gated from two sides — an optimisation that breaks quality is REJECTED
(``accepted=False``), never recorded as a QPS win:

1. **Embedding equivalence** — optimised-vs-fp32 cosine ``>=
   equivalence_cosine_threshold`` (``evals.qps.optimization.equivalence``).
2. **Attribute accuracy** — optimised attribute top-1 ``>= fp32 top-1 -
   accuracy_tolerance`` (``evals.attributes.accuracy_gate``).

The fp32 step is the reference: it is never gated (there is nothing to compare
against) and it supplies the accuracy floor for the optimised steps.

The heavy model work (load weights on the device, embed an equivalence set,
measure QPS via the model-direct bench) is INJECTED as a ``probe``
callable returning a :class:`StepProbe` per step. The runner itself is a pure
gate + row-assembly decision, so it is unit-tested without loading the real
model or saturating the model service. The driver script wires the real probe;
this module never imports torch.

A flat-or-negative ``delta_qps`` on CPU/MPS is honest and ACCEPTED (the quality
gates held) — rejection is strictly a quality decision, not a speed one. GPU
 is where fp16/compile is expected to pay off.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from evals.attributes.accuracy_gate import accuracy_within_tolerance
from evals.qps.optimization.equivalence import embeddings_equivalent


@dataclass(frozen=True)
class OptimisationStep:
    """One point in the optimisation ablation."""

    name: str
    use_fp16: bool
    use_compile: bool
    use_int8: bool = False
    # torch.compile mode when use_compile=True. "reduce-overhead" minimises
    # graph-capture and dispatch cost; "max-autotune" additionally searches over
    # tiling strategies for the exact input shapes — higher warmup cost, better
    # steady-state kernel selection.
    compile_mode: str = "reduce-overhead"
    # Warmup passes to discard before timing. max-autotune's shape search runs
    # during the first several calls, so it needs more warmup than reduce-overhead.
    warmup_passes: int = 3


@dataclass(frozen=True)
class StepProbe:
    """What the model produced under one optimisation config (injected).

    The boundary contract between the runner and the real model/bench: the
    measured QPS/latency from the model-direct bench, plus the fp32 and
    optimised embeddings over a fixed equivalence set and the optimised
    attribute top-1. For the fp32 step, ``fp32_embeddings`` and
    ``optimised_embeddings`` are identical (the reference compares to itself).
    """

    qps: float
    p50_ms: float
    p95_ms: float
    fp32_embeddings: list[list[float]]
    optimised_embeddings: list[list[float]]
    category_top1: float


@dataclass(frozen=True)
class SweepStepResult:
    """A measured, gated frontier row for one (device, optimisation) point."""

    step: str
    device: str
    estimated: bool
    qps: float
    p50_ms: float
    p95_ms: float
    delta_qps: float | None
    delta_pct: float | None
    min_cosine: float
    mean_cosine: float
    category_top1: float
    cosine_ok: bool
    accuracy_ok: bool
    accepted: bool
    reason: str


# The ablation matrix: fp32 -> fp16 -> fp16+compile(reduce-overhead) ->
# int8 -> max-autotune -> fp16+max-autotune.
#
# "compile" (fp16+reduce-overhead): the original torch.compile attempt.
# "max-autotune" / "fp16-max-autotune": compile mode that additionally searches
# over tiling strategies for fixed input shapes (batch=1-24, seq=197, hidden=768).
# Higher warmup cost; different search from reduce-overhead.
# int8: torch.quantization.quantize_dynamic on nn.Linear (CPU only).
# (ONNX / TensorRT are deferred follow-ups per the spec.)
OPTIMISATION_STEPS: list[OptimisationStep] = [
    OptimisationStep(name="fp32", use_fp16=False, use_compile=False, use_int8=False),
    OptimisationStep(name="fp16", use_fp16=True, use_compile=False, use_int8=False),
    OptimisationStep(
        name="compile",
        use_fp16=True,
        use_compile=True,
        use_int8=False,
        compile_mode="reduce-overhead",
    ),
    OptimisationStep(name="int8", use_fp16=False, use_compile=False, use_int8=True),
    OptimisationStep(
        name="max-autotune",
        use_fp16=False,
        use_compile=True,
        use_int8=False,
        compile_mode="max-autotune",
        warmup_passes=15,
    ),
    OptimisationStep(
        name="fp16-max-autotune",
        use_fp16=True,
        use_compile=True,
        use_int8=False,
        compile_mode="max-autotune",
        warmup_passes=15,
    ),
]


def run_optimization_sweep(
    *,
    device: str,
    probe: Callable[[OptimisationStep], StepProbe],
    equivalence_threshold: float,
    accuracy_tolerance: float,
) -> list[SweepStepResult]:
    """Run the fp32 -> fp16 -> compile ablation on one device, gated per step.

    Args:
        device: the device tier this sweep ran on (``cpu``/``mps``/``cuda``);
            recorded on every row.
        probe: callable returning the :class:`StepProbe` for a given step (the
            injected model/bench boundary — see the module docstring).
        equivalence_threshold: config-driven cosine floor for the embedding gate.
        accuracy_tolerance: config-driven max top-1 drop for the gate.

    Returns:
        One :class:`SweepStepResult` per step in :data:`OPTIMISATION_STEPS`
        order. The fp32 row is the baseline (no delta, ungated); each optimised
        row carries its QPS delta vs fp32 and the two gate decisions.
    """
    baseline = probe(OPTIMISATION_STEPS[0])
    fp32_qps = baseline.qps
    fp32_category_top1 = baseline.category_top1

    rows: list[SweepStepResult] = []
    for step in OPTIMISATION_STEPS:
        result = (
            _baseline_row(step, baseline, device)
            if step.name == "fp32"
            else _optimised_row(
                step,
                probe(step),
                device=device,
                fp32_qps=fp32_qps,
                fp32_category_top1=fp32_category_top1,
                equivalence_threshold=equivalence_threshold,
                accuracy_tolerance=accuracy_tolerance,
            )
        )
        rows.append(result)
    return rows


def _baseline_row(
    step: OptimisationStep, probe: StepProbe, device: str
) -> SweepStepResult:
    """The fp32 reference row: ungated, no delta, the accuracy/quality anchor."""
    return SweepStepResult(
        step=step.name,
        device=device,
        estimated=False,
        qps=probe.qps,
        p50_ms=probe.p50_ms,
        p95_ms=probe.p95_ms,
        delta_qps=None,
        delta_pct=None,
        min_cosine=1.0,
        mean_cosine=1.0,
        category_top1=probe.category_top1,
        cosine_ok=True,
        accuracy_ok=True,
        accepted=True,
        reason="baseline (fp32 reference — ungated)",
    )


def _optimised_row(
    step: OptimisationStep,
    probe: StepProbe,
    *,
    device: str,
    fp32_qps: float,
    fp32_category_top1: float,
    equivalence_threshold: float,
    accuracy_tolerance: float,
) -> SweepStepResult:
    """Build a gated, measured row for one optimised step (fp16/compile)."""
    equiv = embeddings_equivalent(
        probe.fp32_embeddings,
        probe.optimised_embeddings,
        threshold=equivalence_threshold,
    )
    accuracy_ok = accuracy_within_tolerance(
        fp32_category_top1, probe.category_top1, accuracy_tolerance
    )
    accepted = equiv.passed and accuracy_ok

    delta_qps = probe.qps - fp32_qps
    delta_pct = (delta_qps / fp32_qps * 100.0) if fp32_qps != 0.0 else None

    return SweepStepResult(
        step=step.name,
        device=device,
        estimated=False,
        qps=probe.qps,
        p50_ms=probe.p50_ms,
        p95_ms=probe.p95_ms,
        delta_qps=delta_qps,
        delta_pct=delta_pct,
        min_cosine=equiv.min_cosine,
        mean_cosine=equiv.mean_cosine,
        category_top1=probe.category_top1,
        cosine_ok=equiv.passed,
        accuracy_ok=accuracy_ok,
        accepted=accepted,
        reason=_reason(
            equiv_passed=equiv.passed,
            min_cosine=equiv.min_cosine,
            threshold=equivalence_threshold,
            accuracy_ok=accuracy_ok,
            optimised_top1=probe.category_top1,
            fp32_top1=fp32_category_top1,
            tolerance=accuracy_tolerance,
        ),
    )


def _reason(
    *,
    equiv_passed: bool,
    min_cosine: float,
    threshold: float,
    accuracy_ok: bool,
    optimised_top1: float,
    fp32_top1: float,
    tolerance: float,
) -> str:
    """Human-readable accept/reject reason naming the gate(s) that fired."""
    if equiv_passed and accuracy_ok:
        return (
            f"ACCEPTED: cosine {min_cosine:.6f} >= {threshold:g} and "
            f"top-1 {optimised_top1:.4f} >= floor {fp32_top1 - tolerance:.4f}"
        )
    parts: list[str] = []
    if not equiv_passed:
        parts.append(
            f"embedding cosine regression: min {min_cosine:.6f} < {threshold:g}"
        )
    if not accuracy_ok:
        parts.append(
            f"attribute accuracy regression: top-1 {optimised_top1:.4f} < floor "
            f"{fp32_top1 - tolerance:.4f} (fp32 {fp32_top1:.4f} - tol {tolerance:g})"
        )
    return "REJECTED: " + "; ".join(parts)
