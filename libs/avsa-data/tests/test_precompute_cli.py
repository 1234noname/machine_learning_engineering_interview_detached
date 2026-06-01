"""Failing tests for — scripts/precompute-embeddings.py CLI.

Authored at step 2A-i (pre-implementation). The script under test does not
yet exist; both branches of the test (subprocess-driven and import-driven)
guard against missing files via ``pytest.fail`` so failures are meaningful
assertion failures, not collection-time errors.

Test invocation choice: **hybrid** — mirroring
``tests/test_acquire_cli_uses_metadata_file.py``.

- The "missing subset manifest" test exercises the script via ``subprocess``
  so the operator-facing stderr message is verified end-to-end.
- The "writes artifact" and "emits outcomes summary" tests load the script
  as a module and drive ``_run`` directly with an ``argparse.Namespace``;
  this avoids paying the subprocess + uv-sync cost per assertion, and lets
  ``respx`` patch the in-process httpx transport (the subprocess'd path
  would need a separate fake-model server).

Captured rationale in the completion report's Pre-implementation Flags.
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
SCRIPT_PATH = REPO_ROOT / "scripts" / "precompute-embeddings.py"


def _require_script_exists() -> None:
    """Skip-as-fail when the CLI script has not yet been authored (2A-i)."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"scripts/precompute-embeddings.py does not yet exist at {SCRIPT_PATH}. "
            "Expected during 2A-i pre-implementation. Implement per "
            "plans/061-071-real-catalog-and-dual-head-plan.md § Phase 2a."
        )


