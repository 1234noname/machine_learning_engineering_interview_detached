#!/usr/bin/env bash
# Draft a PR body by extracting sections from the branch's issue file(s)
# (issues/NNN-*.md) and reshaping them into the PULL_REQUEST_TEMPLATE.md
# layout. Output goes to stdout; pipe to `gh pr create --body-file -`.
#
# Usage:
#   scripts/draft-pr-body.sh                          # auto-detect from branch
#   scripts/draft-pr-body.sh --issue-file <path>      # explicit override (repeatable)
#   scripts/draft-pr-body.sh --no-friction            # skip friction-log detection
#
# Supports single-issue branches (phaseN/issue-NNN) and multi-issue branches
# (phaseN/issues-NNN-MMM[-PPP...]). Pass --issue-file more than once to
# override with explicit paths for multiple issues.
# Mechanical only — no LLM, no semantic compression. The author tightens
# the `## What` and ticks DoD items where actually done before submitting.
# See docs/runbooks/pr-body-quality.md "Drafting a PR body with `just pr`".

set -euo pipefail

ISSUE_FILES=()
NO_FRICTION=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --issue-file)
            ISSUE_FILES+=("$2")
            shift 2
            ;;
        --no-friction)
            NO_FRICTION=1
            shift
            ;;
        -h|--help)
            sed -n '2,16p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "usage: $0 [--issue-file <path>] [--no-friction]" >&2
            exit 2
            ;;
    esac
done

# Auto-detect issue file(s) from the current branch when no override.
if [[ ${#ISSUE_FILES[@]} -eq 0 ]]; then
    branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || {
        echo "error: not in a git repo (use --issue-file <path> to override)" >&2
        exit 1
    }
    # Match single-issue (phaseN/issue-NNN) or multi-issue (phaseN/issues-NNN-MMM[-PPP...]).
    if [[ "$branch" =~ issues?-([0-9]+(-[0-9]+)*) ]]; then
        numbers_str="${BASH_REMATCH[1]}"
        IFS='-' read -ra issue_nums <<< "$numbers_str"
    else
        echo "error: branch '$branch' does not match the phaseN/issue-NNN or phaseN/issues-NNN-MMM convention." >&2
        echo "       Use --issue-file <path> to override." >&2
        exit 1
    fi

    shopt -s nullglob
    for num in "${issue_nums[@]}"; do
        # Match the parent issue file (lowercase first letter after dash);
        # sub-issues like 003-A-... use uppercase and are deliberately skipped.
        matches=( "issues/${num}-"[[:lower:]]*.md )
        if [[ ${#matches[@]} -eq 0 ]]; then
            echo "error: no parent issue file matching issues/${num}-[[:lower:]]*.md" >&2
            exit 1
        fi
        ISSUE_FILES+=("${matches[0]}")
    done
    shopt -u nullglob
fi

for f in "${ISSUE_FILES[@]}"; do
    if [[ ! -f "$f" ]]; then
        echo "error: issue file not found: $f" >&2
        exit 1
    fi
done

# Extract the body of a top-level (## Heading) section from a given file,
# exclusive of the heading line, terminating at the next top-level heading.
extract_section() {
    local heading="$1"
    local file="$2"
    awk -v h="$heading" '
        BEGIN { pat = "^## " h "[[:space:]]*$" }
        $0 ~ pat { in_section=1; next }
        /^## [^#]/ { in_section=0 }
        in_section
    ' "$file"
}

# Trim leading and trailing blank lines from stdin.
trim_blank() {
    awk '
        /^[[:space:]]*$/ {
            if (!seen) next
            buf = buf $0 "\n"
            next
        }
        {
            if (length(buf)) { printf "%s", buf; buf = "" }
            print
            seen = 1
        }
    '
}

# First paragraph of stdin (up to the first blank line after non-blank content).
first_paragraph() {
    awk '
        BEGIN { skip = 1 }
        skip && /^[[:space:]]*$/ { next }
        skip { skip = 0 }
        /^[[:space:]]*$/ { exit }
        { print }
    '
}

# If stdin is whitespace-only, emit the fallback; otherwise pass through.
default_or() {
    local fallback="$1"
    local content
    content=$(cat)
    if [[ -z "${content//[[:space:]]/}" ]]; then
        echo "$fallback"
    else
        echo "$content"
    fi
}

# Concatenate a section across all issue files, trimming blanks.
# Single file: plain content. Multiple files: sub-headed by issue number.
concat_section() {
    local heading="$1"
    if [[ ${#ISSUE_FILES[@]} -eq 1 ]]; then
        extract_section "$heading" "${ISSUE_FILES[0]}" | trim_blank
        return
    fi
    local first=true
    for f in "${ISSUE_FILES[@]}"; do
        local num
        num=$(basename "$f" | cut -d- -f1)
        if [[ "$first" == true ]]; then
            first=false
        else
            echo ""
        fi
        printf '### Issue %s\n\n' "$num"
        extract_section "$heading" "$f" | trim_blank
    done
}

# Detect whether docs/friction.md changed in the branch's commits vs origin/main.
detect_friction() {
    if [[ "$NO_FRICTION" == "1" ]]; then
        echo "N/A"
        return
    fi
    local merge_base
    if merge_base=$(git merge-base origin/main HEAD 2>/dev/null); then
        if git diff --name-only "$merge_base"...HEAD 2>/dev/null | grep -qF docs/friction.md; then
            echo "[docs/friction.md](docs/friction.md)"
            return
        fi
    fi
    echo "N/A"
}

# Build the summary/What block.
# Single file: first paragraph only. Multiple files: one bold-prefixed line per issue.
if [[ ${#ISSUE_FILES[@]} -eq 1 ]]; then
    summary_para=$(extract_section "Summary" "${ISSUE_FILES[0]}" | first_paragraph)
else
    summary_para=""
    for f in "${ISSUE_FILES[@]}"; do
        num=$(basename "$f" | cut -d- -f1)
        para=$(extract_section "Summary" "$f" | first_paragraph)
        [[ -n "$summary_para" ]] && summary_para+=$'\n\n'
        summary_para+="**#${num}**: ${para}"
    done
fi

story_track=$(concat_section "Story / Track")
dod=$(concat_section "Definition of Done")
out_of_scope=$(concat_section "Out of scope" | default_or "- TODO")
skills=$(concat_section "Skills used" | default_or "- None")
friction=$(detect_friction)

cat <<EOF
## What

<!-- TODO: tighten to 1–2 sentences. Drafted from issue Summary: -->
$summary_para

## Story / Track

$story_track

## Definition of Done

$dod

## Out of scope

$out_of_scope

## Skills used

$skills

## Friction log

- $friction
EOF
