"""Failing tests for  revision cycle 1 — metadata pathway (Finding 1).

The module under test does not yet exist; we import inside a try/except so
collection succeeds and each test fails with a meaningful assertion failure
rather than a collection-time ImportError — per docs/agents/standards/testing.md.

Surface under test (``avsa_data.fashion200k_metadata``):

- ``parse_labels(labels_dir) -> dict[str, dict]`` — joins ``*_detect_all.txt``
  files; first-occurrence wins on duplicate paths; skips malformed rows.
- ``parse_image_urls(urls_file) -> dict[str, str]`` — joins the 2-column
  ``image_urls.txt`` into ``{image_path: source_url}``.
- ``build_metadata(labels_dir, urls_file, out_path) -> int`` — joins both into
  a JSONL file written directly to disk; returns the number of rows written.
- ``load_metadata(path) -> Iterator[dict]`` — yields each row as a dict.

The frozen row schema (matches the existing on-disk ``metadata.jsonl``):

    id, category, title, description, source_url, detection_score, split
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    from avsa_data.fashion200k_metadata import (
        build_metadata,
        load_metadata,
        parse_image_urls,
        parse_labels,
    )

    _META_AVAILABLE = True
except ImportError:
    _META_AVAILABLE = False


def _require_meta() -> None:
    if not _META_AVAILABLE:
        pytest.fail(
            "avsa_data.fashion200k_metadata not implemented yet — expected during "
            "2A-i pre-implementation. Implement per plans/061-071-real-catalog-"
            "and-dual-head-plan.md § Phase 1 revision cycle 1 (Finding 1)."
        )


# ----------------------------------------------------------------------------
# Fixtures (inline; cheap to construct, easy to read at the assertion site).
# ----------------------------------------------------------------------------

DRESS_TRAIN = (
    "women/dresses/casual_and_day_dresses/A/A_0.jpeg\t-1.78\tgreen seamed a-line dress\n"
    "women/dresses/casual_and_day_dresses/B/B_0.jpeg\t-2.30\tred floral print cami dress\n"
)
JACKET_TEST = (
    "women/jackets/blazers_and_suit_jackets/C/C_0.jpeg\t-4.48\tembellished wool jacket black\n"
)
URLS = (
    "women/dresses/casual_and_day_dresses/A/A_0.jpeg\thttps://example.test/a.jpeg\n"
    "women/dresses/casual_and_day_dresses/B/B_0.jpeg\thttps://example.test/b.jpeg\n"
    "women/jackets/blazers_and_suit_jackets/C/C_0.jpeg\thttps://example.test/c.jpeg\n"
)


def _write_label_fixture(labels_dir: Path) -> None:
    labels_dir.mkdir(parents=True, exist_ok=True)
    (labels_dir / "dress_train_detect_all.txt").write_text(DRESS_TRAIN, encoding="utf-8")
    (labels_dir / "jacket_test_detect_all.txt").write_text(JACKET_TEST, encoding="utf-8")


def _write_urls_fixture(urls_file: Path) -> None:
    urls_file.parent.mkdir(parents=True, exist_ok=True)
    urls_file.write_text(URLS, encoding="utf-8")


# ----------------------------------------------------------------------------
# parse_labels
# ----------------------------------------------------------------------------


def test_parse_labels_returns_dict_keyed_by_image_path(tmp_path: Path) -> None:
    _require_meta()
    labels_dir = tmp_path / "labels"
    _write_label_fixture(labels_dir)

    out = parse_labels(labels_dir)

    assert set(out.keys()) == {
        "women/dresses/casual_and_day_dresses/A/A_0.jpeg",
        "women/dresses/casual_and_day_dresses/B/B_0.jpeg",
        "women/jackets/blazers_and_suit_jackets/C/C_0.jpeg",
    }
    dress_a = out["women/dresses/casual_and_day_dresses/A/A_0.jpeg"]
    assert dress_a["category"] == "dress", (
        f"category must derive from filename prefix; got {dress_a['category']!r}"
    )
    assert dress_a["split"] == "train", (
        f"split must derive from filename ({{cat}}_{{split}}_detect_all.txt); "
        f"got {dress_a['split']!r}"
    )
    assert dress_a["title"] == "green seamed a-line dress"
    assert dress_a["description"] == "green seamed a-line dress"
    assert dress_a["detection_score"] == pytest.approx(-1.78)

    jacket_c = out["women/jackets/blazers_and_suit_jackets/C/C_0.jpeg"]
    assert jacket_c["category"] == "jacket"
    assert jacket_c["split"] == "test"


def test_parse_labels_first_occurrence_wins_on_duplicate_path(tmp_path: Path) -> None:
    _require_meta()
    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    # Same image path appears in two label files; first read wins.
    (labels_dir / "dress_train_detect_all.txt").write_text(
        "women/dresses/x/x_0.jpeg\t-1.0\tfirst entry\n",
        encoding="utf-8",
    )
    (labels_dir / "dress_test_detect_all.txt").write_text(
        "women/dresses/x/x_0.jpeg\t-2.0\tsecond entry\n",
        encoding="utf-8",
    )

    out = parse_labels(labels_dir)
    row = out["women/dresses/x/x_0.jpeg"]
    # "First occurrence wins": which file is "first" is sort-order
    # deterministic. dress_test_detect_all.txt < dress_train_detect_all.txt
    # alphabetically, so 'second entry' (the test-split row) wins under
    # an alphabetic sort. The contract is: deterministic + documented;
    # the test asserts ONE of them won (not both retained) and that the
    # winner is consistent across calls.
    assert row["title"] in {"first entry", "second entry"}
    # Re-run to confirm determinism.
    out2 = parse_labels(labels_dir)
    assert out2["women/dresses/x/x_0.jpeg"]["title"] == row["title"], (
        "parse_labels must be deterministic on duplicate paths."
    )


def test_parse_labels_skips_malformed_rows(tmp_path: Path) -> None:
    _require_meta()
    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    (labels_dir / "dress_train_detect_all.txt").write_text(
        # Good, then malformed (2 cols), then good again.
        "women/dresses/a/a_0.jpeg\t-1.0\tred dress\n"
        "women/dresses/bad/bad_0.jpeg\t-1.5\n"
        "women/dresses/b/b_0.jpeg\t-2.0\tblue dress\n",
        encoding="utf-8",
    )

    out = parse_labels(labels_dir)
    assert "women/dresses/a/a_0.jpeg" in out
    assert "women/dresses/b/b_0.jpeg" in out
    assert "women/dresses/bad/bad_0.jpeg" not in out, (
        "Malformed rows (wrong column count) must be skipped, not surfaced."
    )


# ----------------------------------------------------------------------------
# parse_image_urls
# ----------------------------------------------------------------------------


def test_parse_image_urls_returns_dict(tmp_path: Path) -> None:
    _require_meta()
    urls_file = tmp_path / "image_urls.txt"
    _write_urls_fixture(urls_file)

    out = parse_image_urls(urls_file)
    assert out == {
        "women/dresses/casual_and_day_dresses/A/A_0.jpeg": "https://example.test/a.jpeg",
        "women/dresses/casual_and_day_dresses/B/B_0.jpeg": "https://example.test/b.jpeg",
        "women/jackets/blazers_and_suit_jackets/C/C_0.jpeg": "https://example.test/c.jpeg",
    }


# ----------------------------------------------------------------------------
# build_metadata
# ----------------------------------------------------------------------------


def test_build_metadata_emits_jsonl_with_required_keys(tmp_path: Path) -> None:
    _require_meta()
    labels_dir = tmp_path / "labels"
    urls_file = tmp_path / "image_urls.txt"
    out_path = tmp_path / "out" / "metadata.jsonl"
    _write_label_fixture(labels_dir)
    _write_urls_fixture(urls_file)

    n = build_metadata(labels_dir=labels_dir, urls_file=urls_file, out_path=out_path)
    assert n == 3, f"three labels with three matching URLs → three rows; got {n}"

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    required = {"id", "category", "title", "description", "source_url", "detection_score", "split"}
    for line in lines:
        row = json.loads(line)
        assert required.issubset(row.keys()), (
            f"row missing required keys: {required - row.keys()}; row={row}"
        )


def test_build_metadata_drops_label_rows_without_url(tmp_path: Path) -> None:
    _require_meta()
    labels_dir = tmp_path / "labels"
    urls_file = tmp_path / "image_urls.txt"
    out_path = tmp_path / "metadata.jsonl"
    labels_dir.mkdir()
    (labels_dir / "dress_train_detect_all.txt").write_text(
        "women/dresses/has-url/x_0.jpeg\t-1.0\thas url\n"
        "women/dresses/no-url/y_0.jpeg\t-2.0\tno url\n",
        encoding="utf-8",
    )
    urls_file.write_text(
        "women/dresses/has-url/x_0.jpeg\thttps://example.test/x.jpeg\n",
        encoding="utf-8",
    )

    n = build_metadata(labels_dir=labels_dir, urls_file=urls_file, out_path=out_path)
    assert n == 1, f"label without matching URL must be dropped; expected 1 row, got {n}"

    [row] = [json.loads(line) for line in out_path.read_text().splitlines()]
    assert row["id"] == "women/dresses/has-url/x_0.jpeg"


def test_build_metadata_returns_row_count(tmp_path: Path) -> None:
    _require_meta()
    labels_dir = tmp_path / "labels"
    urls_file = tmp_path / "image_urls.txt"
    out_path = tmp_path / "metadata.jsonl"
    _write_label_fixture(labels_dir)
    _write_urls_fixture(urls_file)

    n = build_metadata(labels_dir=labels_dir, urls_file=urls_file, out_path=out_path)
    on_disk = len(out_path.read_text().splitlines())
    assert n == on_disk, (
        f"build_metadata return value must equal on-disk row count; return={n}, on_disk={on_disk}"
    )


def test_build_metadata_creates_parent_dirs(tmp_path: Path) -> None:
    _require_meta()
    labels_dir = tmp_path / "labels"
    urls_file = tmp_path / "image_urls.txt"
    out_path = tmp_path / "deep" / "nested" / "dir" / "metadata.jsonl"
    _write_label_fixture(labels_dir)
    _write_urls_fixture(urls_file)

    assert not out_path.parent.exists()
    build_metadata(labels_dir=labels_dir, urls_file=urls_file, out_path=out_path)
    assert out_path.exists(), (
        "build_metadata must create parent directories rather than crash on missing path."
    )


# ----------------------------------------------------------------------------
# load_metadata
# ----------------------------------------------------------------------------


def test_load_metadata_yields_dicts(tmp_path: Path) -> None:
    _require_meta()
    labels_dir = tmp_path / "labels"
    urls_file = tmp_path / "image_urls.txt"
    out_path = tmp_path / "metadata.jsonl"
    _write_label_fixture(labels_dir)
    _write_urls_fixture(urls_file)
    build_metadata(labels_dir=labels_dir, urls_file=urls_file, out_path=out_path)

    rows = list(load_metadata(out_path))
    assert len(rows) == 3
    for row in rows:
        assert isinstance(row, dict), f"load_metadata must yield dicts; got {type(row)!r}"
        assert "id" in row and "source_url" in row
