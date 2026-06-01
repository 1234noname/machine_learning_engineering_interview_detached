""" QPS benchmark sweep runner.

Called by `just bench-qps`. Reads the sweep matrix from config/avsa.toml
[bench], then runs the appropriate measurement for the selected target.

Two targets are supported (select via --target):

  batcher (default)  -- Locust -> Rust batcher (:8081) -> Modal GPU.
                        Measures **system QPS**: the full network path from
                        the calling machine through GKE to Modal.
                        Uses BatcherUser singular contract: {"image_bytes": <b64>}.

                        User-path relevance: this path is hit whenever a shopper
                        user submits an image — image-only search (35% of traffic),
                        image+text queries (25%), and the opening image turn in
                        multi-turn sessions (fraction of 15%). Text-only queries
                        (25%) bypass this path entirely and use the CLIP text
                        encoder instead. In a conversational UX, real per-user
                        image embed rate is low (once per session, seconds apart);
                        the concurrency in these benchmarks models burst capacity
                        (many users simultaneously uploading), not steady-state
                        dialogue throughput.

  model              -- In-memory benchmark: no HTTP, no Locust, no batcher.
                        Modal: calls AvsaModel.bench_in_memory via the Modal
                        SDK (detected when --model-url starts with https://).
                        Local: loads VitEmbedder in-process and times embed().
                        Measures **GPU compute ceiling** — what the hardware
                        can do without network overhead.

Usage (called by just bench-qps):
    uv run python scripts/bench-qps.py \\
        --batcher-url http://localhost:8081 \\
        --target      batcher \\
        --output-dir  evals/qps/baseline \\
        --locustfile  locustfile.py \\
        --config      config/avsa.toml

    # Model in-memory (local):
    uv run python scripts/bench-qps.py --target model

    # Model in-memory (Modal SDK — set MODAL_APP_NAME or use https:// URL):
    AVSA_PROD_MODEL_URL=https://erinversfeldcodes--avsa-model-avsamodel-embed-http.modal.run \\
    uv run python scripts/bench-qps.py --target model --model-url $AVSA_PROD_MODEL_URL
"""

from __future__ import annotations

import argparse
import csv
import os
import socket
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _resolve_batcher_target(*, batcher_url: str) -> tuple[str, str, str]:
    """Resolve ``(url, locust_user_class, run_label)`` for the batcher target.

    Returns the Rust batcher URL, BatcherUser class, and label ``batcher``.
    Only called for ``--target batcher``; the model target uses the in-memory
    path and never calls Locust.
    """
    return batcher_url, "BatcherUser", "batcher"


def _read_config(config_path: Path) -> dict:  # type: ignore[type-arg]
    with config_path.open("rb") as f:
        return tomllib.load(f)


def _batcher_is_reachable(url: str) -> bool:
    parsed = urlparse(url)
    # For HTTPS endpoints (e.g. Modal) use an HTTP health-check rather than a
    # raw TCP socket — Modal only exposes port 443, not the fallback 8081.
    if parsed.scheme in ("https", "http"):
        import urllib.error
        import urllib.request

        # Try /healthz first (Modal), fall back to /health (batcher).
        for path in ("/healthz", "/health"):
            health_url = url.rstrip("/") + path
            try:
                req = urllib.request.Request(health_url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return bool(resp.status < 500)
            except urllib.error.HTTPError as e:
                # 4xx means the server is up; endpoint just doesn't exist here.
                if e.code < 500:
                    return True
            except Exception:
                continue
        return False
    host = parsed.hostname or "localhost"
    port = parsed.port or 8081
    try:
        s = socket.create_connection((host, port), timeout=5)
        s.close()
        return True
    except OSError:
        return False


def _parse_stats_row(csv_path: Path, target: str = "batcher") -> dict:  # type: ignore[type-arg]
    """Return the /embed row from a locust stats CSV, or the Aggregated row.

    For the model target the locust name is ``/embed [model_direct]``; for the
    batcher target it is simply ``/embed``.  We prefer an exact match but fall
    back to Aggregated so existing callers are unaffected.
    """
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))

    # Prefer target-specific row name.
    preferred_name = "/embed [model_direct]" if target == "model" else "/embed"
    for row in rows:
        if row.get("Name") == preferred_name:
            return _coerce_numerics(row)
    # Fall back: any /embed row (e.g. realistic embed variant).
    for row in rows:
        name = row.get("Name", "")
        if "/embed" in name and name != "Aggregated":
            return _coerce_numerics(row)
    for row in rows:
        if row.get("Name") == "Aggregated":
            return _coerce_numerics(row)
    # Return last row if nothing matches.
    return _coerce_numerics(rows[-1]) if rows else {}


