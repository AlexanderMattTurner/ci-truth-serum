#!/usr/bin/env python3
"""
Enforce an always() reporter job on gated GitHub Actions workflows.

A workflow with a decide gate (uses decide-reusable.yaml, or conditions jobs
on needs.decide.outputs.*) fails open when the gate job itself fails: every
gated work job skips, a skipped check run satisfies branch protection, and the
merge greens over a gate that verified nothing. Two shapes close that hole. A
reporter job with `if: always()` always runs and reds when the gate failed. A
fail-closed twin (`if: always() && needs.<gate>.result != 'success'`) is
SKIPPED on every healthy run — booting no runner, still satisfying required
checks — and runs red only when a gate did not succeed; the gated work jobs
themselves then carry the required-check markers.

A twin is accepted only when it really closes the hole, which its `if:` shape
alone does not prove. Its disjuncts must name EVERY decide gate this workflow
declares and no other job: a twin that names one of two gates leaves the other
free to fail unreported, and a twin that names a work job reds every healthy
run. It must `needs:` each gate it names, because an unneeded job's `result` is
empty in that expression. Its last `run:` step must end in a failing command,
because a twin that runs and exits 0 greens the merge on the one run it exists
to redden.

This lint is opinionated: it assumes the decide-job architecture (a `decide`
job exposing `outputs.*` and work jobs gated on `needs.decide.outputs.*`).
Enable it only if you follow that pattern.

Opt out with "# not-required-check" on the pull_request: trigger line when the
workflow is deliberately advisory and never a required status check.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
    decide_gate_names,
    has_always_reporter,
    is_fail_closed_twin,
    twin_defects,
)
from _cts_linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
)
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position

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
    text = path.read_text(encoding="utf-8")
    try:
        doc = safe_load(text)
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

    gate_names = decide_gate_names(jobs)
    if not gate_names or has_always_reporter(jobs):
        return None

    twins = {
        str(name): cfg
        for name, cfg in jobs.items()
        if isinstance(cfg, dict) and is_fail_closed_twin(cfg.get("if", ""))
    }
    if not twins:
        return pr_line, _no_fail_closed_shape()

    defects = {name: twin_defects(cfg, gate_names) for name, cfg in twins.items()}
    if any(not found for found in defects.values()):
        return None  # one twin closes the hole — that is all the workflow needs
    closest = min(defects, key=lambda name: len(defects[name]))
    return pr_line, _twin_fails_open(closest, defects[closest], gate_names)


def _no_fail_closed_shape() -> str:
    return (
        "workflow has a decide gate but nothing that fails closed when the "
        "gate itself fails — every gated work job then skips, and skipped "
        "satisfies a required check, so the merge greens over a broken gate. "
        "Add an always() reporter job that aggregates the work jobs, add a "
        "fail-closed twin (`if: always() && needs.<gate>.result != "
        "'success'`), or add "
        f"'# {OPT_OUT}' to the pull_request: trigger if this workflow is "
        "never a required check."
    )


def _twin_fails_open(name: str, defects: list[str], gate_names: set[str]) -> str:
    gates = ", ".join(sorted(gate_names))
    return (
        f"job '{name}' is shaped like a fail-closed twin but fails open: "
        + "; ".join(defects)
        + ". A twin must name exactly the decide gate(s) "
        f"{gates}, `needs:` each one, and end its last `run:` step in a failing "
        "command such as `exit 1`. Add an always() reporter job instead, or add "
        f"'# {OPT_OUT}' to the pull_request: trigger if this workflow is never a "
        "required check."
    )


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        try:
            found = check_file(path)
        except PathologicalInputError as err:
            print(f"::error file={rel}::{err}")
            total += 1
            continue
        if found is None:
            continue
        line, message = found
        loc = f"file={rel},line={line}" if line else f"file={rel}"
        print(f"::error {loc}::{message}")
        total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        print(
            "A gated workflow needs one of two shapes that fail closed when the "
            "gate itself fails: an always() reporter, or a fail-closed twin that "
            "names every gate and exits nonzero."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
