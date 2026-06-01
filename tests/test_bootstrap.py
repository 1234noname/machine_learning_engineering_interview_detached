"""Tests for the bootstrap slice: per-step elapsed timing in the justfile
`setup`/`bootstrap` recipe, the doc-drift guard (README setup section's
`just <recipe>` references must exist as real recipes), and the structural
shape of the weekly cold-clone workflow (`bootstrap.yml`).

Written before implementation (test-first). Each test names the
specific failure mode it gates.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JUSTFILE = REPO_ROOT / "justfile"
README = REPO_ROOT / "README.md"
BOOTSTRAP_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "bootstrap.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
DOC_DRIFT_SCRIPT = REPO_ROOT / "scripts" / "check-doc-drift.sh"


def _just_recipe_names() -> set[str]:
    """Return the recipe names defined in the local justfile.

    Uses `just --summary` when available; falls back to parsing the justfile
    directly so this helper works in CI environments where `just` is not on PATH
    (e.g. the standard test-unit job, which does not install `just`).
    """
    if shutil.which("just"):
        out = subprocess.run(
            ["just", "--summary"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return set(out.stdout.split())

    # Fallback: parse recipe names from the justfile source.
    # Recipe line: name, optional args, then `:` (not `:=`).
    names: set[str] = set()
    for line in JUSTFILE.read_text().splitlines():
        if not line or line.startswith("#") or line.startswith("set "):
            continue
        m = re.match(r"^([a-z][a-z0-9_-]*)(?:\s[^:]*)?:(?!=)", line)
        if m:
            names.add(m.group(1))
    return names


# ----- per-step elapsed timing in justfile ---------------------------------


class TestBootstrapTiming:
    """The `setup`/`bootstrap` recipe must print `[<step>] elapsed: <N>s`
    after each step. We assert against the recipe source because exercising
    the recipe end-to-end requires brew/gcloud/network, which CI runners
    can't do per-PR."""

    def test_setup_recipe_uses_bash_shebang(self) -> None:
        """Per-step timing needs a single bash process so `$SECONDS` survives
        across steps. The recipe must declare a bash shebang."""
        body = JUSTFILE.read_text()
        match = re.search(
            r"^setup:\s*\n((?:\s{4}.*\n)+)",
            body,
            re.MULTILINE,
        )
        assert match, "could not locate `setup:` recipe in justfile"
        recipe_body = match.group(1)
        assert "#!/usr/bin/env bash" in recipe_body, (
            "setup recipe must use `#!/usr/bin/env bash` shebang so $SECONDS "
            "persists across steps"
        )

    def test_setup_recipe_prints_elapsed_for_each_step(self) -> None:
        """Each named step in the recipe must emit
        `[<step>] elapsed: <N>s` (we assert the format string template
        appears at least once per step)."""
        body = JUSTFILE.read_text()
        # All step names the timing wrapper should cover. Order tracks the
        # current recipe body — see justfile `setup:` source of truth.
        expected_steps = ["brew", "python", "uv-sync", "gcloud", "hooks"]
        for step in expected_steps:
            pattern = rf"\[{re.escape(step)}\] elapsed:"
            assert re.search(pattern, body), (
                f"setup recipe missing elapsed-time output for step '{step}'; "
                f"expected literal '[{step}] elapsed:' in justfile"
            )


# ----- doc-drift -----------------------------------------------------------


