"""Runtime configuration loader — reads config/avsa.toml from the repo root."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class APIConfig:
    rate_limit_rpm: int
    max_upload_bytes: int
    db_url: str = ""


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "config" / "avsa.toml").exists():
            return parent
    raise FileNotFoundError("config/avsa.toml not found in any parent directory")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay into base; overlay keys win at every level.

    Returns a new dict — base and overlay are never mutated.
    """
    merged = dict(base)
    for key, overlay_value in overlay.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            merged[key] = _deep_merge(base_value, overlay_value)
        else:
            merged[key] = overlay_value
    return merged


def _find_overlay(root: Path) -> Path | None:
    """Return path to config/avsa.{AVSA_PROFILE}.toml if AVSA_PROFILE is set and file exists."""
    profile = os.environ.get("AVSA_PROFILE", "")
    if not profile:
        return None
    candidate = root / "config" / f"avsa.{profile}.toml"
    return candidate if candidate.exists() else None


def load_config_raw() -> dict[str, Any]:
    """Return the parsed ``config/avsa.toml`` deep-merged with any profile overlay.

    When ``AVSA_PROFILE`` is set (e.g. ``prod``), the loader looks for
    ``config/avsa.{AVSA_PROFILE}.toml`` and deep-merges it on top of the base.
    Overlay keys win at every nesting level; base keys not mentioned in the
    overlay are preserved.  If the overlay file does not exist the loader
    silently falls back to base-only — no exception is raised.

    The storage backend factory (``avsa_core.storage._build_backend``) needs the
    nested ``[storage]`` table verbatim — the ``APIConfig`` dataclass only
    projects the API-relevant keys, so it can't drive backend construction.
    """
    root = _repo_root()
    path = root / "config" / "avsa.toml"
    with open(path, "rb") as f:
        raw: dict[str, Any] = tomllib.load(f)
    overlay_path = _find_overlay(root)
    if overlay_path is not None:
        with open(overlay_path, "rb") as f:
            overlay: dict[str, Any] = tomllib.load(f)
        raw = _deep_merge(raw, overlay)
    return raw


def load_config() -> APIConfig:
    raw = load_config_raw()
    api = raw.get("api", {})
    return APIConfig(
        rate_limit_rpm=api.get("rate_limit", {}).get("requests_per_minute", 60),
        max_upload_bytes=api.get("max_upload_bytes", 10_485_760),
        db_url=raw.get("db", {}).get("url", ""),
    )
