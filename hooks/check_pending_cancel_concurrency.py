#!/usr/bin/env python3
"""
Forbid a ref-keyed concurrency group on a required-check workflow that can fire
more than one run per commit.

`check_static_concurrency` treats `github.ref` / `github.head_ref` keys as safe
on the assumption that a ref-keyed run is only ever superseded by a *newer run
of the same ref*, whose own reporter re-posts the check. That assumption breaks
when `on.pull_request.types` includes an activity type outside the default
{opened, synchronize, reopened}: types like `labeled` or `closed` fire a new
run WITHOUT a new head SHA, so several runs queue on ONE commit (a Dependabot
PR is born with labels — `opened` + one `labeled` per label land near-
simultaneously). GitHub keeps at most one running + one pending run per group;
the third same-SHA run cancels a sibling that is *current*, not superseded. Its
`always()` reporter resolves `cancelled` → the required check goes RED on the
live head with no real failure.

`cancel-in-progress` cannot save a ref-keyed group here: `true` cancels the
in-progress run (current SHA → red), `false` cancels the pending one (also
current SHA → red). The safe fixes are to drop the group or key it on
`github.run_id` (a group of one cannot cancel a sibling).

A workflow "backs a required check" when it has both a decide gate and an
`always()` reporter (the decide-job + reporter architecture) — and BOTH the
workflow-level `concurrency:` block and every job-level one are checked, since
the incident groups were per-job. This lint is deliberately scoped to per-ref/
per-PR groups (the key polarity check_static_concurrency calls safe); a STATIC
group under the same type storm shares the failure mode but stays that lint's
territory at the workflow level — a job-level static group is a known handoff
gap neither lint flags today.

Opt out with "# pending-cancel-ok" for a deliberately-serialized workflow that
is genuinely never a required check.
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _job_blocks,
    concurrency_line,
    has_always_reporter,
    has_decide_gate,
    job_concurrency_line,
    opted_out,
    workflow_files,
)

OPT_OUT = "pending-cancel-ok"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

# The pull_request activity types that only fire alongside a new head SHA. Any
# type OUTSIDE this set (labeled, closed, ready_for_review, …) fires a fresh run
# on the SAME commit — the storm that makes a ref-keyed group cancel a current-
# SHA sibling.
DEFAULT_PR_TYPES = frozenset({"opened", "synchronize", "reopened"})

# Group-expression substrings that key a group per-ref / per-PR — one shared slot
# for every run of the PR, including the same-SHA siblings a type storm queues.
# `github.ref` also covers `github.ref_name`; `pull_request.number` covers
# `github.event.pull_request.number`. Best-effort substring match of the group
# expression, not a full ${{ }} parse — same policy as check_static_concurrency.
PER_REF_KEYS = (
    "github.ref",
    "github.head_ref",
    "pull_request.number",
    "github.event.number",
)

# A group also keyed per-run is a group of one: it can never hold two runs, so
# it can never pending-cancel a sibling. (Not run_attempt — concurrent runs
# share attempt 1.)
PER_RUN_KEYS = ("github.run_id", "github.run_number")


def _storm_types(doc: dict) -> set[str]:
    """The declared pull_request(_target) activity types that fire a run WITHOUT
    a new head SHA — empty when the workflow sticks to the default types (or the
    `pull_request:` / `pull_request: ~` shorthand, which means the defaults)."""
    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict):
        return set()  # `on: [push, pull_request]` list/scalar form → default types
    extra: set[str] = set()
    for trigger in ("pull_request", "pull_request_target"):
        cfg = triggers.get(trigger)
        if not isinstance(cfg, dict):
            continue  # `pull_request:` / `~` / `true` shorthand → default types
        types = cfg.get("types")
        if isinstance(types, str):
            types = [types]  # GitHub normalizes a scalar filter to a one-item list
        if not isinstance(types, list):
            continue
        extra |= {str(t) for t in types} - DEFAULT_PR_TYPES
    return extra


def _group_of(conc: object) -> object:
    """The group expression of a `concurrency:` value: the mapping's `group`
    key, or the scalar shorthand itself — GitHub treats `concurrency: <expr>`
    as `concurrency: {group: <expr>, cancel-in-progress: false}`."""
    if isinstance(conc, dict):
        return conc.get("group")
    return conc


def _ref_keyed(group: object) -> bool:
    """True when a concurrency group expression is keyed per-ref/per-PR and NOT
    also per-run (github.run_id / run_number make it a group of one — safe)."""
    if group is None:
        return False
    text = str(group)
    if any(key in text for key in PER_RUN_KEYS):
        return False
    return any(key in text for key in PER_REF_KEYS)


def _message(storm: set[str]) -> str:
    types = ", ".join(sorted(storm))
    return (
        "concurrency.group is keyed per-ref/per-PR on a workflow that "
        "backs a required check (decide gate + always() reporter) AND declares "
        f"pull_request types beyond opened/synchronize/reopened ({types}). Those "
        "types fire extra runs on the SAME head SHA; GitHub holds at most one "
        "running + one pending run per group, so a same-SHA sibling gets "
        "cancelled — its always() reporter resolves 'cancelled' and the required "
        "check goes red on the current commit with no real failure "
        "(cancel-in-progress true or false only picks WHICH current-SHA run "
        "dies). Drop the group, or key it on github.run_id (a group of one "
        f"cannot cancel a sibling), or add '# {OPT_OUT}' if this workflow is "
        "never a required check."
    )


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every ref-keyed concurrency group — workflow-
    level OR job-level — on a required-check workflow whose pull_request types
    can queue multiple runs on one head SHA.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify "
                "concurrency-group safety against same-SHA pending-cancellation — "
                "fix the syntax (or run actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict) or opted_out(text, OPT_OUT):
        return []

    storm = _storm_types(doc)
    if not storm:
        return []  # one run per SHA — a ref-keyed group only supersedes older SHAs

    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return []
    if not (has_decide_gate(jobs) and has_always_reporter(jobs)):
        return []  # not a required-check shape — a reddened cancel self-describes

    violations: list[tuple[int | None, str]] = []
    if _ref_keyed(_group_of(doc.get("concurrency"))):
        violations.append((concurrency_line(text), f"workflow-level {_message(storm)}"))

    blocks = _job_blocks(text)
    for name, cfg in jobs.items():
        if not isinstance(cfg, dict):
            continue
        if _ref_keyed(_group_of(cfg.get("concurrency"))):
            block = blocks.get(str(name))
            fallback = block[0] if block else concurrency_line(text)
            line = job_concurrency_line(block, fallback)
            violations.append((line, f"job '{name}': {_message(storm)}"))
    return violations


def main() -> int:
    total = 0
    for path in workflow_files(WORKFLOWS_DIR, ACTIONS_DIR):
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
