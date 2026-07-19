#!/usr/bin/env python3
"""
Enforce no path/branch filter under pull_request(_target): triggers in workflows.

A workflow-level filter on a pull_request trigger means the workflow never fires
when a PR doesn't match it — GitHub shows a required check as
"Expected — Waiting" forever and the PR can't be merged. Two filter families do
this:

  * paths / paths-ignore — a PR that doesn't touch (or touches only ignored)
    paths skips the workflow. The fix is to move path filtering into a job (a
    "decide" job that gates the expensive jobs on a diff), so the workflow
    always fires and always reports while the work skips when nothing relevant
    changed.
  * branches / branches-ignore — a PR whose base branch isn't listed skips the
    workflow. The trap is a STACKED PR based on another feature branch: a
    `branches: [main]` filter skips it, and GitHub does NOT re-fire the workflow
    when it retargets the child's base to main on the parent's merge, so the
    required checks hang permanently. The fix is to drop the filter — a required
    check must fire for every PR regardless of base branch.

Opt out with a "# not-required-check" comment on the pull_request(_target):
trigger line when the workflow is deliberately advisory and never a required
status check.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "not-required-check"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
PR_TRIGGERS = ("pull_request", "pull_request_target")
PATH_FILTERS = ("paths", "paths-ignore")
BRANCH_FILTERS = ("branches", "branches-ignore")
TRIGGER_FILTERS = PATH_FILTERS + BRANCH_FILTERS


def locate_trigger(text: str, trigger: str) -> tuple[int, bool]:
    """Return the trigger declaration's 1-based line number and whether it's opted out."""
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(rf"^\s*{trigger}\s*:", line):
            return num, OPT_OUT in line
    return 1, False


def remediation(filter_key: str, trigger: str) -> str:
    """The tailored message for a filter family that strands a required check.

    paths/branches hang the same way but have different fixes: a path filter
    belongs in a decide job; a branch filter must simply go (a required check
    fires for every PR base)."""
    if filter_key in PATH_FILTERS:
        cause_fix = (
            "prevents the workflow from reporting when paths don't match. "
            "Path-gate inside a job (a decide job) instead"
        )
    else:
        cause_fix = (
            "prevents the workflow from firing for a PR whose base branch isn't "
            "listed — a stacked PR on a non-main base is stranded, and GitHub "
            "does not re-fire the workflow when the base retargets to main on the "
            "parent's merge. A required check must fire for every PR regardless "
            "of base, so drop the branch filter"
        )
    return (
        f"{filter_key}: under {trigger}: {cause_fix} — a required check hangs at "
        f"'Expected — Waiting'. Add '# {OPT_OUT}' if this workflow is never a "
        "required check."
    )


def check_file(path: Path) -> tuple[int | None, str] | None:
    """Return (line, message) if the workflow filters paths/branches on a
    pull_request trigger.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return None, (
            f"could not parse as YAML ({first_line}); cannot verify path/branch "
            "filters on the pull_request trigger — fix the syntax (or run "
            "actionlint) and re-check."
        )
    if not isinstance(doc, dict):
        return None
    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict):
        return None

    for trigger in PR_TRIGGERS:
        cfg = triggers.get(trigger)
        if not isinstance(cfg, dict):
            continue
        filter_key = next((key for key in TRIGGER_FILTERS if key in cfg), None)
        if filter_key is None:
            continue
        line, opted_out = locate_trigger(text, trigger)
        if opted_out:
            continue
        return line, remediation(filter_key, trigger)
    return None


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        found = check_file(path)
        if found is None:
            continue
        line, message = found
        rel = path.relative_to(REPO_ROOT)
        loc = f"file={rel},line={line}" if line else f"file={rel}"
        print(f"::error {loc}::{message}")
        total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        print(
            "A paths/branches filter on a pull_request trigger strands a "
            "required check at 'Expected — Waiting'."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
