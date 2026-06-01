"""Classification QPS-overhead computation + report writer.

Makes AVSA's classification (the #068 category/colour attribute heads) a
*measured* concern: how much `/embed` throughput the attribute heads cost
(embedding-only vs embedding+attributes), recorded alongside the category/colour
top-1 so the speed/accuracy trade is visible in one place (TR1 + TR2).

The linear probes ride the frozen 768-d backbone features in ONE backbone pass
(``VitEmbedder.embed_with_attributes`` runs the backbone once, then a cheap
per-vector ``weights @ vec + bias`` matmul), so the overhead is expected to be
small — but the *actual* % must be measured, not assumed.

This module is the classification analogue of
``evals/qps/story_018/optimization_sweep.py``: the heavy model work (load the
real ViT on a device, time the backbone-only path vs the heads path, compute
top-1 over the #067 held-out split) is INJECTED as an :class:`OverheadProbe`.
The runner here is a pure arithmetic + report-assembly decision, so it is
unit-tested without loading the real model. The driver
(``scripts/bench-classification-overhead.py``) wires the real probe; this module
never imports torch.

The overhead is an **L1** concern — raw ``/embed`` at the model — independent of
#089's per-turn embed cache: the probe times the model directly, not through the
cache.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class OverheadProbe:
    """The injected measurement boundary: embedding-only vs +attributes timings.

    The contract between the runner and the real model/bench. Both paths share
    the SAME single backbone forward; the only difference is whether the
    per-vector head matmul also runs. The two QPS numbers are measured at the
    model (L1), not through the #089 cache.

    Attributes:
        device: the device tier the probe ran on (``cpu``/``mps``/``cuda``).
        embedding_only_qps: throughput of the backbone-only path (heads skipped).
        with_attributes_qps: throughput of the full ``embed_with_attributes``
            path (backbone + the #068 heads).
        embedding_only_p50_ms / embedding_only_p95_ms: backbone-only latency.
        with_attributes_p50_ms / with_attributes_p95_ms: +attributes latency.
        category_top1: measured category top-1 over the held-out set, in [0, 1].
        colour_top1: measured colour top-1 (description-derived, noisier), in
            [0, 1].
    """

    device: str
    embedding_only_qps: float
    with_attributes_qps: float
    embedding_only_p50_ms: float
    with_attributes_p50_ms: float
    embedding_only_p95_ms: float
    with_attributes_p95_ms: float
    category_top1: float
    colour_top1: float


class _Embedder(Protocol):
    """The model surface the timing harness needs (satisfied by VitEmbedder).

    The embedding-only path (:meth:`embed`) runs the backbone forward only; the
    +attributes path (:meth:`embed_with_attributes`) adds the #068 head matmul on
    the same backbone output. ``device`` is the tier the embedder runs on.
    """

    device: str

    def embed(self, images: list[bytes]) -> list[list[float]]: ...

    def embed_with_attributes(
        self, images: list[bytes]
    ) -> tuple[list[list[float]], list[object]]: ...


def measure_overhead_probe(
    embedder: _Embedder,
    *,
    images: list[bytes],
    category_top1: float,
    colour_top1: float,
    timed_passes: int,
    warmup_passes: int,
) -> OverheadProbe:
    """Time the embedding-only vs +attributes paths over a fixed image batch.

    Both paths share the SAME single backbone forward; only the +attributes path
    additionally runs the per-vector #068 head matmul. Timing them against each
    other isolates the head-compute cost. This is the L1 measurement — the model
    directly, not through the #089 per-turn embed cache.

    Args:
        embedder: an object exposing ``embed`` (embedding-only), and
            ``embed_with_attributes`` (+attributes), plus a ``device`` attribute.
        images: the fixed image batch processed on every pass.
        category_top1 / colour_top1: the measured held-out top-1 to carry through
            (computed by the driver against the #067 split), in [0, 1].
        timed_passes: number of timed passes per path (must be >= 1).
        warmup_passes: number of discarded warmup passes per path (>= 0) — the
            first call traces/JITs under torch.compile and would skew QPS.

    Returns:
        An :class:`OverheadProbe` with both paths' QPS + p50/p95 latency.

    Raises:
        ValueError: if ``images`` is empty or ``timed_passes`` < 1.
    """
    if not images:
        raise ValueError("measure_overhead_probe: images must not be empty")
    if timed_passes < 1:
        raise ValueError(
            f"measure_overhead_probe: timed_passes must be >= 1; got {timed_passes}"
        )

    emb_qps, emb_p50, emb_p95 = _time_path(
        lambda: embedder.embed(images),
        batch=len(images),
        timed_passes=timed_passes,
        warmup_passes=warmup_passes,
    )
    attr_qps, attr_p50, attr_p95 = _time_path(
        lambda: embedder.embed_with_attributes(images),
        batch=len(images),
        timed_passes=timed_passes,
        warmup_passes=warmup_passes,
    )
    return OverheadProbe(
        device=embedder.device,
        embedding_only_qps=emb_qps,
        with_attributes_qps=attr_qps,
        embedding_only_p50_ms=emb_p50,
        with_attributes_p50_ms=attr_p50,
        embedding_only_p95_ms=emb_p95,
        with_attributes_p95_ms=attr_p95,
        category_top1=category_top1,
        colour_top1=colour_top1,
    )


def _time_path(
    call: Callable[[], object],
    *,
    batch: int,
    timed_passes: int,
    warmup_passes: int,
) -> tuple[float, float, float]:
    """Return ``(qps, p50_ms, p95_ms)`` for repeatedly calling ``call``.

    QPS is images-processed per wall-second over the timed passes; p50/p95 are
    per-pass latency percentiles. Warmup passes are run and discarded first.
    """
    for _ in range(max(0, warmup_passes)):
        call()
    latencies_ms: list[float] = []
    for _ in range(timed_passes):
        start = time.perf_counter()
        call()
        latencies_ms.append((time.perf_counter() - start) * 1000.0)
    latencies_ms.sort()
    total_s = sum(latencies_ms) / 1000.0
    qps = (timed_passes * batch) / total_s if total_s > 0 else 0.0
    p50 = latencies_ms[len(latencies_ms) // 2]
    p95 = latencies_ms[min(len(latencies_ms) - 1, int(len(latencies_ms) * 0.95))]
    return qps, p50, p95


@dataclass(frozen=True)
class ClassificationOverheadResult:
    """The measured overhead the heads add to ``/embed``, with accuracy alongside.

    ``overhead_pct`` is the throughput fraction lost when the heads run; the
    latency deltas are the added per-request milliseconds at p50/p95. The
    category/colour top-1 sit next to the overhead so the speed/accuracy trade is
    one document.
    """

    device: str
    overhead_pct: float
    embedding_only_qps: float
    with_attributes_qps: float
    latency_delta_p50_ms: float
    latency_delta_p95_ms: float
    category_top1: float
    colour_top1: float

    def to_dict(self) -> dict[str, float | str]:
        """A flat, JSON/TOML-serialisable view (overhead + accuracy together)."""
        return {
            "device": self.device,
            "overhead_pct": self.overhead_pct,
            "embedding_only_qps": self.embedding_only_qps,
            "with_attributes_qps": self.with_attributes_qps,
            "latency_delta_p50_ms": self.latency_delta_p50_ms,
            "latency_delta_p95_ms": self.latency_delta_p95_ms,
            "category_top1": self.category_top1,
            "colour_top1": self.colour_top1,
        }


def compute_classification_overhead(
    probe: OverheadProbe,
) -> ClassificationOverheadResult:
    """Compute the attribute-head overhead from an embedding-only-vs-+attributes probe.

    ``overhead_pct = (1 - with_attributes_qps / embedding_only_qps) * 100`` — the
    fraction of embedding-only throughput lost when the heads also run. A small
    negative value (the +attributes run measured marginally faster) is recorded
    honestly rather than clamped: it is the expected signature of a near-free
    co-output where the head matmul is lost in measurement noise.

    Args:
        probe: the injected measurement (see :class:`OverheadProbe`).

    Returns:
        A :class:`ClassificationOverheadResult` with the overhead %, the source
        QPS pair, the added p50/p95 latency, and the category/colour top-1.

    Raises:
        ValueError: if ``embedding_only_qps`` is non-positive (a failed
            measurement — divide-by-zero rather than a 0% overhead), or a top-1
            is outside ``[0, 1]`` (a malformed measurement).
    """
    if probe.embedding_only_qps <= 0.0:
        raise ValueError(
            "compute_classification_overhead: embedding_only_qps must be > 0 "
            f"(a measured throughput); got {probe.embedding_only_qps!r} — this is "
            "a failed measurement, not a 0% overhead"
        )
    for name, value in (
        ("category_top1", probe.category_top1),
        ("colour_top1", probe.colour_top1),
    ):
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                f"compute_classification_overhead: {name} must be in [0, 1]; "
                f"got {value!r}"
            )

    overhead_pct = (1.0 - probe.with_attributes_qps / probe.embedding_only_qps) * 100.0
    return ClassificationOverheadResult(
        device=probe.device,
        overhead_pct=overhead_pct,
        embedding_only_qps=probe.embedding_only_qps,
        with_attributes_qps=probe.with_attributes_qps,
        latency_delta_p50_ms=probe.with_attributes_p50_ms - probe.embedding_only_p50_ms,
        latency_delta_p95_ms=probe.with_attributes_p95_ms - probe.embedding_only_p95_ms,
        category_top1=probe.category_top1,
        colour_top1=probe.colour_top1,
    )


def write_overhead_report(
    path: Path,
    *,
    result: ClassificationOverheadResult,
    config_hash: str,
) -> Path:
    """Write the overhead + accuracy result to a committed TOML document.

    The artifact carries TR1 (the overhead %) and TR2 (category/colour top-1)
    together, plus the config-reproducibility hash, so a reviewer reads the
    speed/accuracy trade without rerunning the heavy real-model sweep. Mirrors
    the committed-artifact discipline of ``evals/attributes/story-020/
    accuracy-frontier.toml`` and ``frontier.json``.

    Args:
        path: destination ``.toml`` path (parents created if needed).
        result: the computed overhead/accuracy result.
        config_hash: reproducibility hash of the config the sweep ran under.

    Returns:
        The :class:`Path` written.
    """
    fields = result.to_dict()
    fields["config_hash"] = config_hash

    lines = [
        "# — classification (#068 attribute heads) as a",
        "# measured, first-class signal: the embedding-only-vs-+attributes /embed",
        "# overhead % (TR1) recorded next to category/colour top-1 (TR2).",
        "#",
        "# overhead_pct = (1 - with_attributes_qps / embedding_only_qps) * 100.",
        "# Both QPS numbers are measured at the MODEL (L1 raw /embed), independent",
        "# of the #089 per-turn embed cache. The heads ride the frozen 768-d",
        "# backbone features in one backbone pass, so the overhead is expected to",
        "# be small — the value below is the ACTUAL measured %.",
        "#",
        "# category_top1 is the #071-gated accuracy; colour_top1 is reported only",
        "# (description-derived, noisier — see baseline.toml colour_caveat).",
        "",
    ]
    for key in (
        "device",
        "config_hash",
        "overhead_pct",
        "embedding_only_qps",
        "with_attributes_qps",
        "latency_delta_p50_ms",
        "latency_delta_p95_ms",
        "category_top1",
        "colour_top1",
    ):
        lines.append(_toml_line(key, fields[key]))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _toml_line(key: str, value: float | str) -> str:
    """Render one ``key = value`` TOML line (string values quoted)."""
    if isinstance(value, str):
        return f'{key} = "{value}"'
    return f"{key} = {value}"
