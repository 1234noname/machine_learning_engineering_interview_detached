"""Tests for scripts/build-fashion200k-checksums.py ( rev2 Fix B).

The checksum script reads ``data/fashion200k/labels/*.txt`` and
``data/fashion200k/image_urls.txt``, emits ``<sha256>  <relative_path>``
lines (the same format ``sha256sum`` itself prints), sorted by path,
and writes to ``--out``. The output file is committed at
``evals/catalog/fashion200k/inputs-sha256.txt`` so a reproducer can
verify their upstream download matches the snapshot this build was
made against.

These tests cover:

1. Format: every line is ``<64-hex>  <relative_path>\\n``.
2. Determinism: output is sorted by path.
3. Round-trip: the file is verifiable with the standard ``sha256sum -c``
   tool (or ``shasum -a 256 -c`` on macOS without GNU coreutils).
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "build-fashion200k-checksums.py"

# Format the standard sha256sum tool emits: 64-char hex, two spaces,
# then the relative path. We enforce two spaces (not one) because that
# is what ``sha256sum -c`` accepts as canonical.
_SHA256SUM_LINE_RE = re.compile(r"^[0-9a-f]{64}  .+$")


def _load_script() -> ModuleType:
    """Import scripts/build-fashion200k-checksums.py as a module.

    Calls pytest.fail with a meaningful message if the script doesn't
    exist yet — distinguishes "pre-implementation" from "broken import".
    """
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"{SCRIPT_PATH} does not exist yet — expected during 2A-i "
            "pre-implementation. Implement rev2 Fix B."
        )
    spec = importlib.util.spec_from_file_location("build_fashion200k_checksums", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_fashion200k_tree(tmp_path: Path) -> Path:
    """Build a minimal data/fashion200k/ tree with 2 label files + image_urls.txt.

    Returns the root we'd pass as --data-root.
    """
    root = tmp_path / "data" / "fashion200k"
    labels = root / "labels"
    labels.mkdir(parents=True)
    (labels / "dress_test_detect_all.txt").write_text(
        "women/dresses/A/a_0.jpeg\t-1.0\tred dress\n", encoding="utf-8"
    )
    (labels / "dress_train_detect_all.txt").write_text(
        "women/dresses/B/b_0.jpeg\t-2.0\tblue dress\n", encoding="utf-8"
    )
    (root / "image_urls.txt").write_text(
        "women/dresses/A/a_0.jpeg\thttps://example.test/a.jpeg\n"
        "women/dresses/B/b_0.jpeg\thttps://example.test/b.jpeg\n",
        encoding="utf-8",
    )
    return root


def _run_script(data_root: Path, out_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--data-root",
            str(data_root),
            "--out",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_checksums_emits_sha256sum_format(tmp_path: Path) -> None:
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"{SCRIPT_PATH} does not exist yet — expected during 2A-i "
            "pre-implementation. Implement rev2 Fix B."
        )
    data_root = _make_fashion200k_tree(tmp_path)
    out_path = tmp_path / "inputs-sha256.txt"

    proc = _run_script(data_root, out_path)
    assert proc.returncode == 0, (
        f"script exited non-zero ({proc.returncode}); stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert out_path.exists(), f"expected output file at {out_path} after run"

    text = out_path.read_text(encoding="utf-8")
    assert text.endswith("\n"), "output file must end with a trailing newline"

    lines = text.rstrip("\n").split("\n")
    # 2 label files + 1 image_urls.txt
    assert len(lines) == 3, f"expected 3 lines (2 labels + image_urls); got {len(lines)}: {lines!r}"
    for line in lines:
        assert _SHA256SUM_LINE_RE.match(line), (
            f"line does not match sha256sum format (<64-hex>  <path>): {line!r}"
        )
    # And every recorded hash actually matches its input file.
    for line in lines:
        digest, rel_path = line.split("  ", 1)
        on_disk = (data_root / rel_path).read_bytes()
        assert hashlib.sha256(on_disk).hexdigest() == digest, (
            f"recorded digest for {rel_path!r} does not match its on-disk content"
        )


def test_build_checksums_sorted_by_path(tmp_path: Path) -> None:
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"{SCRIPT_PATH} does not exist yet — expected during 2A-i "
            "pre-implementation. Implement rev2 Fix B."
        )
    data_root = _make_fashion200k_tree(tmp_path)
    out_path = tmp_path / "inputs-sha256.txt"

    proc = _run_script(data_root, out_path)
    assert proc.returncode == 0, f"script failed: {proc.stderr!r}"

    text_first = out_path.read_text(encoding="utf-8")
    paths_first = [line.split("  ", 1)[1] for line in text_first.rstrip("\n").split("\n")]
    assert paths_first == sorted(paths_first), (
        f"output paths must be sorted; got order: {paths_first!r}"
    )

    # Re-run; should be byte-identical (idempotent + deterministic).
    proc2 = _run_script(data_root, out_path)
    assert proc2.returncode == 0, f"second run failed: {proc2.stderr!r}"
    text_second = out_path.read_text(encoding="utf-8")
    assert text_first == text_second, (
        "two consecutive runs must produce byte-identical output (idempotent)"
    )


def test_build_checksums_compatible_with_sha256sum_dash_c(tmp_path: Path) -> None:
    """Verify the file is consumable by the standard ``sha256sum -c`` tool."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"{SCRIPT_PATH} does not exist yet — expected during 2A-i "
            "pre-implementation. Implement rev2 Fix B."
        )

    # Locate a sha256-verifying tool. GNU coreutils ships ``sha256sum``;
    # macOS without coreutils ships ``shasum`` (POSIX-y).
    sha256sum = shutil.which("sha256sum")
    shasum = shutil.which("shasum")
    if sha256sum is not None:
        verify_cmd = [sha256sum, "-c", "inputs-sha256.txt"]
    elif shasum is not None:
        verify_cmd = [shasum, "-a", "256", "-c", "inputs-sha256.txt"]
    else:
        pytest.skip("neither sha256sum nor shasum available on PATH")

    data_root = _make_fashion200k_tree(tmp_path)
    out_path = data_root / "inputs-sha256.txt"

    proc = _run_script(data_root, out_path)
    assert proc.returncode == 0, f"script failed: {proc.stderr!r}"

    # ``sha256sum -c`` resolves paths relative to its CWD, so run it from
    # data_root (which is the recorded base for the relative paths).
    verify = subprocess.run(
        verify_cmd,
        cwd=data_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, (
        f"sha256 verifier rejected the file; stdout={verify.stdout!r} stderr={verify.stderr!r}"
    )
    # Every line should show ': OK' — three inputs, three OKs.
    ok_lines = [line for line in verify.stdout.splitlines() if line.endswith(": OK")]
    assert len(ok_lines) == 3, (
        f"expected 3 ': OK' lines (2 labels + image_urls); got {len(ok_lines)}: {verify.stdout!r}"
    )


# Silence unused-import warnings when running under specific configurations.
_ = hashlib
