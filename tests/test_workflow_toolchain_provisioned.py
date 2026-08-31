"""Every workflow job that reaches `uv` must also provision `uv`.

`release-canary` and `startup-failure-scan` both call a `uv run` console script
through a wrapper in `.github/scripts/`. Neither job passed `setup-python: true`
to the shared `setup-base-env` action, so the action's uv steps were skipped and
both scripts died with `uv: command not found` — on every scheduled fire, for
weeks. Their own wiring suites did not see it: each drives the wrapper against a
STUBBED `uv` on PATH, which is a dependency more permissive than the real runner.

This test closes that gap from the other side. It reads the real workflow tree,
follows each `run:` body into the scripts it invokes, and asserts that a job that
can reach the `uv` command runs in a job that installs it.
"""

import re

import pytest
import yaml

from tests._helpers import REPO_ROOT, load_hook

bash_ast = load_hook("_cts_bash_ast.py", "toolchain_provisioned_bash_ast")

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPTS = REPO_ROOT / ".github" / "scripts"
TOOL = "uv"
SETUP_ACTION = "./.github/actions/setup-base-env"
# The repo's own pinned-and-verified uv installer, and the upstream action it
# wraps (which `setup-base-env` still uses behind `setup-python`).
LOCAL_UV_ACTION = "./.github/actions/setup-uv"
SETUP_UV_ACTION = "astral-sh/setup-uv"
# A `bash .github/scripts/x.sh` (or a bare path) inside a run: body or another
# script. Matching the PATH, not a command boundary, is deliberate: this only
# decides which file to read next, and reading one file too many is harmless.
SCRIPT_REF = re.compile(r"\.github/scripts/[\w./-]+")


def _runs_tool(text: str) -> bool:
    """True when TEXT executes TOOL as a command word.

    Asks the bash grammar rather than scanning the text: `uv` inside a message a
    command prints, or inside a comment, is not an invocation.
    """
    try:
        root = bash_ast.parse(text)
    except (bash_ast.UnparseableShellError, bash_ast.PathologicalInputError):
        # A `run:` body can hold `${{ }}` templating that is not valid bash. Fall
        # back to the text, which over-reports rather than under-reports.
        return re.search(rf"(?<![-\w]){re.escape(TOOL)}\s", text) is not None
    return any(
        bash_ast.command_name(node) == TOOL
        for node in bash_ast.iter_nodes(root, "command")
    )


def _scripts_reaching_tool() -> set[str]:
    """Repo-relative paths under `.github/scripts` that reach TOOL, transitively."""
    bodies = {
        str(path.relative_to(REPO_ROOT)): path.read_text(encoding="utf-8")
        for path in SCRIPTS.rglob("*")
        if path.is_file() and path.suffix in {".sh", ".bash", ""}
    }
    reaching = {rel for rel, body in bodies.items() if _runs_tool(body)}
    while True:
        grown = {
            rel
            for rel, body in bodies.items()
            if rel not in reaching and reaching & set(SCRIPT_REF.findall(body))
        }
        if not grown:
            return reaching
        reaching |= grown


def _provisions_tool(job: dict) -> bool:
    """True when JOB installs TOOL — through the shared action or setup-uv directly."""
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        if uses == LOCAL_UV_ACTION or uses.startswith(SETUP_UV_ACTION):
            return True
        if uses == SETUP_ACTION:
            # `false` is the action's default, so an absent input does not provision.
            # Any other value (including a `${{ }}` expression) is credited: this
            # test cannot evaluate an expression, and the workflows that use one
            # gate it on a file whose absence also removes the need.
            if str((step.get("with") or {}).get("setup-python", "false")) != "false":
                return True
    return False


def _jobs_reaching_tool() -> list[tuple[str, str]]:
    """Every `(workflow file, job id)` whose steps can reach TOOL."""
    scripts = _scripts_reaching_tool()
    reaching = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for job_id, job in (doc.get("jobs") or {}).items():
            bodies = [
                str(step.get("run", ""))
                for step in job.get("steps") or []
                if isinstance(step, dict) and step.get("run")
            ]
            if any(
                _runs_tool(body) or scripts & set(SCRIPT_REF.findall(body))
                for body in bodies
            ):
                reaching.append((path.name, job_id))
    return reaching


def test_some_job_reaches_the_tool() -> None:
    """Non-vacuity: the assertion below is worthless if nothing matches."""
    assert _jobs_reaching_tool(), (
        f"no workflow job reaches `{TOOL}` — the reachability walk found nothing, "
        "so the provisioning assertion cannot fail"
    )


def test_a_wrapper_script_is_reached_through_its_caller() -> None:
    """Non-vacuity: the walk really follows a `run:` body into a script."""
    assert ".github/scripts/startup-failure-scan.sh" in _scripts_reaching_tool()


@pytest.mark.parametrize(
    ("workflow", "job_id"), _jobs_reaching_tool(), ids=lambda part: str(part)
)
def test_a_job_that_reaches_the_tool_provisions_it(workflow: str, job_id: str) -> None:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    assert _provisions_tool(doc["jobs"][job_id]), (
        f"{workflow} job {job_id!r} runs `{TOOL}` but no step installs it — the "
        f"step dies with `{TOOL}: command not found`. Add a step that uses "
        f"{LOCAL_UV_ACTION}, the repo's pinned installer."
    )