def _aggregate_stats_rows(rows: list[dict]) -> tuple[dict, dict]:  # type: ignore[type-arg]
    """Median-aggregate per-run locust stats rows for one (bs, c) point.

    Returns ``(aggregated_row, runs_info)``:

    - ``aggregated_row``: copy of the first run's stats with the four QPS /
      latency fields (``Requests/s``, ``Median Response Time``, ``95%``,
      ``99%``) replaced by the median across runs. Non-numeric fields
      (``Name``, etc.) take the first run's value.
    - ``runs_info``: per-run arrays + range for downstream readers that want
      to see the actual variance rather than only the median point estimate.
    """
    import statistics

    def _vals(key: str) -> list[float]:
        out: list[float] = []
        for r in rows:
            v = r.get(key)
            if v is None or v in ("", "N/A"):
                continue
            try:
                out.append(float(v))
            except (ValueError, TypeError):
                continue
        return out

    qps_vals = _vals("Requests/s")
    p50_vals = _vals("Median Response Time")
    p95_vals = _vals("95%")
    p99_vals = _vals("99%")

    agg = dict(rows[0]) if rows else {}
    if qps_vals:
        agg["Requests/s"] = statistics.median(qps_vals)
    if p50_vals:
        agg["Median Response Time"] = statistics.median(p50_vals)
    if p95_vals:
        agg["95%"] = statistics.median(p95_vals)
    if p99_vals:
        agg["99%"] = statistics.median(p99_vals)

    runs_info: dict = {  # type: ignore[type-arg]
        "n_measured": len(rows),
        "qps": qps_vals,
        "p50_ms": p50_vals,
        "p95_ms": p95_vals,
        "p99_ms": p99_vals,
        "qps_min": min(qps_vals) if qps_vals else 0.0,
        "qps_max": max(qps_vals) if qps_vals else 0.0,
    }
    return agg, runs_info


def _aggregate_probe_results(results: list[dict]) -> tuple[dict, dict]:  # type: ignore[type-arg]
    """Median-aggregate per-run in-process probe results for one batch_size.

    Mirrors :func:`_aggregate_stats_rows` but for the in-process probe dict
    shape (``qps`` / ``p50_ms`` / ``p95_ms`` keys, no Locust fields).
    """
    import statistics

    qps_vals = [float(r["qps"]) for r in results]
    p50_vals = [float(r["p50_ms"]) for r in results]
    p95_vals = [float(r["p95_ms"]) for r in results]

    agg = {
        "qps": statistics.median(qps_vals),
        "p50_ms": statistics.median(p50_vals),
        "p95_ms": statistics.median(p95_vals),
    }
    runs_info: dict = {  # type: ignore[type-arg]
        "n_measured": len(results),
        "qps": qps_vals,
        "p50_ms": p50_vals,
        "p95_ms": p95_vals,
        "qps_min": min(qps_vals) if qps_vals else 0.0,
        "qps_max": max(qps_vals) if qps_vals else 0.0,
    }
    return agg, runs_info


