"""Unit tests for the model-vs-batcher target selection in bench-qps.py.

``scripts/bench-qps.py`` can drive the QPS sweep against **either** target:

  batcher (default) -> POST to the Rust batcher :8081 via ``BatcherUser``
                       (system QPS, singular ``{"image_bytes": <b64>}`` contract).
  model             -> POST directly to the ViT model :8090 via
                       ``ModelUser`` (raw model QPS, plural
                       ``{"images": [<b64>]}`` contract).

These tests pin the per-target stats-row selection (the model sweep's locust row
is named ``/embed [model_direct]``) without importing locust or saturating a live
service. The script is hyphenated so it is loaded via importlib (the repo
convention for ``scripts/*.py``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BENCH_QPS = _REPO_ROOT / "scripts" / "bench-qps.py"


def _load_bench_qps() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_qps", _BENCH_QPS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_bench_qps = _load_bench_qps()


class TestParseStatsRowByTarget:
    """The model sweep's locust row is named ``/embed [model_direct]``."""

    def test_model_row_preferred_for_model_target(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "stats.csv"
        csv_path.write_text(
            "Name,Request Count,Failure Count,Median Response Time,"
            "Average Response Time,Min Response Time,Max Response Time,"
            "Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,"
            "99.99%,100%\n"
            "/embed,100,0,80,80,70,90,40.0,0,80,80,80,80,85,90,95,99,110,"
            "120,130\n"
            "/embed [model_direct],100,0,90,90,80,100,45.0,0,90,90,90,90,"
            "95,100,105,110,120,130,140\n"
            "Aggregated,200,0,85,85,70,100,42.0,0,85,85,85,85,90,95,100,"
            "105,115,125,135\n"
        )
        row = _bench_qps._parse_stats_row(csv_path, target="model")
        assert row["Name"] == "/embed [model_direct]"
        assert row["Requests/s"] == 45.0

    def test_batcher_row_preferred_for_batcher_target(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "stats.csv"
        csv_path.write_text(
            "Name,Request Count,Failure Count,Median Response Time,"
            "Average Response Time,Min Response Time,Max Response Time,"
            "Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,"
            "99.99%,100%\n"
            "/embed,100,0,80,80,70,90,40.0,0,80,80,80,80,85,90,95,99,110,"
            "120,130\n"
            "Aggregated,100,0,80,80,70,90,40.0,0,80,80,80,80,85,90,95,99,"
            "110,120,130\n"
        )
        row = _bench_qps._parse_stats_row(csv_path, target="batcher")
        assert row["Name"] == "/embed"
        assert row["Requests/s"] == 40.0