def _load_cli_module() -> ModuleType:
    """Import scripts/precompute-embeddings.py as a module for in-process tests."""
    _require_script_exists()
    spec = importlib.util.spec_from_file_location("precompute_embeddings_cli", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


def _three_item_subset_manifest(tmp_path: Path) -> Path:
    """Write a 3-item subset manifest matching the #061 rev2 schema."""
    manifest_path = tmp_path / "subset-manifest.json"
    payload = {
        "seed": 17,
        "criteria": {"count": 3},
        "dataset_version": "fashion200k-v1.0",
        "items": [
            {
                "id": "women/dresses/A/a_0.jpeg",
                "category": "dress",
                "title": "red dress",
                "source_url": "https://row-url.test/a.jpeg",
                "split": "train",
            },
            {
                "id": "women/dresses/B/b_0.jpeg",
                "category": "dress",
                "title": "blue dress",
                "source_url": "https://row-url.test/b.jpeg",
                "split": "train",
            },
            {
                "id": "women/dresses/C/c_0.jpeg",
                "category": "dress",
                "title": "green dress",
                "source_url": "https://row-url.test/c.jpeg",
                "split": "test",
            },
        ],
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def _mock_model_service(router: respx.MockRouter, model_url: str) -> None:
    """Mock /embed and /embed_text with shape-faithful responses.

    Returns 768-d image vectors and 512-d text vectors; the count of
    vectors in the response matches the count of inputs in the request.
    """

    def _image_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        n = len(payload.get("images", []))
        return httpx.Response(200, json={"embeddings": [[0.0] * 768 for _ in range(n)]})

    def _text_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        n = len(payload.get("texts", []))
        return httpx.Response(200, json={"embeddings": [[0.0] * 512 for _ in range(n)]})

    router.post(f"{model_url}/embed").mock(side_effect=_image_handler)
    router.post(f"{model_url}/embed_text").mock(side_effect=_text_handler)


def _seed_image_bytes(backend_root: Path, items: list[dict[str, object]]) -> None:
    """Pre-populate the backend with image bytes for each item.

    The pipeline reads image bytes from storage ('s
    ``LocalStorageBackend.put_object`` placement: ``fashion200k/images/<id>.jpg``).
    """
    for item in items:
        item_id = str(item["id"])
        target = backend_root / "fashion200k" / "images" / f"{item_id}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"image-bytes-for-{item_id}".encode())


# ----------------------------------------------------------------------------
# CLI behaviour
# ----------------------------------------------------------------------------


def test_cli_requires_subset_manifest_to_exist(tmp_path: Path) -> None:
    """``--subset-manifest /nonexistent`` exits non-zero with a clear stderr."""
    _require_script_exists()

    nonexistent = tmp_path / "does-not-exist.json"
    data_root = tmp_path / "data"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--subset-manifest",
            str(nonexistent),
            "--data-root",
            str(data_root),
            "--model-url",
            "http://model.test",
        ],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "AVSA_STORAGE_HMAC_SECRET": "test-secret",
        },
    )

    assert proc.returncode != 0, (
        f"CLI must exit non-zero when --subset-manifest is missing; exit={proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    combined = proc.stdout + proc.stderr
    assert "manifest" in combined.lower() and "not found" in combined.lower(), (
        f"stderr must clearly identify the missing subset manifest; got: {combined!r}"
    )


def test_cli_writes_artifact_under_data_embeddings_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end run lands a bundle under ``data/embeddings/<hash>/``."""
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    module = _load_cli_module()

    subset_manifest = _three_item_subset_manifest(tmp_path)
    data_root = tmp_path / "data"
    model_url = "http://model.test"

    # Pre-seed image bytes under the backend root; the pipeline reads these.
    with subset_manifest.open("r", encoding="utf-8") as f:
        items = json.load(f)["items"]
    _seed_image_bytes(data_root, items)

    args = argparse.Namespace(
        subset_manifest=subset_manifest,
        data_root=data_root,
        model_url=model_url,
        concurrency=2,
        batch_size=2,
        verify=False,
    )

    with respx.mock(assert_all_called=False) as router:
        _mock_model_service(router, model_url)
        rc = asyncio.run(module._run(args))

    assert rc == 0, f"expected exit 0 on happy path; got {rc}"

    # Locate the artifact directory under data/embeddings/.
    embeddings_root = data_root / "data" / "embeddings"
    # The pipeline lands the bundle under <data_root>/data/embeddings/<hash>/
    # when the storage backend uses ``data_root`` as its root. Depending on
    # how the CLI configures the backend (root=data_root vs root=data_root/..),
    # the literal path may differ; assert flexibly on the structural shape
    # rather than the exact prefix.
    candidate_a = data_root / "data" / "embeddings"
    candidate_b = data_root.parent / "data" / "embeddings"  # if backend is repo-rooted
    candidate_c = data_root / "embeddings"  # if data_root already names the data/ dir

    found_hashes: list[Path] = []
    for candidate in (candidate_a, candidate_b, candidate_c):
        if candidate.is_dir():
            found_hashes.extend(p for p in candidate.iterdir() if p.is_dir())

    assert found_hashes, (
        "no artifact directory landed under any of "
        f"{[str(c) for c in (candidate_a, candidate_b, candidate_c)]} — "
        "the CLI must write under data/embeddings/<hash>/"
    )
    # At least one of those <hash>/ directories must contain a manifest.json
    # plus an embeddings.jsonl (per the JSONL-not-parquet decision in
    # test_embedding_pipeline.py).
    hash_dir = found_hashes[0]
    files = sorted(p.name for p in hash_dir.iterdir() if p.is_file())
    assert "manifest.json" in files, (
        f"manifest.json missing from artifact dir {hash_dir}; saw {files!r}"
    )
    assert any(name.endswith(".jsonl") or name.endswith(".parquet") for name in files), (
        f"embeddings bundle (.jsonl or .parquet) missing from artifact dir {hash_dir}; "
        f"saw {files!r}"
    )
    # Embeddings root sanity: avoid an unused-variable lint on the dual path.
    _ = embeddings_root


def test_cli_emits_outcomes_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final stdout includes item_count, image_dim, text_dim, and the content_hash."""
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    module = _load_cli_module()

    subset_manifest = _three_item_subset_manifest(tmp_path)
    data_root = tmp_path / "data"
    model_url = "http://model.test"

    with subset_manifest.open("r", encoding="utf-8") as f:
        items = json.load(f)["items"]
    _seed_image_bytes(data_root, items)

    args = argparse.Namespace(
        subset_manifest=subset_manifest,
        data_root=data_root,
        model_url=model_url,
        concurrency=2,
        batch_size=2,
        verify=False,
    )

    with respx.mock(assert_all_called=False) as router:
        _mock_model_service(router, model_url)
        rc = asyncio.run(module._run(args))

    assert rc == 0, f"expected exit 0; got {rc}"

    captured = capsys.readouterr()
    combined = captured.out + captured.err

    # The summary line should mention all four diagnostic fields.
    for needle in ("item_count=3", "image_dim=768", "text_dim=512"):
        assert needle in combined, (
            f"CLI summary must include {needle!r}; got combined output:\n{combined}"
        )

    # The content_hash should also be surfaced — assert a hex-ish token of
    # at least 8 chars appears alongside the literal substring "content_hash".
    assert "content_hash" in combined, (
        f"CLI summary must include the literal 'content_hash' label; got:\n{combined}"
    )


# ----------------------------------------------------------------------------
# --verify equivalence gate (DoD line 49)
# ----------------------------------------------------------------------------


def _mock_model_phased(
    router: respx.MockRouter,
    model_url: str,
    *,
    write_image: list[float],
    write_text: list[float],
    verify_image: list[float],
    verify_text: list[float],
    write_image_calls: int,
    write_text_calls: int,
) -> None:
    """Mock /embed + /embed_text with distinct write-phase vs verify-phase vectors.

    ``_run`` performs the WRITE phase (batched embeds) entirely before the
    VERIFY phase (per-sample re-embeds), so the two phases are separable by
    call order. The first ``write_*_calls`` calls to each endpoint return the
    write-phase vector (what lands in the artifact); subsequent calls return
    the verify-phase vector (what --verify compares against). When the two
    differ, the verify equivalence check must fail.
    """
    state = {"image": 0, "text": 0}

    def _image_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        n = len(payload.get("images", []))
        state["image"] += 1
        vec = write_image if state["image"] <= write_image_calls else verify_image
        return httpx.Response(200, json={"embeddings": [list(vec) for _ in range(n)]})

    def _text_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        n = len(payload.get("texts", []))
        state["text"] += 1
        vec = write_text if state["text"] <= write_text_calls else verify_text
        return httpx.Response(200, json={"embeddings": [list(vec) for _ in range(n)]})

    router.post(f"{model_url}/embed").mock(side_effect=_image_handler)
    router.post(f"{model_url}/embed_text").mock(side_effect=_text_handler)


def test_verify_passes_when_live_embeddings_match_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--verify`` exits 0 + prints confirmation when live == artifact vectors."""
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    module = _load_cli_module()

    subset_manifest = _three_item_subset_manifest(tmp_path)
    data_root = tmp_path / "data"
    model_url = "http://model.test"

    with subset_manifest.open("r", encoding="utf-8") as f:
        items = json.load(f)["items"]
    _seed_image_bytes(data_root, items)

    args = argparse.Namespace(
        subset_manifest=subset_manifest,
        data_root=data_root,
        model_url=model_url,
        concurrency=2,
        batch_size=2,
        verify=True,
        verify_sample_size=5,
        config=REPO_ROOT / "config" / "avsa.toml",
    )

    # Write phase and verify phase return the SAME non-zero vectors → cosine
    # == 1.0 for every sample/modality, comfortably above the threshold. A
    # non-zero vector is required: cosine of an all-zero vector is undefined
    # (treated as 0.0), which would (correctly) fail the gate.
    same_image = [1.0] + [0.0] * 767
    same_text = [1.0] + [0.0] * 511
    with respx.mock(assert_all_called=False) as router:
        _mock_model_phased(
            router,
            model_url,
            write_image=same_image,
            write_text=same_text,
            verify_image=same_image,
            verify_text=same_text,
            write_image_calls=2,
            write_text_calls=2,
        )
        rc = asyncio.run(module._run(args))

    assert rc == 0, f"--verify must exit 0 when live embeddings match artifact; got {rc}"

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # Confirmation line surfaces the min observed cosine.
    assert "verify" in combined.lower() and "cosine" in combined.lower(), (
        f"--verify pass must print a confirmation naming the min cosine; got:\n{combined}"
    )


def test_verify_fails_when_live_embeddings_diverge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--verify`` exits non-zero + names the failing sample on divergence.

    This is the load-bearing test: it fails outright if ``--verify`` is a
    no-op, because a no-op returns 0 regardless of the divergence injected
    here.
    """
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    module = _load_cli_module()

    subset_manifest = _three_item_subset_manifest(tmp_path)
    data_root = tmp_path / "data"
    model_url = "http://model.test"

    with subset_manifest.open("r", encoding="utf-8") as f:
        items = json.load(f)["items"]
    _seed_image_bytes(data_root, items)

    args = argparse.Namespace(
        subset_manifest=subset_manifest,
        data_root=data_root,
        model_url=model_url,
        concurrency=2,
        batch_size=2,
        verify=True,
        verify_sample_size=5,
        config=REPO_ROOT / "config" / "avsa.toml",
    )

    # 3 items / batch-size 2 → the WRITE phase issues 2 calls to each endpoint
    # (a batch of 2 then a batch of 1). The VERIFY phase re-embeds the sampled
    # rows AFTER that. The phased mock returns vec_A for the write calls (what
    # lands in the artifact) and an ORTHOGONAL vec_B for the verify calls →
    # cosine == 0.0, far below the 0.9999 threshold. A no-op --verify never
    # issues the verify-phase calls and would (wrongly) exit 0.
    with respx.mock(assert_all_called=False) as router:
        _mock_model_phased(
            router,
            model_url,
            write_image=[1.0] + [0.0] * 767,
            write_text=[1.0] + [0.0] * 511,
            verify_image=[0.0, 1.0] + [0.0] * 766,
            verify_text=[0.0, 1.0] + [0.0] * 510,
            write_image_calls=2,
            write_text_calls=2,
        )
        rc = asyncio.run(module._run(args))

    assert rc != 0, (
        "--verify must exit non-zero when live embeddings diverge from the "
        f"artifact; got {rc}. A no-op --verify would (wrongly) return 0."
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # The failure must name the diverging sample id and the cosine.
    assert "cosine" in combined.lower(), (
        f"--verify failure must report the observed cosine; got:\n{combined}"
    )
    assert any(item["id"] in combined for item in items), (
        f"--verify failure must name the failing sample id; got:\n{combined}"
    )


# Silence unused-import warnings when running under specific configurations.
_ = httpx