def _coerce_numerics(row: dict) -> dict:  # type: ignore[type-arg]
    numeric = {
        "Request Count",
        "Failure Count",
        "Median Response Time",
        "Average Response Time",
        "Min Response Time",
        "Max Response Time",
        "Requests/s",
        "Failures/s",
        "50%",
        "66%",
        "75%",
        "80%",
        "90%",
        "95%",
        "98%",
        "99%",
        "99.9%",
        "99.99%",
        "100%",
    }
    out = dict(row)
    for col in numeric:
        if col in out and out[col] not in (None, "", "N/A"):
            try:
                out[col] = float(out[col])
            except ValueError:
                out[col] = 0.0
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=" QPS benchmark sweep")
    parser.add_argument("--batcher-url", default="http://localhost:8081")
    parser.add_argument(
        "--model-url",
        default="http://localhost:8090",
        help="URL of the ViT model service (used when --target=model).",
    )
    parser.add_argument(
        "--target",
        choices=["batcher", "model"],
        default="batcher",
        help=(
            "Benchmark target. "
            "'batcher' (default): Locust -> Rust batcher -> Modal GPU — "
            "measures full system QPS including network. "
            "'model': in-memory probe — Modal SDK (https:// URL) or local "
            "in-process — measures GPU compute ceiling with no HTTP overhead."
        ),
    )
    parser.add_argument("--output-dir", default="evals/qps/baseline")
    parser.add_argument("--locustfile", default="locustfile.py")
    parser.add_argument("--config", default="config/avsa.toml")
    parser.add_argument(
        "--bench-section",
        default="",
        help=(
            "Dotted sub-section of [bench] to overlay for sweep params "
            "(e.g. 'prod' reads [bench.prod] and merges it over [bench]). "
            "Values in the sub-section override the parent; absent keys fall back to [bench]."
        ),
    )
    parser.add_argument(
        "--with-recall",
        action="store_true",
        default=False,
        help=(
            "After the QPS sweep, run recall@5 against the seeded catalog DB "
            "(image + text modalities) and print the results. "
            "Reads DATABASE_URL env var or falls back to [db].url in avsa.toml."
        ),
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = _read_config(config_path)
    bench_base = cfg["bench"]
    if args.bench_section:
        bench_override = bench_base.get(args.bench_section, {})
        bench = {**bench_base, **bench_override}
    else:
        bench = bench_base
    batcher = cfg["batcher"]

    batch_sizes: list[int] = bench["batch_sizes"]
    if _env_bs := os.environ.get("AVSA_BENCH_BATCH_SIZES"):
        batch_sizes = [int(x) for x in _env_bs.split(",")]

    target: str = args.target
    output_dir = Path(args.output_dir)

    # Ensure repo root is on sys.path so results_writer is importable.
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from evals.qps.optimization.results_writer import (
        ConfigHash,
        build_result,
        build_result_from_probe,
        collect_environment,
        read_gpu_snapshot,
        write_result,
    )

    # Compute config hash (bench + batcher sections).
    config_hash = ConfigHash.from_dict({"bench": bench, "batcher": batcher})

    # --- model target: in-memory probe (no Locust, no HTTP) ---
    if target == "model":
        # Same n_runs / warmup_passes precedence as the batcher path so a
        # comparison series uses identical aggregation on both surfaces.
        model_n_runs = int(bench.get("n_runs", 1))
        if _env_runs := os.environ.get("AVSA_BENCH_RUNS"):
            model_n_runs = int(_env_runs)
        model_warmup = int(bench.get("warmup_passes", 0))
        if _env_warmup := os.environ.get("AVSA_BENCH_WARMUP"):
            model_warmup = int(_env_warmup)
        return _run_model_in_memory(
            model_url=args.model_url,
            batch_sizes=batch_sizes,
            n_passes=int(bench.get("n_passes", 50)),
            n_runs=model_n_runs,
            warmup_passes=model_warmup,
            config_hash=config_hash,
            output_dir=output_dir,
            with_recall=args.with_recall,
            cfg=cfg,
            build_result_from_probe=build_result_from_probe,
            collect_environment=collect_environment,
            read_gpu_snapshot=read_gpu_snapshot,
            write_result=write_result,
        )

    # --- batcher target: Locust-based system QPS sweep ---
    concurrency_levels: list[int] = bench["concurrency_levels"]
    if _env_conc := os.environ.get("AVSA_BENCH_CONCURRENCY"):
        concurrency_levels = [int(x) for x in _env_conc.split(",")]
    run_duration_s: int = bench["run_duration_s"]
    spawn_rate: int = bench["spawn_rate"]

    # Measurement-rigor: warmup_passes runs are discarded, then n_runs runs
    # are collected and median-aggregated per (bs, c) point. Both knobs live
    # in config/avsa.base.toml [bench] (overridable per-profile in
    # [bench.prod] / [bench.model]); the env-var pair below is the same kind
    # of inner-loop escape hatch as AVSA_BENCH_BATCH_SIZES / _CONCURRENCY.
    n_runs: int = int(bench.get("n_runs", 1))
    if _env_runs := os.environ.get("AVSA_BENCH_RUNS"):
        n_runs = int(_env_runs)
    warmup_passes: int = int(bench.get("warmup_passes", 0))
    if _env_warmup := os.environ.get("AVSA_BENCH_WARMUP"):
        warmup_passes = int(_env_warmup)

    target_url, locust_user_class, run_label = _resolve_batcher_target(
        batcher_url=args.batcher_url
    )

    if not _batcher_is_reachable(target_url):
        print(
            f"ERROR: batcher not reachable at {target_url}",
            file=sys.stderr,
        )
        print(
            "       Start the batcher with: just batcher-dev",
            file=sys.stderr,
        )
        return 1

    print(f"==>  bench-qps sweep  target={target} ({run_label})")
    print(f"    url:         {target_url}")
    print(f"    user_class:  {locust_user_class}")
    print(f"    batch_sizes: {batch_sizes}")
    print(f"    concurrency: {concurrency_levels}")
    print(f"    duration:    {run_duration_s}s per point")
    print(
        f"    runs/point:  {n_runs} measured + {warmup_passes} warmup (median reported)"
    )
    print(f"    config hash: {config_hash}")
    print()

    output_dir.mkdir(parents=True, exist_ok=True)
    locustfile = Path(args.locustfile).resolve()

    def _run_locust_once(
        run_id_inner: str, *, batch_size: int, concurrency: int, tmp: Path
    ) -> dict | None:  # type: ignore[type-arg]
        """Run one locust subprocess and return the parsed /embed stats row.

        Returns None and prints an error if locust never produced a CSV; the
        caller decides whether to abort or continue.
        """
        csv_prefix = str(tmp / run_id_inner)
        locust_cmd = [
            sys.executable,
            "-m",
            "locust",
            "-f",
            str(locustfile),
            "--headless",
            "-u",
            str(concurrency),
            "-r",
            str(spawn_rate),
            "-t",
            f"{run_duration_s}s",
            "--host",
            target_url,
            "--csv",
            csv_prefix,
            "--csv-full-history",
            "--only-summary",
            locust_user_class,
        ]
        env_locust = {
            **os.environ,
            "AVSA_BATCH_SIZE": str(batch_size),
            "AVSA_BATCHER_URL": target_url,
        }
        proc = subprocess.run(
            locust_cmd, env=env_locust, capture_output=True, text=True
        )
        stats_csv = tmp / f"{run_id_inner}_stats.csv"
        if proc.returncode != 0:
            if not stats_csv.exists():
                print(
                    f"    locust exited {proc.returncode} and no CSV written",
                    file=sys.stderr,
                )
                print(proc.stderr[-2000:], file=sys.stderr)
                return None
            print(
                f"    locust exited {proc.returncode} (partial failures) — CSV exists, continuing",
                file=sys.stderr,
            )
        return _parse_stats_row(stats_csv, target=target)

    with tempfile.TemporaryDirectory(prefix="avsa-bench-qps-") as tmp_dir:
        tmp = Path(tmp_dir)

        for batch_size in batch_sizes:
            for concurrency in concurrency_levels:
                ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
                run_id = f"bench-{run_label}-bs{batch_size}-c{concurrency}-{ts}"
                print(f"==> bs={batch_size} c={concurrency} run_id={run_id}")

                # Warmup passes — results discarded.
                for w in range(warmup_passes):
                    warm_id = f"{run_id}-w{w + 1}"
                    print(f"    [warmup {w + 1}/{warmup_passes}]")
                    row = _run_locust_once(
                        warm_id, batch_size=batch_size, concurrency=concurrency, tmp=tmp
                    )
                    if row is None:
                        return 1

                # Measured passes — collected and aggregated.
                measured_rows: list[dict] = []  # type: ignore[type-arg]
                for r in range(n_runs):
                    meas_id = f"{run_id}-r{r + 1}"
                    row = _run_locust_once(
                        meas_id, batch_size=batch_size, concurrency=concurrency, tmp=tmp
                    )
                    if row is None:
                        return 1
                    measured_rows.append(row)
                    qps_r = float(row.get("Requests/s") or 0)
                    p50_r = float(row.get("Median Response Time") or 0)
                    print(
                        f"    [run {r + 1}/{n_runs}] qps={qps_r:.1f}  p50={p50_r:.0f}ms"
                    )

                agg_row, runs_info = _aggregate_stats_rows(measured_rows)
                runs_info["n_warmup"] = warmup_passes

                gpu_snap = read_gpu_snapshot()
                env_meta = collect_environment(recorded=False)
                env_meta["bench_target"] = target

                result_doc = build_result(
                    run_id=run_id,
                    config_hash=config_hash,
                    environment=env_meta,
                    batch_size=batch_size,
                    concurrency=concurrency,
                    stats_row=agg_row,
                    gpu_snapshot=gpu_snap,
                    runs=runs_info,
                )
                out_path = write_result(result_doc, output_dir=output_dir)
                median_qps = float(agg_row.get("Requests/s") or 0)
                print(
                    f"    median qps={median_qps:.1f}  "
                    f"(range {runs_info['qps_min']:.1f}-{runs_info['qps_max']:.1f})"
                )
                print(f"    result -> {out_path}")

    print()
    print(f"==> bench-qps sweep complete  target={target}  Results in {output_dir}/")
    print(f"==> config hash: {config_hash}")

    if args.with_recall:
        _run_recall(cfg=cfg)

    return 0


def _run_model_in_memory(
    *,
    model_url: str,
    batch_sizes: list[int],
    n_passes: int,
    n_runs: int,
    warmup_passes: int,
    config_hash: str,
    output_dir: Path,
    with_recall: bool,
    cfg: dict[str, Any],
    build_result_from_probe: Callable[..., Any],
    collect_environment: Callable[..., Any],
    read_gpu_snapshot: Callable[..., Any],
    write_result: Callable[..., Any],
) -> int:
    """Run the model-direct in-memory benchmark (no Locust, no HTTP).

    Modal path  — model_url starts with https://: calls AvsaModel.bench_in_memory
                  via the Modal SDK using MODAL_APP_NAME env (default avsa-model).
    Local path  — model_url is a local http:// address: imports VitEmbedder
                  in-process and times embed() calls directly.
    """
    use_modal = model_url.startswith("https://")
    modal_app_name = os.environ.get("MODAL_APP_NAME", "avsa-model")

    print("==>  bench-qps  target=model (in-memory)")
    if use_modal:
        print(f"    mode:        Modal SDK  (app={modal_app_name})")
    else:
        print(f"    mode:        local in-process  ({model_url})")
    print(f"    batch_sizes: {batch_sizes}")
    print(f"    n_passes:    {n_passes} per point")
    print(
        f"    runs/point:  {n_runs} measured + {warmup_passes} warmup (median reported)"
    )
    print(f"    config hash: {config_hash}")
    print()

    if use_modal:
        try:
            import modal as _modal
        except ImportError:
            print(
                "ERROR: modal SDK not installed — run: uv pip install modal",
                file=sys.stderr,
            )
            return 1
        _model_cls = _modal.Cls.from_name(modal_app_name, "AvsaModel")
        _model_instance = _model_cls()

        def probe(batch_size: int) -> dict:  # type: ignore[type-arg]
            return _model_instance.bench_in_memory.remote(  # type: ignore[no-any-return]
                batch_size=batch_size, n_passes=n_passes
            )
    else:
        probe = _make_local_in_memory_probe(n_passes=n_passes)

    output_dir.mkdir(parents=True, exist_ok=True)
    env_meta = collect_environment(recorded=False)
    env_meta["bench_target"] = "model"

    for batch_size in batch_sizes:
        ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
        run_id = f"bench-model-in-memory-bs{batch_size}-{ts}"
        print(f"==> bs={batch_size}  run_id={run_id}")

        # Warmup passes — results discarded.
        for w in range(warmup_passes):
            print(f"    [warmup {w + 1}/{warmup_passes}]")
            probe(batch_size)

        # Measured passes — collected and aggregated.
        probe_results: list[dict] = []  # type: ignore[type-arg]
        for r in range(n_runs):
            pr = probe(batch_size)
            probe_results.append(pr)
            print(
                f"    [run {r + 1}/{n_runs}] qps={pr['qps']:.1f}  "
                f"p50={pr['p50_ms']:.0f}ms  p95={pr['p95_ms']:.0f}ms"
            )

        agg, runs_info = _aggregate_probe_results(probe_results)
        runs_info["n_warmup"] = warmup_passes

        gpu_snap = read_gpu_snapshot()
        result_doc = build_result_from_probe(
            run_id=run_id,
            config_hash=config_hash,
            environment=env_meta,
            batch_size=batch_size,
            n_passes=n_passes,
            qps=agg["qps"],
            p50_ms=agg["p50_ms"],
            p95_ms=agg["p95_ms"],
            gpu_snapshot=gpu_snap,
            runs=runs_info,
        )
        out_path = write_result(result_doc, output_dir=output_dir)
        print(
            f"    median qps={agg['qps']:.1f}  "
            f"(range {runs_info['qps_min']:.1f}-{runs_info['qps_max']:.1f})  "
            f"p50={agg['p50_ms']:.0f}ms  p95={agg['p95_ms']:.0f}ms"
        )
        print(f"    result -> {out_path}")

    print()
    print(f"==> bench-qps sweep complete  target=model  Results in {output_dir}/")
    print(f"==> config hash: {config_hash}")

    if with_recall:
        _run_recall(cfg=cfg)

    return 0


def _make_local_in_memory_probe(*, n_passes: int) -> Callable[..., dict[str, float]]:
    """Return a probe callable that loads VitEmbedder in-process and times embed().

    The embedder is loaded once and reused across all batch_size calls.
    Requires the model extras (torch, transformers, Pillow) to be installed.
    """
    import io
    import time

    repo_root = Path(__file__).parent.parent
    sys.path.insert(0, str(repo_root / "apps" / "model" / "src"))

    try:
        from avsa_model.vit import VitEmbedder  # type: ignore[import-not-found]
        from PIL import Image as PILImage
    except ImportError as exc:
        print(
            f"ERROR: model deps not importable for local in-memory bench: {exc}\n"
            "       Install with: uv pip install -e apps/model[model]",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    embedder = VitEmbedder()

    def probe(batch_size: int) -> dict:  # type: ignore[type-arg]
        images: list[bytes] = []
        for i in range(batch_size):
            buf = io.BytesIO()
            PILImage.new("RGB", (224, 224), color=(i * 10 % 256, 40, 80)).save(
                buf, format="JPEG"
            )
            images.append(buf.getvalue())

        for _ in range(5):
            embedder.embed(images)

        latencies: list[float] = []
        for _ in range(n_passes):
            t0 = time.perf_counter()
            embedder.embed(images)
            latencies.append((time.perf_counter() - t0) * 1000.0)

        latencies.sort()
        total_s = sum(latencies) / 1000.0
        qps = (n_passes * batch_size) / total_s if total_s > 0 else 0.0
        return {
            "qps": qps,
            "p50_ms": latencies[n_passes // 2],
            "p95_ms": latencies[min(n_passes - 1, int(n_passes * 0.95))],
        }

    return probe


def _run_recall(*, cfg: dict) -> None:  # type: ignore[type-arg]
    """Run recall@5 for image and text modalities; print results."""
    try:
        import psycopg

        from evals.retrieval.run_recall_at_k import run_recall_at_k
    except ImportError as exc:
        print(f"\n==> recall@5 skipped — deps not importable: {exc}", file=sys.stderr)
        return

    db_url = os.environ.get("DATABASE_URL") or cfg.get("db", {}).get("url", "")
    if not db_url:
        print(
            "\n==> recall@5 skipped — no DATABASE_URL or [db].url in config",
            file=sys.stderr,
        )
        return

    repo_root = Path(__file__).parent.parent
    fixtures_dir = repo_root / "evals" / "retrieval"
    stories = sorted(p.parent.name for p in fixtures_dir.glob("*/fixtures.jsonl"))

    print("\n==> recall@5")
    try:
        conn = psycopg.connect(db_url)
    except Exception as exc:
        print(f"    DB connect failed: {exc}", file=sys.stderr)
        return

    # Detect whether the catalog stores local Fashion200k paths or original
    # Lyst CDN URLs (https://cdn*.lystit.com/...). The seeder contract
    # (/images/fashion200k/images/{id}.jpg) is the local-dev default; prod was
    # seeded from the source dataset preserving the original CDN URLs.
    item_id_to_url: Callable[[str], str] | None = None
    url_to_item_id: Callable[[str], str] | None = None
    with conn.cursor() as _cur:
        _cur.execute("SELECT image_url FROM catalog.products LIMIT 1")
        _sample = _cur.fetchone()
    if _sample and _sample[0].startswith("https://"):
        # CDN-URL catalog: build item_id <-> url mapping from metadata.jsonl.
        metadata_path = repo_root / "data" / "fashion200k" / "metadata.jsonl"
        if not metadata_path.exists():
            print(
                "    recall@5 skipped — CDN-URL catalog but "
                "data/fashion200k/metadata.jsonl not found",
                file=sys.stderr,
            )
        else:
            import json as _json

            _id_to_url: dict[str, str] = {}
            _url_to_id: dict[str, str] = {}
            with open(metadata_path, encoding="utf-8") as _mf:
                for _line in _mf:
                    _line = _line.strip()
                    if not _line:
                        continue
                    _obj = _json.loads(_line)
                    _id_to_url[_obj["id"]] = _obj["source_url"]
                    _url_to_id[_obj["source_url"]] = _obj["id"]
            item_id_to_url = _id_to_url.__getitem__
            url_to_item_id = _url_to_id.__getitem__
            print(
                f"    URL resolver: CDN ({len(_id_to_url):,} items from metadata.jsonl)"
            )

    with conn:
        for story in stories:
            for modality in ("image", "text"):
                fixtures_path = fixtures_dir / story / "fixtures.jsonl"
                try:
                    result = run_recall_at_k(
                        conn,
                        fixtures_path=fixtures_path,
                        modality=modality,
                        k=5,
                        item_id_to_url=item_id_to_url,
                        url_to_item_id=url_to_item_id,
                    )
                    print(
                        f"    {story} ({modality}): "
                        f"recall@5={result.mean_recall_at_5:.4f}  "
                        f"n={result.num_queries}"
                    )
                except Exception as exc:
                    print(f"    {story} ({modality}): ERROR — {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
