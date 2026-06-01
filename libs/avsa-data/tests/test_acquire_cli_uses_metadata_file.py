"""Failing tests for  revision cycle 1 — CLI metadata pathway (Finding 3).

The CLI (scripts/acquire-fashion200k.py) MUST:

- Require ``--metadata-file`` to exist; fail loudly otherwise (no synthetic
  universe fallback).
- Build the per-item URL from each metadata row's ``source_url`` by default.
- Respect ``--source-url-template`` ONLY as an override for the row URL
  (operator-supplied local mirror).

The test imports the CLI module dynamically (it's a script under
``scripts/``, not under the ``avsa_api`` package) and drives ``_run`` directly
with a constructed ``argparse.Namespace`` to avoid the cost of a subprocess
per test. The "missing-file" test exercises the script via ``subprocess`` so
the operator-facing stderr message is verified end-to-end.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "acquire-fashion200k.py"


def _load_cli_module() -> ModuleType:
    """Import scripts/acquire-fashion200k.py as a module for in-process tests.

    Returns ``None`` (signalling pytest.fail in the test) when the script's
    new ``--metadata-file`` surface is absent — i.e. during 2A-i.
    """
    spec = importlib.util.spec_from_file_location("acquire_fashion200k_cli", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require_metadata_file_flag() -> None:
    """Skip-as-fail when the CLI hasn't grown the new flag yet (2A-i).

    Inspects the script source text rather than parsing argparse (the parser
    needs sys.argv at construction time); presence of "--metadata-file" in
    the source is sufficient to know the implementation has landed.
    """
    src = Path(SCRIPT_PATH).read_text(encoding="utf-8")
    if "--metadata-file" not in src:
        pytest.fail(
            "scripts/acquire-fashion200k.py does not yet accept --metadata-file. "
            "Expected during 2A-i pre-implementation. Implement per plan § Phase 1 "
            "revision cycle 1 (Finding 3)."
        )


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


def _write_metadata_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _three_row_metadata(tmp_path: Path) -> Path:
    metadata = tmp_path / "metadata.jsonl"
    _write_metadata_jsonl(
        metadata,
        [
            {
                "id": "women/dresses/A/a_0.jpeg",
                "category": "dress",
                "title": "red dress",
                "description": "red dress",
                "source_url": "https://row-url.test/a.jpeg",
                "detection_score": -1.0,
                "split": "train",
            },
            {
                "id": "women/dresses/B/b_0.jpeg",
                "category": "dress",
                "title": "blue dress",
                "description": "blue dress",
                "source_url": "https://row-url.test/b.jpeg",
                "detection_score": -2.0,
                "split": "train",
            },
            {
                "id": "women/dresses/C/c_0.jpeg",
                "category": "dress",
                "title": "green dress",
                "description": "green dress",
                "source_url": "https://row-url.test/c.jpeg",
                "detection_score": -3.0,
                "split": "test",
            },
        ],
    )
    return metadata


# ----------------------------------------------------------------------------
# CLI behaviour
# ----------------------------------------------------------------------------


def test_cli_requires_metadata_file_to_exist(tmp_path: Path) -> None:
    """Run the script with a nonexistent --metadata-file; expect non-zero exit
    and a stderr message pointing the operator at the prepare step."""
    nonexistent = tmp_path / "does-not-exist.jsonl"
    out = tmp_path / "manifest.json"
    data_root = tmp_path / "data"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--metadata-file",
            str(nonexistent),
            "--count",
            "1",
            "--out",
            str(out),
            "--data-root",
            str(data_root),
        ],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "AVSA_STORAGE_HMAC_SECRET": "test-secret",
        },
    )

    assert proc.returncode != 0, (
        f"CLI must exit non-zero when --metadata-file is missing; exit={proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    combined = proc.stdout + proc.stderr
    assert "metadata" in combined.lower() and "not found" in combined.lower(), (
        f"stderr must clearly identify the missing metadata file; got: {combined!r}"
    )
    assert "prepare-fashion200k-metadata" in combined, (
        f"stderr must point the operator at the prepare step; got: {combined!r}"
    )


def test_cli_reads_source_url_from_metadata_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no --source-url-template, the CLI must fetch each row's URL verbatim."""
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    module = _load_cli_module()
    _require_metadata_file_flag()

    metadata = _three_row_metadata(tmp_path)
    out = tmp_path / "manifest.json"
    data_root = tmp_path / "data"

    args = argparse.Namespace(
        seed=17,
        count=3,
        out=out,
        data_root=data_root,
        concurrency=2,
        source_url_template=None,
        metadata_file=metadata,
    )

    with respx.mock() as router:
        ra = router.get("https://row-url.test/a.jpeg").respond(200, content=b"a-bytes")
        rb = router.get("https://row-url.test/b.jpeg").respond(200, content=b"b-bytes")
        rc = router.get("https://row-url.test/c.jpeg").respond(200, content=b"c-bytes")

        rc_code = asyncio.run(module._run(args))

        assert rc_code == 0, f"expected exit 0 on all-fetched run; got {rc_code}"
        assert ra.called and rb.called and rc.called, (
            "each per-row URL must be requested verbatim; "
            f"called: a={ra.called} b={rb.called} c={rc.called}"
        )


def test_cli_source_url_template_overrides_when_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --source-url-template, the row URLs are overridden via the template."""
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    module = _load_cli_module()
    _require_metadata_file_flag()

    metadata = _three_row_metadata(tmp_path)
    out = tmp_path / "manifest.json"
    data_root = tmp_path / "data"

    args = argparse.Namespace(
        seed=17,
        count=3,
        out=out,
        data_root=data_root,
        concurrency=2,
        source_url_template="http://local/{id}",
        metadata_file=metadata,
    )

    # assert_all_called=False because the catch-all guard route is, by
    # design, expected to receive zero calls (the test asserts as much).
    with respx.mock(assert_all_called=False) as router:
        # Template URLs (NOT row URLs) — assert these are called.
        local_a = router.get("http://local/women/dresses/A/a_0.jpeg").respond(
            200, content=b"a-bytes"
        )
        local_b = router.get("http://local/women/dresses/B/b_0.jpeg").respond(
            200, content=b"b-bytes"
        )
        local_c = router.get("http://local/women/dresses/C/c_0.jpeg").respond(
            200, content=b"c-bytes"
        )
        # If row URLs slip through, this catch-all 500 surfaces as `failed`
        # results and the test detects it via the exit code.
        catchall = router.get(url__regex=r"https://row-url\.test/.*").respond(500)

        rc_code = asyncio.run(module._run(args))

        assert rc_code == 0, (
            f"expected exit 0; got {rc_code}. row-url calls (should be 0): {catchall.call_count}"
        )
        assert local_a.called and local_b.called and local_c.called, (
            "template URLs must be the ones fetched when --source-url-template is supplied"
        )
        assert catchall.call_count == 0, (
            "row source_url MUST NOT be fetched when the template override is supplied"
        )


# Silence unused-import warning when running under specific configurations.
_ = httpx