class TestDocDrift:
    """`README.md` setup-section commands must all be real `just` recipes."""

    def test_doc_drift_script_exists_and_is_executable(self) -> None:
        assert DOC_DRIFT_SCRIPT.exists(), (
            f"{DOC_DRIFT_SCRIPT} is missing; doc-drift CI step needs it"
        )
        assert DOC_DRIFT_SCRIPT.stat().st_mode & 0o111, (
            f"{DOC_DRIFT_SCRIPT} must be executable (chmod +x)"
        )

    def test_doc_drift_passes_on_current_repo(self) -> None:
        """The script must exit 0 against the repo as it stands."""
        result = subprocess.run(
            ["bash", str(DOC_DRIFT_SCRIPT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"doc-drift script failed on current repo:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_doc_drift_detects_missing_recipe(self, tmp_path: Path) -> None:
        """When the README mentions `just <missing-recipe>`, the script
        must exit non-zero and name the missing recipe."""
        fake_readme = tmp_path / "README.md"
        fake_readme.write_text(
            "## Setup\n\n```bash\njust definitely-not-a-real-recipe\n```\n"
        )
        # Run the script against the fake README via env override.
        result = subprocess.run(
            ["bash", str(DOC_DRIFT_SCRIPT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
                "README_PATH": str(fake_readme),
            },
        )
        assert result.returncode != 0, (
            "doc-drift script should fail when README references a missing recipe"
        )
        combined = result.stdout + result.stderr
        assert "definitely-not-a-real-recipe" in combined, (
            "failure message must name the missing recipe; got:\n" + combined
        )

    def test_readme_setup_commands_all_resolve_to_real_recipes(self) -> None:
        """Sanity duplicate of the script's invariant, so a developer who
        breaks the README sees a clean pytest failure even before pushing.

        Scoped to the setup section, fenced code blocks only — the English
        word `just` in prose should not register as a recipe reference."""
        text = README.read_text()
        recipes = _just_recipe_names()
        # Setup section runs from the first `## …` heading whose title
        # contains "setup" or "start" up to the next `## ` heading.
        section_match = re.search(
            r"^##\s+(?:Quick start|Setup|Getting started).*?(?=^##\s|\Z)",
            text,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        assert section_match, "README missing a Quick start / Setup section"
        section = section_match.group(0)
        # Extract fenced code blocks only.
        fences = re.findall(r"```[^\n]*\n(.*?)```", section, re.DOTALL)
        joined = "\n".join(fences)
        referenced = {
            m.group(1) for m in re.finditer(r"\bjust\s+([A-Za-z][A-Za-z0-9-]*)", joined)
        }
        missing = referenced - recipes
        assert not missing, (
            f"README references just recipes that don't exist: {sorted(missing)}"
        )


# ----- bootstrap.yml workflow shape ---------------------------------------


class TestBootstrapWorkflow:
    """The weekly cold-clone workflow must exist with the structure the
    issue specifies. We assert against the YAML source rather than running
    the workflow, because workflow execution belongs to GitHub Actions."""

    @pytest.fixture(scope="class")
    def workflow(self) -> str:
        assert BOOTSTRAP_WORKFLOW.exists(), (
            f"{BOOTSTRAP_WORKFLOW} is missing; the weekly cold-clone job needs it"
        )
        return BOOTSTRAP_WORKFLOW.read_text()

    def test_runs_on_ubuntu(self, workflow: str) -> None:
        assert "runs-on: ubuntu-latest" in workflow

    def test_weekly_cron(self, workflow: str) -> None:
        # `0 6 * * 1` = 06:00 UTC every Monday — the polyglot cold-clone cadence
        # . The cron field is quoted in workflow YAML, so allow either style.
        assert re.search(r"""cron:\s*['"]0 6 \* \* 1['"]""", workflow), (
            "bootstrap.yml must schedule cron '0 6 * * 1' (Monday 06:00 UTC)"
        )

    def test_actions_pinned_to_sha(self, workflow: str) -> None:
        """No floating tags. Every `uses:` referring to a third-party action
        must be pinned to a 40-char hex SHA (or be a local `./` action)."""
        uses_lines = re.findall(r"uses:\s*(\S+)", workflow)
        third_party = [u for u in uses_lines if not u.startswith("./")]
        for ref in third_party:
            assert "@" in ref, f"action {ref} missing @ref"
            sha = ref.split("@", 1)[1]
            assert re.fullmatch(r"[0-9a-f]{40}", sha), (
                f"action {ref} must be pinned to a 40-char commit SHA "
                f"(no floating tags like @v4)"
            )

    def test_runs_just_bootstrap_and_just_test(self, workflow: str) -> None:
        assert "just bootstrap" in workflow
        assert "just test" in workflow

    def test_asserts_total_elapsed_under_budget(self, workflow: str) -> None:
        """Total bootstrap+test wall-clock must be asserted ≤ the documented
        budget (1500 s for the polyglot cold-clone). The error message
        must name the actual elapsed seconds so the failing CI log is
        self-explanatory."""
        assert "1500" in workflow, (
            "bootstrap.yml must assert total elapsed ≤ 1500 seconds"
        )
        # The budget message must interpolate the actual elapsed time. The
        # workflow uses bash's ${SECONDS} (and ${BUDGET_SECONDS}); accept either
        # those or an ${ELAPSED}-style variable.
        assert re.search(r"\$\{?(SECONDS|BUDGET_SECONDS|ELAPSED)\}?", workflow), (
            "assertion must print the actual elapsed seconds (e.g. ${SECONDS})"
        )


# ----- lockfile-integrity CI step -----------------------------------------


class TestLockfileIntegrityCIStep:
    def test_ci_workflow_runs_uv_lock_check(self) -> None:
        ci = CI_WORKFLOW.read_text()
        assert "uv lock --check" in ci, (
            "ci.yml must include a step that runs `uv lock --check`"
        )


# ----- doc-drift CI step --------------------------------------------------


class TestDocDriftCIStep:
    def test_ci_workflow_invokes_doc_drift_check(self) -> None:
        ci = CI_WORKFLOW.read_text()
        # Either the script name or the recognisable job/step name must
        # appear — both are acceptable evidence the check is wired up.
        assert ("check-doc-drift.sh" in ci) or ("doc-drift" in ci), (
            "ci.yml must wire up the doc-drift check"
        )
