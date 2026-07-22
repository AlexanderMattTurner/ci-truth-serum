#!/usr/bin/env python3
"""
Forbid a static workflow-level concurrency lock on a required-check workflow.

A workflow-level `concurrency.group` with no per-ref/per-PR key (no `github.ref`,
`github.head_ref`, …) serializes *every* ref through one slot. GitHub keeps at
most one running + one pending run per group and cancels the older *pending* run
wholesale when a newer one arrives — that cancelled run starts **zero** jobs, so
an `always()` reporter never executes and the required status check it backs
hangs at "Expected — Waiting for status to be reported" forever. The existing
always()-reporter guard cannot catch this: the cancellation happens at the
concurrency-queue stage, before any job initializes.

A workflow "backs a required check" here when it has both a decide gate and an
`always()` reporter (the decide-job + reporter architecture). When global
serialization is genuinely needed (e.g. a shared volume), put the `concurrency:`
block on the expensive **job** instead: the run always starts, decide + the
reporter always execute, and a superseded run surfaces as a definitive red.

Opt out with "# static-concurrency-ok" for a serialized workflow that is
deliberately never a required check.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    group_is_per_ref,
    has_always_reporter,
    has_decide_gate,
)

OPT_OUT = "static-concurrency-ok"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _concurrency_line(text: str) -> int:
    """Return the 1-based line number of the top-level `concurrency:` key."""
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(r"^concurrency\s*:", line):
            return num
    return 1


def _opted_out(text: str) -> bool:
    """True only when the opt-out token appears inside an actual `#` comment, not
    anywhere in the byte stream — a `group: "static-concurrency-ok"` string value
    must not silently disable the lint (that would be a fail-open)."""
    return any(
        OPT_OUT in line.split("#", 1)[1] for line in text.splitlines() if "#" in line
    )


def check_file(path: Path) -> tuple[int | None, str] | None:
    """Return (line, message) if the workflow has a static workflow-level lock
    on a required-check (decide gate + always() reporter) shape.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return None, (
            f"could not parse as YAML ({first_line}); cannot verify workflow-level "
            "concurrency safety — fix the syntax (or run actionlint) and re-check."
        )
    if not isinstance(doc, dict):
        return None
    conc = doc.get("concurrency")
    if not isinstance(conc, dict) or "group" not in conc:
        return None
    if _opted_out(text):
        return None

    group = str(conc.get("group", ""))
    if group_is_per_ref(group):
        return None  # per-ref / per-PR group — only superseded by its own ref

    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return None
    if not (has_decide_gate(jobs) and has_always_reporter(jobs)):
        return None  # not a required-check shape — a static lock is fine here

    line = _concurrency_line(text)
    return line, (
        "workflow-level concurrency.group is static (no github.ref / "
        "github.head_ref key) on a workflow that backs a required check "
        "(decide gate + always() reporter). A sibling ref's run can cancel "
        "this one's *pending* run wholesale — zero jobs start, the always() "
        "reporter never runs, and the required check hangs at 'Expected — "
        "Waiting' forever. Move the concurrency: block onto the expensive job "
        "to serialize there while the run + reporter always execute, or add "
        f"'# {OPT_OUT}' if this workflow is never a required check."
    )


def main() -> int:
    files = sorted(WORKFLOWS_DIR.glob("*.yaml")) + sorted(WORKFLOWS_DIR.glob("*.yml"))
    total = 0
    for path in files:
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
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
