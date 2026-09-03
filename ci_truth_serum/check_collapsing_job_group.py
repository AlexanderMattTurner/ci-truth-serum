#!/usr/bin/env python3
"""
Forbid a per-ref job concurrency group that collapses on an event the job runs on.

A job-level `concurrency.group` that names a per-ref key — `github.head_ref`,
`github.event.pull_request.number`, `github.ref` — reads as "one slot per branch
or per pull request". That reading holds only on the events where the key holds
a value. GitHub leaves `github.head_ref` and the pull-request number EMPTY off a
pull-request event. GitHub pins `github.ref` to the default branch on an event
that carries no ref of its own, such as `schedule`, `workflow_run`, or
`pull_request_target`. On such an event the group is one fixed string, so every
run of that event shares one slot.

Take `group: labeler-${{ github.event.pull_request.number }}` with
`cancel-in-progress: true`, on a workflow that fires on `pull_request` and on
`schedule`. Each pull-request run gets `labeler-41`, `labeler-42`, and so on.
Every scheduled run gets `labeler-`. The second scheduled run therefore cancels
the first, which was doing unrelated work on a different commit, and the check
battery it started is paid for and thrown away.

A job whose `if:` cannot run on the collapsing event is safe, because a skipped
job claims no slot. So the lint asks two questions and reports only when both
answers point the same way: does the group collapse on an event the workflow
declares, and does the job's `if:` still admit that event? The `if:` reading is
one-sided — a term it cannot classify restricts nothing — so a job gated through
a `needs.<gate>.outputs` value reads as admitting every event.

Three fixes: add a key that varies on the collapsing event (`github.run_id`
always does), narrow the job's `if:` to the events the group keys on, or drop
the group.

A group with NO per-ref key is out of scope, and deliberately so. Moving a
static workflow-level group down onto the expensive job is the remedy
check_static_concurrency and check_cancellable_required_check both prescribe, so
a lint that flagged it would fire on every application of the blessed fix.

Opt out with "# inert-group-ok: <reason>" on the job's key line or inside its
body. Two reasons qualify: the job's real gate makes it inert on the collapsing
event, or the job serializes every run of that event on purpose. The reason is
required; a bare annotation does not suppress.

This lint is opinionated (Tier 2).
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _job_blocks,
    annotated,
    declared_events,
    group_collapse_events,
    group_has_per_ref_key,
    job_admitted_events,
    workflow_files,
)
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position

ALLOW = "inert-group-ok"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

_MESSAGE = (
    "job '{name}' keys its concurrency group on the ref. That key is empty or "
    "fixed on {events}. Every {first} run of this workflow then shares the one "
    "slot '{group}'. The job's `if:` still lets the job run there, so a sibling "
    "{first} run cancels it or queues behind it. The cancelled run tested an "
    "unrelated commit, and its whole check battery is paid for and thrown away. "
    "Add a key that varies on {first}; github.run_id always does. Or narrow the "
    "`if:` to the events the group keys on. Or add '# " + ALLOW + ": <reason>' "
    "when the job is inert on {first}, or serializes every {first} run on "
    "purpose."
)


def _block_opted_out(block_text: str) -> bool:
    """True if a reason-bearing `# inert-group-ok:` sits in a `#` comment inside
    the job's source block — never in a string value.

    Line by line, because the shared matcher's reason tail deliberately cannot
    cross a newline: run over the whole block at once, a bare annotation ending a
    line would borrow the next line's first character as its reason."""
    return any(annotated(line, ALLOW) for line in block_text.splitlines())


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every job whose per-ref group collapses on an
    event the job's `if:` still admits.

    A file that cannot be parsed as YAML is itself reported as a violation (line
    ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_job_timeout &c.)."""
    text = path.read_text(encoding="utf-8")
    try:
        doc = safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify job "
                "concurrency groups — fix the syntax (or run actionlint) and "
                "re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []

    events = declared_events(doc)
    blocks = _job_blocks(text)
    violations: list[tuple[int | None, str]] = []
    for name, cfg in jobs.items():
        if not isinstance(cfg, dict):
            continue
        conc = cfg.get("concurrency")
        group = conc.get("group") if isinstance(conc, dict) else conc
        if not isinstance(group, str) or not group:
            continue
        if not group_has_per_ref_key(group):
            continue  # a fully static group is the blessed serialization remedy
        collapsed = set(group_collapse_events(group, events))
        hits = sorted(collapsed & job_admitted_events(cfg.get("if"), events))
        if not hits:
            continue
        block = blocks.get(str(name))
        if block and _block_opted_out(block[1]):
            continue
        violations.append(
            (
                block[0] if block else 1,
                _MESSAGE.format(
                    name=name,
                    events=" and ".join(f"a '{event}' run" for event in hits),
                    first=f"'{hits[0]}'",
                    group=group,
                ),
            )
        )
    return violations


def main() -> int:
    files = workflow_files(WORKFLOWS_DIR)
    total = 0
    for path in files:
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
