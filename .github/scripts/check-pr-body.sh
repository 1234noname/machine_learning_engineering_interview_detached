#!/usr/bin/env bash
# Validate a PR body against the AVSA spec.
#
# Usage: .github/scripts/check-pr-body.sh <body-file>
#
# Exits 0 if compliant; non-zero with a clear message naming each missing
# section. Used by:
#   - .github/workflows/pr-form-check.yml (server-side enforcement on PRs)
#   - tests/fixtures/pr-body-spec/run-all.sh (fixture verification)
#
# The required-sections spec is shared by .github/PULL_REQUEST_TEMPLATE.md
# (provides them) and .githooks/pre-push (checks for non-empty body). If
# the spec changes, all three places update in the same PR — see
# docs/runbooks/pr-body-quality.md "Updating the PR body spec".

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <body-file>" >&2
    exit 2
fi

body_file="$1"
if [[ ! -f "$body_file" ]]; then
    echo "error: file not found: $body_file" >&2
    exit 2
fi

errors=()

# Reference: an issue link, story id, or substrate track tag.
if ! grep -qE '#[0-9]+|STORY-[0-9]+|Track [A-E]' "$body_file"; then
    errors+=("missing reference: PR body must include an issue link (e.g. '#42'), story id (''), or substrate track tag ('Track A').")
fi

# Definition of Done section (case-insensitive).
if ! grep -qiE '^## Definition of Done\s*$' "$body_file"; then
    errors+=("missing section: '## Definition of Done' must appear as a top-level heading.")
fi

# Out of scope section (case-insensitive).
if ! grep -qiE '^## Out of scope\s*$' "$body_file"; then
    errors+=("missing section: '## Out of scope' must appear as a top-level heading.")
fi

if (( ${#errors[@]} > 0 )); then
    echo "PR body does not match the required form:"
    for e in "${errors[@]}"; do
        echo "  - $e"
    done
    echo ""
    echo "See .github/PULL_REQUEST_TEMPLATE.md for the canonical form."
    exit 1
fi

echo "PR body is compliant."
