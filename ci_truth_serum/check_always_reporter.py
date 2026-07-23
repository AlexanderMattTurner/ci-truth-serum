#!/usr/bin/env python3
"""
Enforce an always() reporter job on gated GitHub Actions workflows.

A workflow with a decide gate (uses decide-reusable.yaml, or conditions jobs
on needs.decide.outputs.*) can strand required status checks at "Expected —
Waiting" when the gate skips all work jobs — GitHub never receives a
conclusion. The fix is a reporter job with `if: always()` that always reports.

This lint is opinionated: it assumes the decide-job + reporter architecture
(a `decide` job exposing `outputs.*`, work jobs gated on `needs.decide.outputs.*`,
and a final `if: always()` reporter that aggregates them). Enable it only if you
follow that pattern.

Opt out with "# not-required-check" on the pull_request: trigger line when the
workflow is deliberately advisory and never a required status check.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
    has_always_reporter,
    has_decide_gate,
)
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "not-required-check"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
PR_TRIGGERS = ("pull_request", "pull_request_target")


def _locate_trigger(text: str, trigger: str) -> tuple[int, bool]:
    """Return (1-based line number, opted-out) for the first occurrence of trigger."""
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(rf"^\s*{trigger}\s*:", line):
            return num, annotated(line, OPT_OUT, require_reason=False)
    return 1, False


def _trigger_names(triggers: object) -> set[str]:
    """The set of trigger names an `on:` value declares, across every spelling:
    a scalar (`on: pull_request`), a list (`on: [pull_request, push]`), or a
    mapping (`on:\n  pull_request:`). Mirrors check_requires_concurrency's
    `_is_pr_triggered` so list/scalar forms are never silently skipped."""
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        return {t for t in triggers if isinstance(t, str)}
    if isinstance(triggers, dict):
        return {k for k in triggers if isinstance(k, str)}
    return set()


def check_file(path: Path) -> tuple[int | None, str] | None:
    """Return (line, message) if the workflow is gated but lacks an always() reporter.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return None, (
            f"could not parse as YAML ({first_line}); cannot verify always() "
            "reporter coverage — fix the syntax (or run actionlint) and re-check."
        )
    if not isinstance(doc, dict):
        return None

    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    names = _trigger_names(triggers)

    # Only check workflows that fire on pull_request (or pull_request_target).
    pr_line: int | None = None
    opted_out = False
    for trigger in PR_TRIGGERS:
        if trigger in names:
            line, out = _locate_trigger(text, trigger)
            if pr_line is None:
                pr_line = line
            if out:
                opted_out = True
    if pr_line is None or opted_out:
        return None

    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return None

    if not has_decide_gate(jobs) or has_always_reporter(jobs):
        return None

    return pr_line, (
        "workflow has a decide gate but no job with `if: always()` — "
        "gated work jobs are skipped when nothing relevant changed, leaving "
        "required checks at 'Expected — Waiting'. Add an always() reporter job "
        "that aggregates the work jobs, or add "
        f"'# {OPT_OUT}' to the pull_request: trigger if this workflow is "
        "never a required check."
    )


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
            "A gated workflow without an always() reporter strands a required "
            "check at 'Expected — Waiting'."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
