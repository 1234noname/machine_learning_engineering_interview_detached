"""Generate config/avsa.toml from config/avsa.base.toml + environment overlay.

The output file is gitignored — readers always read exactly one file (avsa.toml).
Source files (avsa.base.toml, avsa.local.toml, avsa.prod.toml) are committed;
the generated avsa.toml is derived and never committed.

Usage (called by `just config-gen <env>`):
    uv run python scripts/config-gen.py ci     # base only — CI-safe defaults
    uv run python scripts/config-gen.py local  # base + config/avsa.local.toml
    uv run python scripts/config-gen.py prod   # base + config/avsa.prod.toml
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


def _deep_merge(base: dict, overlay: dict) -> dict:  # type: ignore[type-arg]
    """Recursively merge overlay onto base. Overlay wins at every level."""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = repr(v)
        if "." not in s and "e" not in s.lower():
            s += ".0"
        return s
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, list):
        items = ", ".join(_toml_value(item) for item in v)
        return f"[{items}]"
    raise TypeError(f"unsupported TOML type: {type(v).__name__}")


def _emit_table(data: dict, prefix: str, lines: list[str]) -> None:  # type: ignore[type-arg]
    """Emit scalars first, then sub-tables (TOML section ordering requirement)."""
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")
    for key, value in data.items():
        if isinstance(value, dict):
            section = f"{prefix}.{key}"
            lines.append("")
            lines.append(f"[{section}]")
            _emit_table(value, section, lines)


def _dump_toml(data: dict) -> str:  # type: ignore[type-arg]
    lines: list[str] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append("")
            lines.append(f"[{key}]")
            _emit_table(value, key, lines)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate config/avsa.toml from base + env overlay"
    )
    parser.add_argument(
        "env", choices=["ci", "local", "prod"], help="Target environment"
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root directory (default: detected from script path)",
    )
    args = parser.parse_args()

    if args.repo_root:
        repo_root = Path(args.repo_root)
    else:
        repo_root = Path(__file__).resolve().parent.parent
    base_path = repo_root / "config" / "avsa.base.toml"
    output_path = repo_root / "config" / "avsa.toml"

    if not base_path.exists():
        print(f"ERROR: base config not found: {base_path}", file=sys.stderr)
        return 1

    with base_path.open("rb") as f:
        config = tomllib.load(f)

    if args.env != "ci":
        overlay_path = repo_root / "config" / f"avsa.{args.env}.toml"
        if overlay_path.exists():
            with overlay_path.open("rb") as f:
                overlay = tomllib.load(f)
            config = _deep_merge(config, overlay)
            print(f"==> merged {overlay_path.name} onto base")
        else:
            print(
                f"WARN: no overlay at {overlay_path}, using base only",
                file=sys.stderr,
            )

    with output_path.open("w") as f:
        f.write(_dump_toml(config))

    print(f"==> {output_path} generated (env={args.env})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
