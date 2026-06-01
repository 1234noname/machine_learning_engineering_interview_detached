"""Tests for AVSA_PROFILE overlay-merge behaviour in avsa_core.config."""

from pathlib import Path

import pytest

from avsa_core.config import load_config_raw


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _make_base_toml(tmp_path: Path) -> Path:
    """Write a minimal base avsa.toml and return its parent config dir."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_toml(
        config_dir / "avsa.toml",
        """\
[api]
batcher_url = "http://localhost:8081"

[api.rate_limit]
requests_per_minute = 60
burst = 10
""",
    )
    return tmp_path


def test_base_only_no_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When AVSA_PROFILE is unset, load_config_raw returns base config only."""
    root = _make_base_toml(tmp_path)
    # Patch _repo_root so config.py finds our temp tree
    monkeypatch.delenv("AVSA_PROFILE", raising=False)
    import avsa_core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_repo_root", lambda: root)

    raw = load_config_raw()
    assert raw["api"]["batcher_url"] == "http://localhost:8081"


def test_overlay_overrides_flat_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An overlay TOML's flat key wins over the base value."""
    root = _make_base_toml(tmp_path)
    _write_toml(
        tmp_path / "config" / "avsa.staging.toml",
        """\
[api]
batcher_url = "http://prod-batcher:8001"
""",
    )
    monkeypatch.setenv("AVSA_PROFILE", "staging")
    import avsa_core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_repo_root", lambda: root)

    raw = load_config_raw()
    assert raw["api"]["batcher_url"] == "http://prod-batcher:8001"


def test_overlay_merges_nested_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Overlay updates only the keys it specifies; other nested keys survive."""
    root = _make_base_toml(tmp_path)
    _write_toml(
        tmp_path / "config" / "avsa.prod.toml",
        """\
[api.rate_limit]
requests_per_minute = 120
""",
    )
    monkeypatch.setenv("AVSA_PROFILE", "prod")
    import avsa_core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_repo_root", lambda: root)

    raw = load_config_raw()
    # Overlay wins on its key
    assert raw["api"]["rate_limit"]["requests_per_minute"] == 120
    # Sibling key not in overlay is preserved
    assert raw["api"]["rate_limit"]["burst"] == 10


def test_overlay_missing_file_falls_back_to_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When AVSA_PROFILE is set but no overlay file exists, fall back to base silently."""
    root = _make_base_toml(tmp_path)
    monkeypatch.setenv("AVSA_PROFILE", "ghost")
    import avsa_core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_repo_root", lambda: root)

    # Must not raise; must return base config
    raw = load_config_raw()
    assert raw["api"]["batcher_url"] == "http://localhost:8081"


def test_overlay_precedence_nested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Overlay adds its own keys while leaving unrelated base keys untouched."""
    root = _make_base_toml(tmp_path)
    _write_toml(
        tmp_path / "config" / "avsa.custom.toml",
        """\
[api]
batcher_url = "http://other"
""",
    )
    monkeypatch.setenv("AVSA_PROFILE", "custom")
    import avsa_core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_repo_root", lambda: root)

    raw = load_config_raw()
    # Overlay key wins
    assert raw["api"]["batcher_url"] == "http://other"
    # Base nested key that overlay did not touch is preserved
    assert raw["api"]["rate_limit"]["requests_per_minute"] == 60
