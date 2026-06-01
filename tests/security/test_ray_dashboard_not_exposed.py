"""Mitigation guard for CVE-2023-48022 (Ray Dashboard RCE).

The Ray Dashboard exposes a remote-code-execution attack surface and
upstream has marked it won't-fix as a design choice. AVSA's allowlist
(`.pip-audit-allowlist.toml`) carries the entry under `permanent` with
mitigation = "AVSA does not run the dashboard". This test enforces that
mitigation: it grep-walks the project tree for any code or config that
would *enable* the dashboard, and fails if it finds one.

If the project ever needs the dashboard (e.g. for offline debugging in a
network-isolated env), drop `include_dashboard=True` in a single,
audited place AND remove the permanent allowlist entry — the upstream
won't-fix means we'd need a different mitigation (network policy,
auth proxy, etc.) at that point.
"""

from pathlib import Path

# Patterns that turn the dashboard ON. The defaults across the ray API
# surface are dashboard-off, so we only need to catch explicit enables.
DASHBOARD_ENABLERS = (
    "include_dashboard=True",
    "include_dashboard = True",
    "ray dashboard",  # CLI invocation
    "ray-dashboard",
)

# Files we don't audit: vendored deps, build outputs, this test itself
# (it contains the patterns as data), and the allowlist file (which
# documents the CVE and would otherwise self-trigger).
EXCLUDED_DIRS = {".venv", ".git", "node_modules", "dist", "build", ".terraform"}
EXCLUDED_FILES = {
    "test_ray_dashboard_not_exposed.py",
    ".pip-audit-allowlist.toml",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _candidate_files() -> list[Path]:
    root = _project_root()
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.name in EXCLUDED_FILES:
            continue
        if path.suffix not in {".py", ".sh", ".yaml", ".yml", ".toml", ".tf"}:
            continue
        candidates.append(path)
    return candidates


def test_ray_dashboard_is_not_enabled_anywhere() -> None:
    offenders: list[tuple[Path, int, str]] = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in DASHBOARD_ENABLERS:
                if pattern in line:
                    offenders.append((path, lineno, line.strip()))

    assert not offenders, (
        "Ray dashboard enabler(s) found — CVE-2023-48022 mitigation "
        "violated. Either remove the enabler or replace the permanent "
        "allowlist entry with an alternative mitigation:\n"
        + "\n".join(f"  {p}:{ln}: {src}" for p, ln, src in offenders)
    )
