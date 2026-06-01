"""Unit tests for the catalog seed helpers and surrounding wiring.

These tests do NOT touch the database — they exercise the pure helper
functions used by `scripts/seed-catalog.py` and assert that the
operator-facing wiring (workflow, justfile recipe, config keys, docs)
is correctly set up. Integration tests that require a live Postgres +
pgvector instance live in `tests/integration/test_catalog_fixture.py`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import stat
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from machine_learning_engineering_interview import catalog_seed


def _copy_rows() -> Callable[..., int]:
    """``catalog_seed.copy_rows`` fetched dynamically.

        Accessed via ``getattr`` so this test file can pass the repo-root mypy gate
        *before* ``copy_rows`` grows its new ``include_text_embedding`` keyword
    . Once implemented the signature change is statically visible to
        callers in production; the test deliberately stays loose here.
    """
    return cast("Callable[..., int]", getattr(catalog_seed, "copy_rows"))  # noqa: B009


def _rows_for_source() -> Callable[..., Iterator[dict[str, Any]]]:
    """``catalog_seed.rows_for_source`` fetched dynamically (see ``_copy_rows``)."""
    return cast(
        "Callable[..., Iterator[dict[str, Any]]]",
        getattr(catalog_seed, "rows_for_source"),  # noqa: B009
    )


REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed-catalog.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "seed-catalog.yml"
CONFIG = REPO_ROOT / "config" / "avsa.toml"
STAKEHOLDERS = REPO_ROOT / "STAKEHOLDERS.md"
JUSTFILE = REPO_ROOT / "justfile"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SETUP_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "setup.md"


# ---------------------------------------------------------------------------
# Stub embedding generator — deterministic, 768-d, reproducible per id.
# ---------------------------------------------------------------------------


class TestStubEmbedding:
    def test_returns_list_of_768_floats(self) -> None:
        vec = catalog_seed.stub_embedding(0)
        assert isinstance(vec, list)
        assert len(vec) == 768
        assert all(isinstance(v, float) for v in vec)

    def test_is_deterministic_for_same_seed(self) -> None:
        assert catalog_seed.stub_embedding(42) == catalog_seed.stub_embedding(42)

    def test_differs_across_seeds(self) -> None:
        assert catalog_seed.stub_embedding(0) != catalog_seed.stub_embedding(1)

    def test_values_are_non_negative(self) -> None:
        # Components of a normalised positive-quadrant vector are in [0, 1].
        vec = catalog_seed.stub_embedding(7)
        assert all(v >= 0.0 for v in vec)

    def test_is_l2_normalised(self) -> None:
        """stub_embedding must return a unit vector.

        pgvector's <=> cosine-distance operator requires unit vectors to give
        correct results — a non-normalised stub would silently produce wrong
        similarity rankings in every integration test that seeds the catalog.
        """
        import math

        for seed in [0, 1, 42, 999, 10_000]:
            vec = catalog_seed.stub_embedding(seed)
            norm = math.sqrt(sum(v * v for v in vec))
            assert norm == pytest.approx(1.0, abs=1e-5), (
                f"stub_embedding({seed}) is not L2-normalised: norm={norm:.8f}"
            )


# ---------------------------------------------------------------------------
# Synthetic product generator — deterministic row content per index.
# ---------------------------------------------------------------------------


class TestSyntheticProduct:
    def test_produces_all_required_columns(self) -> None:
        row = catalog_seed.synthetic_product(0)
        # Columns must match catalog.products NOT-NULL set from specs/db/catalog.sql.
        for key in (
            "title",
            "category",
            "colour",
            "formality",
            "occasion",
            "price_cents",
            "image_url",
            "embedding",
        ):
            assert key in row, f"missing required column {key!r}"

    def test_is_deterministic_per_index(self) -> None:
        a = catalog_seed.synthetic_product(123)
        b = catalog_seed.synthetic_product(123)
        assert a == b

    def test_embedding_is_768_dim(self) -> None:
        row = catalog_seed.synthetic_product(0)
        assert len(row["embedding"]) == 768

    def test_price_cents_is_positive_int(self) -> None:
        row = catalog_seed.synthetic_product(0)
        assert isinstance(row["price_cents"], int)
        assert row["price_cents"] > 0


# ---------------------------------------------------------------------------
# Config loader — reads catalog seed knobs from config/avsa.toml.
# ---------------------------------------------------------------------------


class TestConfigLoader:
    def test_loads_seed_count_default(self) -> None:
        #  caps the subset at 5000 (local-resource cap, 2026-05-25;
        # superseding #028's 10k synthetic default). config/avsa.toml is the
        # single source of truth; assert the loader reads the current bound.
        cfg = catalog_seed.load_config(CONFIG)
        assert cfg.seed_count == 5000

    def test_loads_committed_source(self) -> None:
        # The committed default source is "fashion200k", not synthetic-v1
        # ( + user-confirmed decision 2026-05-25 flipped the catalog
        # source; see docs/adr/0007-catalog-dataset-fashion200k.md amendment and
        # STAKEHOLDERS.md "Catalog data"). fashion200k is the committed default
        # across all AVSA environments; synthetic-v1 is retained only as the
        # hermetic pytest-fixture / CI source. This is intentional, not drift.
        cfg = catalog_seed.load_config(CONFIG)
        assert cfg.source == "fashion200k"


# ---------------------------------------------------------------------------
# Seed script — exists, executable, license + source documented in header.
# ---------------------------------------------------------------------------


class TestSeedScript:
    def test_script_exists(self) -> None:
        assert SEED_SCRIPT.is_file(), f"missing {SEED_SCRIPT}"

    def test_script_is_executable(self) -> None:
        mode = SEED_SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, "scripts/seed-catalog.py must be chmod +x"

    def test_header_documents_data_source(self) -> None:
        head = SEED_SCRIPT.read_text(encoding="utf-8")[:2000]
        assert "synthetic-v1" in head, "header must name the catalog source"

    def test_header_documents_license(self) -> None:
        head = SEED_SCRIPT.read_text(encoding="utf-8")[:2000]
        assert re.search(r"license", head, re.IGNORECASE), (
            "header must document the data license"
        )

    def test_stub_env_var_is_documented(self) -> None:
        head = SEED_SCRIPT.read_text(encoding="utf-8")[:2000]
        assert "AVSA_EMBED_STUB" in head, "AVSA_EMBED_STUB toggle must be documented"


# ---------------------------------------------------------------------------
# CI workflow — manual + scheduled only, never per-PR.
# ---------------------------------------------------------------------------


class TestSeedWorkflow:
    @pytest.fixture
    def workflow_yaml(self) -> dict[str, object]:
        with WORKFLOW.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)  # type: ignore[no-any-return]

    def test_workflow_exists(self) -> None:
        assert WORKFLOW.is_file(), f"missing {WORKFLOW}"

    def test_only_manual_or_scheduled_triggers(
        self, workflow_yaml: dict[str, object]
    ) -> None:
        # PyYAML parses the bare `on:` key as the boolean True; tolerate either.
        triggers = workflow_yaml.get("on")
        if triggers is None:
            triggers = workflow_yaml.get(True)  # type: ignore[call-overload]
        assert isinstance(triggers, dict), (
            f"expected mapping triggers, got {triggers!r}"
        )
        allowed = {"workflow_dispatch", "schedule"}
        forbidden = set(triggers.keys()) - allowed
        assert not forbidden, (
            f"seed-catalog workflow must only run on manual or scheduled triggers; "
            f"forbidden triggers present: {sorted(forbidden)}"
        )
        assert "workflow_dispatch" in triggers
        assert "schedule" in triggers

    def test_action_shas_are_pinned(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        # Find every `uses: owner/repo@<ref>` and assert <ref> is a 40-char SHA.
        # Local composite actions (uses: ./.github/actions/...) are exempt.
        uses_re = re.compile(r"uses:\s*([^\s#]+)")
        for ref in uses_re.findall(text):
            if ref.startswith("./"):
                continue
            assert "@" in ref, f"unpinned action reference: {ref}"
            _, sha = ref.rsplit("@", 1)
            assert re.fullmatch(r"[0-9a-f]{40}", sha), (
                f"action {ref} must pin a 40-char commit SHA"
            )

    def test_not_referenced_from_per_pr_ci(self) -> None:
        ci = CI_WORKFLOW.read_text(encoding="utf-8")
        assert "seed-catalog.yml" not in ci, (
            "ci.yml must not reference the seed-catalog workflow (per-PR PRs must not "
            "trigger the full 10k seed; story-001 )."
        )


# ---------------------------------------------------------------------------
# Justfile recipe — sets stub=0, runs the script.
# ---------------------------------------------------------------------------


class TestJustfileRecipe:
    def test_recipe_present(self) -> None:
        text = JUSTFILE.read_text(encoding="utf-8")
        assert re.search(r"(?m)^seed-catalog\b", text), (
            "missing `just seed-catalog` recipe"
        )

    def test_recipe_disables_stub_and_runs_script(self) -> None:
        text = JUSTFILE.read_text(encoding="utf-8")
        # The recipe must call the script and explicitly set the stub flag to 0
        # so a real /embed call is made — the stub is for tests only.
        recipe_body = re.search(r"(?m)^seed-catalog\b.*?(?=^[a-z]|\Z)", text, re.DOTALL)
        assert recipe_body, "no `seed-catalog` recipe block found"
        body = recipe_body.group(0)
        assert "AVSA_EMBED_STUB=0" in body, "recipe must set AVSA_EMBED_STUB=0"
        assert "scripts/seed-catalog.py" in body, "recipe must invoke the seed script"


# ---------------------------------------------------------------------------
# config/avsa.toml — required catalog keys with documented defaults.
# ---------------------------------------------------------------------------


class TestConfigFile:
    @pytest.fixture
    def config(self) -> dict[str, object]:
        with CONFIG.open("rb") as fh:
            return tomllib.load(fh)

    def test_catalog_seed_count_present(self, config: dict[str, object]) -> None:
        catalog = config.get("catalog")
        assert isinstance(catalog, dict)
        # : subset cap reduced to 5000 (local-resource cap,
        # 2026-05-25), superseding #028's 10k synthetic default.
        assert catalog.get("seed_count") == 5000

    def test_catalog_source_present(self, config: dict[str, object]) -> None:
        catalog = config.get("catalog")
        assert isinstance(catalog, dict)
        # The committed source is "fashion200k" — the deliberate default flipped
        # from synthetic-v1 by  + the 2026-05-25 decision (see
        # docs/adr/0007-catalog-dataset-fashion200k.md amendment and
        # STAKEHOLDERS.md). synthetic-v1 is retained only as the hermetic
        # fixture/CI source. Intentional, not drift.
        assert catalog.get("source") == "fashion200k"


# ---------------------------------------------------------------------------
# STAKEHOLDERS.md — data provenance documented.
# ---------------------------------------------------------------------------


class TestStakeholdersDoc:
    def test_exists(self) -> None:
        assert STAKEHOLDERS.is_file(), f"missing {STAKEHOLDERS}"

    def test_documents_catalog_source_and_license(self) -> None:
        text = STAKEHOLDERS.read_text(encoding="utf-8")
        assert "synthetic-v1" in text, "must name the synthetic catalog source"
        assert re.search(r"license", text, re.IGNORECASE), (
            "must document the data license"
        )


# ---------------------------------------------------------------------------
# Defensive: stub helpers always produce JSON-serialisable rows so a future
# COPY path can round-trip them through text format if needed.
# ---------------------------------------------------------------------------


class TestRowSerialisability:
    def test_synthetic_product_round_trips_json(self) -> None:
        row = catalog_seed.synthetic_product(0)
        # vector field is large; round-trip a slim view to keep the assertion fast.
        slim = {k: v for k, v in row.items() if k != "embedding"}
        assert json.loads(json.dumps(slim)) == slim


# ---------------------------------------------------------------------------
# — copy_rows text_embedding parameterization.
#
# These exercise the new `include_text_embedding` flag on copy_rows. They use
# a fake COPY cursor (mock at the psycopg boundary, per testing.md § Mock
# discipline) that records the COPY column-list string and the per-row tuples
# written, so the assertions inspect the wire shape without a live Postgres.
# ---------------------------------------------------------------------------


class _FakeCopy:
    """Captures ``write_row`` tuples for one ``cur.copy(...)`` context."""

    def __init__(self, recorder: _FakeCursor) -> None:
        self._recorder = recorder

    def __enter__(self) -> _FakeCopy:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def write_row(self, row: tuple[object, ...]) -> None:
        self._recorder.written_rows.append(row)


class _FakeCursor:
    """Minimal stand-in for a psycopg cursor used by ``copy_rows``."""

    def __init__(self) -> None:
        self.copy_sql: str | None = None
        self.written_rows: list[tuple[object, ...]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def copy(self, sql: str) -> _FakeCopy:
        self.copy_sql = sql
        return _FakeCopy(self)


class _FakeConn:
    """Stand-in for ``psycopg.Connection`` exposing only ``cursor()``."""

    def __init__(self) -> None:
        self.cur = _FakeCursor()

    def cursor(self) -> _FakeCursor:
        return self.cur


def _fashion200k_like_row() -> dict[str, Any]:
    """A row shaped like the fashion200k loader output (image + text vectors)."""
    return {
        "title": "black knit midi dress",
        "category": "dress",
        "colour": "black",
        "formality": "smart-casual",
        "occasion": "everyday",
        "price_cents": 7999,
        "image_url": "/images/fashion200k/images/x.jpeg.jpg",
        "embedding": [0.1] * catalog_seed.EMBEDDING_DIM,
        "text_embedding": [0.2] * 512,
    }


class TestCopyRowsTextEmbedding:
    def test_default_excludes_text_embedding(self) -> None:
        """Default (synthetic-v1 contract): 8-column COPY, no text_embedding."""
        conn = _FakeConn()
        row = catalog_seed.synthetic_product(0)
        written = _copy_rows()(conn, [row])
        assert written == 1
        cur = conn.cur
        assert cur.copy_sql is not None
        assert "text_embedding" not in cur.copy_sql, (
            "default copy_rows must not reference text_embedding in the COPY "
            f"column list (synthetic-v1 contract); got SQL {cur.copy_sql!r}"
        )
        assert len(cur.written_rows[0]) == 8, (
            "default copy_rows must write an 8-field tuple (synthetic-v1 "
            f"contract); got {len(cur.written_rows[0])} fields"
        )

    def test_with_text_embedding_includes_512_vector(self) -> None:
        """``include_text_embedding=True`` → 9-column COPY incl. a 512-d vector."""
        conn = _FakeConn()
        row = _fashion200k_like_row()
        written = _copy_rows()(conn, [row], include_text_embedding=True)
        assert written == 1
        cur = conn.cur
        assert cur.copy_sql is not None
        assert "text_embedding" in cur.copy_sql, (
            "include_text_embedding=True must add text_embedding to the COPY "
            f"column list; got SQL {cur.copy_sql!r}"
        )
        tuple_written = cur.written_rows[0]
        assert len(tuple_written) == 9, (
            f"include_text_embedding=True must write a 9-field tuple; got "
            f"{len(tuple_written)} fields"
        )
        # The last field is the text_embedding formatted as a pgvector literal
        # ("[v1,v2,...]" text format, same as the image embedding field).
        text_field = tuple_written[-1]
        assert isinstance(text_field, str), (
            f"text_embedding must be COPY-encoded as a pgvector text literal; "
            f"got {type(text_field)!r}"
        )
        assert text_field.startswith("[") and text_field.endswith("]"), (
            f"text_embedding literal must be bracketed pgvector text; got "
            f"{text_field!r}"
        )
        assert text_field.count(",") == 511, (
            "the text_embedding literal must encode exactly 512 components "
            f"(511 commas); got {text_field.count(',')} commas"
        )


# ---------------------------------------------------------------------------
# — source dispatch.
#
# `[catalog.source]` selects the row generator: "synthetic-v1" routes to the
# synthetic generator; "fashion200k" routes to the fashion200k loader (lazily
# imported from avsa_api — which is NOT on the repo-root test path, so the
# dispatch's loader hook is monkeypatched here at its boundary); an unknown
# source raises a clear ValueError.
#
# Expected surface: a `rows_for_source(source, *, count, ...)` dispatch in
# catalog_seed that returns/yields the row dicts for the chosen source.
# ---------------------------------------------------------------------------


def _has_dispatch() -> bool:
    return hasattr(catalog_seed, "rows_for_source")


def _require_dispatch() -> None:
    if not _has_dispatch():
        pytest.fail(
            "catalog_seed.rows_for_source dispatch not implemented yet — expected "
            "during 2A-i pre-implementation. Implement per "
            "issues/063-fashion200k-seeder-loader.md (source selects synthetic-v1 "
            "vs fashion200k)."
        )


class TestSourceDispatch:
    def test_synthetic_v1_uses_synthetic_product(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _require_dispatch()
        seen: list[int] = []
        real = catalog_seed.synthetic_product

        def _spy(index: int) -> dict[str, Any]:
            seen.append(index)
            return real(index)

        monkeypatch.setattr(catalog_seed, "synthetic_product", _spy)
        rows = list(_rows_for_source()("synthetic-v1", count=3))
        assert seen == [0, 1, 2], (
            f"synthetic-v1 must route to synthetic_product for each index; saw {seen!r}"
        )
        assert len(rows) == 3, f"expected 3 synthetic rows; got {len(rows)}"

    def test_fashion200k_uses_loader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _require_dispatch()
        sentinel_rows: list[dict[str, Any]] = [
            {"title": "sentinel", "category": "dress"}
        ]
        calls: list[dict[str, object]] = []

        def _fake_loader(*args: object, **kwargs: object) -> list[dict[str, Any]]:
            calls.append({"args": args, "kwargs": kwargs})
            return sentinel_rows

        # The dispatch must expose a single seam that yields the fashion200k
        # rows (it lazily imports avsa_data.catalog_fashion200k.fashion200k_rows
        # behind this name). Patching it proves the dispatch *routes* there
        # without requiring avsa_api on the repo-root test path.
        if not hasattr(catalog_seed, "_fashion200k_rows"):
            pytest.fail(
                "catalog_seed must expose a `_fashion200k_rows` seam that the "
                "dispatch calls for source='fashion200k' (lazily importing "
                "avsa_data.catalog_fashion200k); not implemented yet."
            )
        monkeypatch.setattr(catalog_seed, "_fashion200k_rows", _fake_loader)
        rows = list(_rows_for_source()("fashion200k", count=5))
        assert calls, "fashion200k source must route to the fashion200k loader seam"
        assert rows == sentinel_rows, (
            "rows_for_source must yield the fashion200k loader's rows for "
            f"source='fashion200k'; got {rows!r}"
        )

    def test_unknown_source_raises_valueerror(self) -> None:
        _require_dispatch()
        with pytest.raises(ValueError, match="(?i)source") as excinfo:
            list(_rows_for_source()("does-not-exist", count=1))
        assert "does-not-exist" in str(excinfo.value), (
            f"the error must name the unknown source value; got {str(excinfo.value)!r}"
        )


# ---------------------------------------------------------------------------
#  TR3 — turnkey seed: `--embedding-artifact` defaults from
# `[catalog] embedding_artifact` so `just seed-catalog` (and stack-up's
# seed-on-empty) run with no flag against the committed LFS artifact.
# ---------------------------------------------------------------------------


class TestEmbeddingArtifactConfigDefault:
    def test_load_config_reads_embedding_artifact(self, tmp_path: Path) -> None:
        toml = tmp_path / "avsa.toml"
        toml.write_text(
            '[catalog]\nseed_count = 5000\nsource = "fashion200k"\n'
            'embedding_artifact = "embeddings/abc123"\n',
            encoding="utf-8",
        )
        cfg = catalog_seed.load_config(toml)
        assert cfg.embedding_artifact == "embeddings/abc123"

    def test_load_config_embedding_artifact_optional(self, tmp_path: Path) -> None:
        # Absent key → None (synthetic-v1 never needs an artifact); the loader
        # must not raise KeyError for the optional field.
        toml = tmp_path / "avsa.toml"
        toml.write_text(
            '[catalog]\nseed_count = 10\nsource = "synthetic-v1"\n',
            encoding="utf-8",
        )
        cfg = catalog_seed.load_config(toml)
        assert cfg.embedding_artifact is None

    def test_committed_config_sets_embedding_artifact(self) -> None:
        # The committed config must carry the turnkey default so `just
        # seed-catalog` needs no --embedding-artifact (#090 TR3).
        cfg = catalog_seed.load_config(CONFIG)
        assert cfg.embedding_artifact, (
            "config/avsa.toml [catalog] embedding_artifact must be set so "
            "`just seed-catalog` is turnkey (#090 TR3)"
        )


def _load_seed_script() -> Any:
    """Import the hyphenated ``scripts/seed-catalog.py`` as a module.

    The filename can't be imported with a plain ``import``; load it by path so
    the ``_rows_for_seed`` artifact-resolution logic is unit-testable. Import is
    side-effect-free (psycopg is imported lazily inside ``main``).
    """
    spec = importlib.util.spec_from_file_location("_seed_catalog_script", SEED_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRowsForSeedArtifactResolution:
    """``scripts/seed-catalog.py::_rows_for_seed`` artifact-path resolution."""

    def _args(self, **overrides: Any) -> argparse.Namespace:
        defaults: dict[str, Any] = {
            "embedding_artifact": None,
            "manifest": Path("/nonexistent/manifest.json"),
            "config": CONFIG,
            "embed_url": "http://localhost:8001/embed",
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_fashion200k_without_artifact_raises_clear_error(self) -> None:
        # Neither --embedding-artifact nor [catalog] embedding_artifact → a
        # clear ValueError before any DB/backend work (bug #2 from #084 accept).
        mod = _load_seed_script()
        cfg = catalog_seed.SeedConfig(
            seed_count=5, source="fashion200k", embedding_artifact=None
        )
        with pytest.raises(ValueError, match="(?i)embedding artifact"):
            mod._rows_for_seed(cfg, 5, self._args(), use_stub=False)

    def test_defaults_artifact_from_config_when_flag_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Flag absent but config set → the config value is used as the storage
        # key (turnkey). Stub the backend + the row loader so the test is
        # hermetic (no avsa_api, no DB) and capture the artifact_dir routed.
        mod = _load_seed_script()
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(mod, "_build_storage_backend", lambda _p: object())
        captured: dict[str, Any] = {}

        def _fake_rows(source: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
            captured["source"] = source
            captured["artifact_dir"] = kwargs.get("artifact_dir")
            return iter([])

        monkeypatch.setattr(catalog_seed, "rows_for_source", _fake_rows)
        cfg = catalog_seed.SeedConfig(
            seed_count=5, source="fashion200k", embedding_artifact="embeddings/cfgkey"
        )
        rows, include_text = mod._rows_for_seed(
            cfg, 5, self._args(manifest=manifest), use_stub=False
        )
        list(rows)
        assert captured["artifact_dir"] == Path("embeddings/cfgkey"), (
            "with no --embedding-artifact, the [catalog] embedding_artifact "
            f"config default must be used; routed {captured.get('artifact_dir')!r}"
        )
        assert include_text is True, "fashion200k must seed the text_embedding column"

    def test_flag_overrides_config_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        mod = _load_seed_script()
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(mod, "_build_storage_backend", lambda _p: object())
        captured: dict[str, Any] = {}

        def _fake_rows(_source: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
            captured["artifact_dir"] = kwargs.get("artifact_dir")
            return iter([])

        monkeypatch.setattr(catalog_seed, "rows_for_source", _fake_rows)
        cfg = catalog_seed.SeedConfig(
            seed_count=5, source="fashion200k", embedding_artifact="embeddings/cfgkey"
        )
        rows, _ = mod._rows_for_seed(
            cfg,
            5,
            self._args(
                manifest=manifest, embedding_artifact=Path("embeddings/flagkey")
            ),
            use_stub=False,
        )
        list(rows)
        assert captured["artifact_dir"] == Path("embeddings/flagkey"), (
            "an explicit --embedding-artifact must override the config default; "
            f"routed {captured.get('artifact_dir')!r}"
        )


# ---------------------------------------------------------------------------
#  TR5 — the setup runbook documents Git-LFS seed-readiness so a
# fresh clone seeds without an out-of-band Fashion200k download.
# ---------------------------------------------------------------------------


class TestSetupRunbookDocumentsLfs:
    def test_runbook_documents_lfs_pull_and_seed_readiness(self) -> None:
        assert SETUP_RUNBOOK.is_file(), f"missing {SETUP_RUNBOOK}"
        text = SETUP_RUNBOOK.read_text(encoding="utf-8")
        assert re.search(r"git lfs pull", text), (
            "setup runbook must document that `just setup` runs `git lfs pull`"
        )
        assert re.search(r"(?i)seed-ready|turnkey", text), (
            "setup runbook must state the clone is seed-ready after `just setup`"
        )

    def test_runbook_records_lfs_budget(self) -> None:
        # TR5: record the LFS storage/bandwidth budget; flag that CI doesn't pull.
        text = SETUP_RUNBOOK.read_text(encoding="utf-8")
        assert re.search(r"526M|1GB|bandwidth", text), (
            "setup runbook must record the Git-LFS storage/bandwidth budget"
        )
        assert re.search(r"(?i)ci does not pull|lfs: false|TR4", text), (
            "setup runbook must note CI does not pull LFS (bandwidth stays low)"
        )
