#!/usr/bin/env python3
"""
Require a concurrency block on every pull_request(_target)-triggered workflow.

The sibling `check_concurrency` lint validates a concurrency block *if one is
present* (it must set cancel-in-progress); it never fires when the block is
absent entirely. That is the exact gap that lets a PR workflow ship with no
concurrency control at all: every new push to the PR then starts a *second*
full run instead of cancelling the superseded one, so a busy branch stacks runs
and starves a shared, capped runner pool. This lint closes that gap — a
PR-triggered workflow must declare concurrency *somewhere*.

"Somewhere" is deliberate: the concurrency block may sit at the workflow level
(the common `group: ${{ github.workflow }}-${{ github.head_ref || github.ref }}`
house block) OR on a job. Job-level is not a loophole — it is the doctrine-
mandated shape for a required-check workflow that needs serialization, because a
*static* workflow-level lock on a required check hangs it at "Expected —
Waiting" (see check_static_concurrency). So both placements satisfy this rule.

Scope and non-goals:
  - Only pull_request / pull_request_target triggers are required to have it —
    those are the ones that fan out per-push-per-PR and drive the starvation.
    A push-only / schedule-only / workflow_call workflow is exempt (a reusable
    workflow inherits its caller's concurrency; a scheduled one does not stack
    per PR).
  - This catches the *total absence* of concurrency, not partial coverage: a
    workflow with a block on one job but an uncontrolled matrix on another still
    passes. Requiring per-job coverage would false-positive on the routine
    decide/reporter jobs that the workflow-level block already covers.

Opt out with a "# concurrency-not-required" comment for a PR workflow that
deliberately wants queue-don't-cancel (rare — that is the starvation behavior).
This lint is opinionated (Tier 2).
"""

import re
import sys
from pathlib import Path

import yaml

OPT_OUT = "concurrency-not-required"
PR_TRIGGERS = ("pull_request", "pull_request_target")
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _is_pr_triggered(triggers: object) -> bool:
    """True if a workflow's parsed `on:` value declares a pull_request trigger.

    Handles every `on:` spelling: a scalar (`on: pull_request`), a list
    (`on: [pull_request, push]`), and a mapping (`on:\n  pull_request:`).
    """
    if isinstance(triggers, str):
        return triggers in PR_TRIGGERS
    if isinstance(triggers, list):
        return any(t in PR_TRIGGERS for t in triggers)
    if isinstance(triggers, dict):
        return any(t in triggers for t in PR_TRIGGERS)
    return False


def _has_concurrency(doc: dict) -> bool:
    """True if concurrency is declared at the workflow level or on any job."""
    if doc.get("concurrency") is not None:
        return True
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return False
    return any(
        isinstance(cfg, dict) and cfg.get("concurrency") is not None
        for cfg in jobs.values()
    )


def _trigger_line(text: str) -> int:
    """1-based line of the pull_request(_target) trigger, else the `on:` line, else 1."""
    on_line = 1
    for num, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if re.match(r"^on\s*[:=]", stripped) or stripped == "on:":
            on_line = num
        if any(re.search(rf"\b{t}\b", line) for t in PR_TRIGGERS):
            return num
    return on_line


def check_file(path: Path) -> tuple[int, str] | None:
    """Return (line, message) if a PR-triggered workflow declares no concurrency."""
    text = path.read_text()
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        return None
    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    if not _is_pr_triggered(triggers):
        return None
    if _has_concurrency(doc):
        return None
    if OPT_OUT in text:
        return None
    return _trigger_line(text), (
        "pull_request-triggered workflow declares no concurrency: block — a new "
        "push starts a second full run instead of cancelling the superseded one, "
        "stacking runs on a capped runner pool. Add the house block "
        "'group: ${{ github.workflow }}-${{ github.head_ref || github.ref }}' with "
        "'cancel-in-progress: ${{ github.event_name == \"pull_request\" }}' at the "
        "workflow level (or on the expensive job, if it backs a required check), "
        f"or add '# {OPT_OUT}' if queue-don't-cancel is intended."
    )


def main() -> int:
    files = sorted(WORKFLOWS_DIR.glob("*.yaml")) + sorted(WORKFLOWS_DIR.glob("*.yml"))
    total = 0
    for path in files:
        found = check_file(path)
        if found is None:
            continue
        line, message = found
        print(f"::error file={path.relative_to(REPO_ROOT)},line={line}::{message}")
        total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
