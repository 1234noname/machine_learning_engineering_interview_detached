#!/usr/bin/env bash
# check-doc-drift.sh — fail if README.md's setup section references a `just`
# recipe that doesn't exist in the justfile.
#
# Scope: only `just <recipe>` calls inside fenced code blocks (```…```) under
# the first `## Quick start` / `## Setup` / `## Getting started` heading. The
# English word `just` in prose is ignored.
#
# Env overrides (for tests):
#   README_PATH    — path to README; defaults to ./README.md
#   JUSTFILE_PATH  — path to justfile; defaults to ./justfile
set -euo pipefail

README_PATH="${README_PATH:-README.md}"
JUSTFILE_PATH="${JUSTFILE_PATH:-justfile}"

if [[ ! -f "$README_PATH" ]]; then
    echo "doc-drift: README not found at $README_PATH" >&2
    exit 2
fi
if [[ ! -f "$JUSTFILE_PATH" ]]; then
    echo "doc-drift: justfile not found at $JUSTFILE_PATH" >&2
    exit 2
fi

# Extract the setup section: the first `## (Quick start|Setup|Getting started)`
# heading up to (but excluding) the next `## ` heading.
section=$(awk '
    /^##[[:space:]]+(Quick start|Setup|Getting started)/ {capture=1; next}
    capture && /^##[[:space:]]/ {capture=0}
    capture {print}
' "$README_PATH")

if [[ -z "$section" ]]; then
    echo "doc-drift: README has no Quick start / Setup / Getting started section" >&2
    exit 2
fi

# Inside that section, grab fenced code-block bodies only.
fenced=$(awk '
    /^```/ { fenced = !fenced; next }
    fenced { print }
' <<<"$section")

# Recipe references: every `just <name>` where <name> starts with a letter.
# Exclude `just --…` flag invocations (not a recipe call).
referenced=$(grep -oE '\bjust[[:space:]]+[A-Za-z][A-Za-z0-9-]*' <<<"$fenced" \
    | awk '{print $2}' \
    | sort -u || true)

# Recipe names known to the justfile. Prefer `just --summary` when available
# (single source of truth), fall back to grep on the file so the CI step
# doesn't depend on `just` being on PATH if some future runner skips it.
if command -v just >/dev/null 2>&1; then
    recipes=$(just --justfile "$JUSTFILE_PATH" --summary 2>/dev/null | tr ' ' '\n' | sort -u)
else
    recipes=$(grep -oE '^[A-Za-z][A-Za-z0-9-]*' "$JUSTFILE_PATH" | sort -u)
fi

missing=()
while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    if ! grep -qxF "$name" <<<"$recipes"; then
        missing+=("$name")
    fi
done <<<"$referenced"

if (( ${#missing[@]} > 0 )); then
    echo "doc-drift: README setup section references just recipes that don't exist:" >&2
    for m in "${missing[@]}"; do
        echo "  - $m" >&2
    done
    exit 1
fi

echo "doc-drift: OK"
