#!/usr/bin/env bash
# pip-audit wrapper — reads .pip-audit-allowlist.toml and feeds the
# union of permanent + temporary IDs to `pip-audit` as `--ignore-vuln`
# flags. Single source of truth for the allowlist (was duplicated
# between .pre-commit-config.yaml and .github/workflows/ci.yml until
# #13).
#
# Usage:
#   scripts/pip-audit.sh                       # active-venv audit (local default)
#   scripts/pip-audit.sh -r requirements.txt   # audit a requirements file (CI)
#   scripts/pip-audit.sh --strict              # extra args forwarded to pip-audit
#
# Behaviour:
#   - Schema-validates each allowlist entry (id + package + mitigation
#     for permanent; id + package + fix_version + tracked_by for
#     temporary). Missing required fields => non-zero exit, no audit
#     run. The schema enforcement is the whole point — without it,
#     people drop bare CVE ids in and the rationale rots.
#   - Forwards all CLI args to pip-audit unchanged.

set -euo pipefail

ALLOWLIST="${PIP_AUDIT_ALLOWLIST:-.pip-audit-allowlist.toml}"

if [[ ! -f "$ALLOWLIST" ]]; then
    echo "pip-audit wrapper: allowlist not found at $ALLOWLIST" >&2
    exit 2
fi

# Use uv-managed Python (3.12 has tomllib + datetime in stdlib).
# Schema-validate, check expiry, emit one CVE id per line.
ids=$(uv run python - <<PY
import sys
import tomllib
from datetime import date

with open("$ALLOWLIST", "rb") as f:
    data = tomllib.load(f)

errors: list[str] = []
ids: list[str] = []
today = date.today()

for i, entry in enumerate(data.get("permanent", [])):
    for required in ("id", "package", "mitigation"):
        if required not in entry:
            errors.append(f"permanent[{i}] missing '{required}'")
    if "id" in entry:
        ids.append(entry["id"])

for i, entry in enumerate(data.get("temporary", [])):
    for required in ("id", "package", "fix_version", "tracked_by"):
        if required not in entry:
            errors.append(f"temporary[{i}] missing '{required}'")
    if "not_after" in entry:
        try:
            expiry = date.fromisoformat(entry["not_after"])
        except ValueError:
            errors.append(f"temporary[{i}] not_after '{entry['not_after']}' is not a valid ISO date (YYYY-MM-DD)")
        else:
            if today > expiry:
                ref = entry.get("tracked_by", "no tracked_by set")
                errors.append(
                    f"temporary[{i}] (id={entry.get('id', '?')}) expired: "
                    f"not_after {entry['not_after']} has passed — "
                    f"upgrade the dep or remove the entry (tracked_by: {ref})"
                )
    if "id" in entry:
        ids.append(entry["id"])

if errors:
    print("pip-audit allowlist errors:", file=sys.stderr)
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    sys.exit(2)

for cid in ids:
    print(cid)
PY
)

ignore_args=()
while IFS= read -r id; do
    [[ -z "$id" ]] && continue
    ignore_args+=(--ignore-vuln "$id")
done <<< "$ids"

# `${arr[@]+"${arr[@]}"}` is the bash idiom for "expand if non-empty,
# else nothing" — needed because `set -u` would otherwise trip on an
# empty array under bash 3.x (macOS).
exec uv run pip-audit ${ignore_args[@]+"${ignore_args[@]}"} "$@"
