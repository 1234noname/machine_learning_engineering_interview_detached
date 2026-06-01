"""CI-safe tests for  bench-prod config and script.

Validates:
  1. [bench.prod] section exists in config/avsa.toml and its values override [bench].
  2. bench-prod.sh exits non-zero when AVSA_PROD_BATCHER_URL is unset.

No network required — these tests are fully offline.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG = _REPO_ROOT / "config" / "avsa.toml"
_BENCH_PROD_SH = _REPO_ROOT / "scripts" / "bench-prod.sh"


class TestBenchProdConfig:
    def test_bench_prod_section_exists(self) -> None:
        with _CONFIG.open("rb") as f:
            cfg = tomllib.load(f)
        assert "prod" in cfg["bench"], "[bench.prod] section missing from avsa.toml"

    def test_bench_prod_concurrency_wider_than_default(self) -> None:
        with _CONFIG.open("rb") as f:
            cfg = tomllib.load(f)
        bench_prod = cfg["bench"]["prod"]
        bench_base = {k: v for k, v in cfg["bench"].items() if not isinstance(v, dict)}
        assert "concurrency_levels" in bench_prod, (
            "[bench.prod] must declare concurrency_levels"
        )
        prod_max = max(bench_prod["concurrency_levels"])
        base_max = max(bench_base["concurrency_levels"])
        assert prod_max > base_max, (
            "[bench.prod] max concurrency must exceed [bench] max "
            "to push GPU to saturation"
        )

    def test_bench_prod_run_duration_longer_than_default(self) -> None:
        with _CONFIG.open("rb") as f:
            cfg = tomllib.load(f)
        bench_prod = cfg["bench"]["prod"]
        bench_base = {k: v for k, v in cfg["bench"].items() if not isinstance(v, dict)}
        assert bench_prod["run_duration_s"] >= bench_base["run_duration_s"], (
            "[bench.prod] run_duration_s should be >= [bench] run_duration_s"
        )

    def test_bench_prod_merge_overrides_concurrency(self) -> None:
        """Simulate the --bench-section prod merge logic from bench-qps.py."""
        with _CONFIG.open("rb") as f:
            cfg = tomllib.load(f)
        bench_base = cfg["bench"]
        bench_override = bench_base.get("prod", {})
        merged = {**bench_base, **bench_override}
        assert merged["concurrency_levels"] == bench_override["concurrency_levels"]

    def test_bench_prod_batch_sizes_is_single_element(self) -> None:
        """[bench.prod] batch_sizes must be [1].

        Prod sweep sends 1 image/request (the real-world call pattern against
        the Rust batcher); the batcher's max_batch_size knob — not locust's
        batch_sizes param — is what drives batching behaviour.  Sweeping
        multiple batch_sizes values would just add redundant measurement.
        """
        with _CONFIG.open("rb") as f:
            cfg = tomllib.load(f)
        bench_prod = cfg["bench"]["prod"]
        assert bench_prod.get("batch_sizes") == [1], (
            "[bench.prod] batch_sizes must be [1] (single-image prod contract)"
        )


class TestBenchProdScript:
    def test_exits_nonzero_without_url(self) -> None:
        """bench-prod.sh must fail fast when AVSA_PROD_BATCHER_URL is unset."""
        result = subprocess.run(
            ["bash", str(_BENCH_PROD_SH)],
            capture_output=True,
            text=True,
            env={},
        )
        assert result.returncode != 0, "bench-prod.sh must exit non-zero if URL unset"
        assert "AVSA_PROD_BATCHER_URL" in result.stderr, (
            "error message must name the required env var"
        )
