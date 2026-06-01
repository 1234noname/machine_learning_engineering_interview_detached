"""QPS benchmark results writer.

Reads a locust stats dict (one row from the locust CSV) and a GPU
utilisation snapshot, then writes a machine-readable JSON result to
`evals/qps/baseline/<run_id>.json`.

Schema (ResultSchema):
    {
        "run_id": str,
        "timestamp": ISO-8601 string,
        "config_hash": hex string — SHA-256 of canonical config JSON,
        "environment": {
            "cpu": str,
            "gpu": str | null,
            "image_digest": str,   # placeholder until container CI lands
            "recorded": bool,      # true when this is a recorded/stub baseline
        },
        "matrix_point": {"batch_size": int, "concurrency": int},
        "metrics": {
            "qps": float,
            "p50_ms": float,
            "p95_ms": float,
            "p99_ms": float,
            "gpu_util_pct": int | null,
        },
    }
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

# LocustStatsRow uses locust's actual CSV column names (spaces, % chars).
# Using a plain type alias (dict[str, Any]) avoids TypedDict identifier
# constraints while still documenting the expected shape.
LocustStatsRow = dict[str, Any]

GpuSnapshot = dict[str, Any]  # {"gpu_util_pct": int | None}

MatrixPoint = dict[str, Any]  # {"batch_size": int, "concurrency": int}

Metrics = dict[str, Any]  # {"qps", "p50_ms", "p95_ms", "p99_ms", "gpu_util_pct"}

ResultSchema = dict[str, Any]


# ---------------------------------------------------------------------------
# ConfigHash
# ---------------------------------------------------------------------------


class ConfigHash:
    """Derive a reproducible hex SHA-256 from a config dict.

    Canonical form: JSON with sorted keys, no extra whitespace.
    Same config → same hash across Python versions and platforms.
    """

    @staticmethod
    def from_dict(cfg: dict[str, Any]) -> str:
        canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# GPU snapshot
# ---------------------------------------------------------------------------


def read_gpu_snapshot() -> GpuSnapshot:
    """Query GPU utilisation via nvidia-smi.

    Degrades gracefully to ``{"gpu_util_pct": None}`` when:
    - nvidia-smi is not on PATH (FileNotFoundError)
    - nvidia-smi exits with a non-zero return code
    - stdout cannot be parsed as an integer
    """
    try:
        _smi_args = [
            "nvidia-smi",
            "--query-gpu=utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
        result = subprocess.run(
            _smi_args,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError):
        return {"gpu_util_pct": None}

    if result.returncode != 0:
        return {"gpu_util_pct": None}

    try:
        util_pct = int(result.stdout.strip().splitlines()[0].strip())
    except (ValueError, IndexError):
        return {"gpu_util_pct": None}

    return {"gpu_util_pct": util_pct}


# ---------------------------------------------------------------------------
# Build result
# ---------------------------------------------------------------------------


def build_result_from_probe(
    *,
    run_id: str,
    config_hash: str,
    environment: dict[str, Any],
    batch_size: int,
    n_passes: int,
    qps: float,
    p50_ms: float,
    p95_ms: float,
    gpu_snapshot: GpuSnapshot,
    runs: dict[str, Any] | None = None,
) -> ResultSchema:
    """Build a ResultSchema from in-memory probe data (no Locust CSV).

    Used by the model-in-memory bench path (Modal SDK or local in-process),
    where QPS and latencies come directly from the probe rather than a locust
    stats CSV. Uses ``n_passes`` instead of ``concurrency`` in ``matrix_point``
    since there is no concurrency dimension in a sequential in-process timing.

    ``runs`` (optional) carries the per-run arrays + warmup metadata for
    multi-run measurements; the headline ``qps``/``p50_ms``/``p95_ms`` should
    already be the aggregated (median) values across those runs.
    """
    timestamp = datetime.now(tz=UTC).isoformat()
    metrics: Metrics = {
        "qps": qps,
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "p99_ms": None,
        "gpu_util_pct": gpu_snapshot.get("gpu_util_pct"),
    }
    doc: ResultSchema = {
        "run_id": run_id,
        "timestamp": timestamp,
        "config_hash": config_hash,
        "environment": environment,
        "matrix_point": {
            "batch_size": batch_size,
            "n_passes": n_passes,
        },
        "metrics": metrics,
    }
    if runs is not None:
        doc["runs"] = runs
    return doc


def build_result(
    *,
    run_id: str,
    config_hash: str,
    environment: dict[str, Any],
    batch_size: int,
    concurrency: int,
    stats_row: LocustStatsRow,
    gpu_snapshot: GpuSnapshot,
    runs: dict[str, Any] | None = None,
) -> ResultSchema:
    """Assemble a ResultSchema dict from a locust stats row and a GPU snapshot.

    Args:
        run_id: Unique identifier for this benchmark run.
        config_hash: SHA-256 hex digest of the relevant config section.
        environment: Metadata dict (cpu, gpu, image_digest, recorded).
        batch_size: Batch size used in this matrix point.
        concurrency: Concurrency level used in this matrix point.
        stats_row: One row from the locust stats CSV (as a dict).
            Expected keys: ``"Median Response Time"``, ``"95%"``, ``"99%"``,
            ``"Requests/s"``.
        gpu_snapshot: Result of :func:`read_gpu_snapshot`.

    Returns:
        A fully-populated ResultSchema dict ready for JSON serialisation.
    """
    timestamp = datetime.now(tz=UTC).isoformat()

    def _stat(key: str) -> float:
        v = stats_row.get(key, 0)
        if v in (None, "", "N/A"):
            return 0.0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0

    metrics: Metrics = {
        "qps": _stat("Requests/s"),
        "p50_ms": _stat("Median Response Time"),
        "p95_ms": _stat("95%"),
        "p99_ms": _stat("99%"),
        "gpu_util_pct": gpu_snapshot.get("gpu_util_pct"),
    }

    doc: ResultSchema = {
        "run_id": run_id,
        "timestamp": timestamp,
        "config_hash": config_hash,
        "environment": environment,
        "matrix_point": {
            "batch_size": batch_size,
            "concurrency": concurrency,
        },
        "metrics": metrics,
    }
    if runs is not None:
        doc["runs"] = runs
    return doc


# ---------------------------------------------------------------------------
# Write result to disk
# ---------------------------------------------------------------------------


def write_result(result: ResultSchema, *, output_dir: Path) -> Path:
    """Serialise *result* to ``<output_dir>/<run_id>.json``.

    Creates *output_dir* (and any missing parents) if it does not exist.

    Returns:
        The :class:`Path` of the written file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id: str = result["run_id"]
    out_path = output_dir / f"{run_id}.json"
    with out_path.open("w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    return out_path


# ---------------------------------------------------------------------------
# Environment metadata
# ---------------------------------------------------------------------------


def collect_environment(
    *,
    image_digest: str = "placeholder-no-ci-image-yet",
    recorded: bool = False,
) -> dict[str, Any]:
    """Collect host environment metadata for embedding in a result.

    Args:
        image_digest: Container image digest (placeholder until container CI).
        recorded: Set to True for stub/recorded baselines.

    Returns:
        A dict with ``cpu``, ``gpu``, ``image_digest``, ``python``,
        ``recorded``, and ``platform`` keys.
    """
    gpu_snap = read_gpu_snapshot()
    gpu_info: str | None
    if gpu_snap["gpu_util_pct"] is not None:
        # nvidia-smi is available — capture the GPU name too.
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            gpu_info = r.stdout.strip() if r.returncode == 0 else "unknown-gpu"
        except (FileNotFoundError, OSError):
            gpu_info = None
    else:
        gpu_info = None

    return {
        "cpu": platform.processor() or platform.machine(),
        "gpu": gpu_info,
        "image_digest": image_digest,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "recorded": recorded,
    }
