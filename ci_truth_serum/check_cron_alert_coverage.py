#!/usr/bin/env python3
"""Fail a scheduled workflow whose failures reach nobody.

A workflow triggered by `on.schedule:` has no PR surface: no check run, no
reviewer, nothing blocking a merge. Its red lands in an Actions tab nobody
opens, so a scheduled guard that has been failing for six weeks is
indistinguishable from one that has been passing. Every scheduled workflow must
therefore EITHER route its failures to a human, OR carry an explicit opt-out
marker that states why nobody needs to know.

The load-bearing half is REACHABILITY, not presence. A notification step gated
`if: success()`, or living in a job gated `needs.build.result == 'success'`,
looks exactly like coverage in review and fires exactly never on the path it
exists for — the inert-feature bug wearing a fix's clothes. So a notifier is
credited only when its own gate, or its job's, is failure-directed:

  * `failure()`, `cancelled()`, or `always()` anywhere in the expression; or
  * a `.result` / `.conclusion` / `.outcome` comparison pointing AT failure —
    `!= 'success'`, `== 'failure'`, `== 'cancelled'`, `== 'timed_out'`.

A gate whose only such comparison is `== 'success'` (or `!= 'failure'`), or that
calls `success()`, BLOCKS the failure path and disqualifies the notifier even if
a sibling clause mentions a result. An UNGATED trailing step is not credited
either: GitHub abandons a job at its first failed step, so a notify step with no
`if:` never runs on the failure it is meant to report.

What counts as a notification is configurable, never hardcoded to one vendor.
The default patterns live in `_linecheck.NOTIFIER_PATTERNS` — shared with
check_failure_notifier_coverage, which uses the same list to recognize the
notifier workflow itself — and match case-insensitively against a step's
`uses:`, `name:`, and `run:`. They cover the common human-routing sinks:

    notif  ntfy  slack  pagerduty  opsgenie  victorops  discord  telegram
    mattermost  webhook  sms  smtp  send[-_]?e?mail  sendmail
    gh issue create  <verb>-<...>-issue  issue_write

Add your own with `--notifier-pattern REGEX` (repeatable); the flag EXTENDS the
defaults rather than replacing them, so naming a house sink can never silently
un-recognize the sinks already matched.

The opt-out is `# cron-alert: false  # <reason>` on the `schedule:` key line or
one of its direct-child lines. A missing reason, or a negative placeholder
("n/a", "none", "not needed"), is a failure: the reason is the entire point of
the marker, because it is the only thing a reviewer can actually check.

Two modes, split at the line between a repo that has not adopted the pattern and
one that has adopted it wrongly. By DEFAULT the only findings are (a) a malformed
`# cron-alert:` marker and (b) a notifier a SUCCESS-ONLY gate blocks — claiming
coverage you do not have is worse than claiming none, while a scheduled workflow
with no notifier at all is simply un-adopted and stays silent, so the hook can
ship in a default hook set. With `--require-alert`, every scheduled workflow must
carry a failure-REACHABLE notifier or a reasoned marker; a notifier gated on
something unrelated to status (an output, an event name) no longer suffices.
Globs every workflow like the other workflow lints; the passed file list is
ignored.
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    has_trigger,
    key_block_lines,
    notifier_matcher,
    parse_optout_marker,
    step_text,
    unwrap_expression,
    WORKFLOW_GLOBS,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
# Composite actions are NOT scanned: only a workflow can carry `on.schedule:`.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

MARKER = "cron-alert"

# The status functions that put an expression on the failure path (`always()`
# runs the step whatever happened, so it covers failure too) and the one that
# takes it off.
_REACHING_CALLS = re.compile(r"\b(?:failure|cancelled|always)\s*\(\s*\)")
_BLOCKING_CALLS = re.compile(r"\bsuccess\s*\(\s*\)")
# A comparison against a job/step status: `needs.x.result == 'failure'`,
# `steps.y.outcome != 'success'`, `needs.z.result == "cancelled"`.
_STATUS_COMPARISON = re.compile(
    r"\.(?:result|conclusion|outcome)\s*(?P<op>==|!=)\s*['\"](?P<value>[\w-]+)['\"]"
)

REACHABLE, BLOCKED, NEUTRAL = "reachable", "blocked", "neutral"


def _points_at_failure(op: str, value: str) -> bool:
    """True when one `.result`-style comparison can hold on a failure path:
    `== '<anything but success>'`, or `!= 'success'`."""
    return (value != "success") if op == "==" else (value == "success")


def gate_direction(gate: object) -> str:
    """Which way an `if:` expression points relative to the failure path.

    REACHABLE — the gate can be true when something failed. BLOCKED — the gate
    is true only on success, so nothing behind it ever reports a failure.
    NEUTRAL — the gate says nothing about status (an inputs/event/outputs test,
    or no gate at all); on its own that is not coverage, because an ungated step
    in a failed job is skipped.
    """
    text = unwrap_expression(gate)
    if not text:
        return NEUTRAL

    comparisons = [
        (m.group("op"), m.group("value").lower())
        for m in _STATUS_COMPARISON.finditer(text)
    ]
    if _REACHING_CALLS.search(text) or any(
        _points_at_failure(op, value) for op, value in comparisons
    ):
        return REACHABLE
    if _BLOCKING_CALLS.search(text) or comparisons:
        return BLOCKED
    return NEUTRAL


def notifier_steps(doc: dict, matcher: "re.Pattern[str]") -> list[dict]:
    """Every notification step in the workflow, each as
    `{"line", "job", "name", "step_gate", "job_gate"}`.

    A job-level `uses:` (a reusable notifier workflow) is reported as a single
    pseudo-step whose two gates are both the job's own — it has no inner step to
    carry a second one.
    """
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []

    found: list[dict] = []
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        job_gate = job.get("if", "")
        job_line = job.get("__line__", 1)
        if isinstance(job.get("uses"), str) and matcher.search(job["uses"]):
            found.append(
                {
                    "line": job_line,
                    "job": str(job_name),
                    "name": job["uses"],
                    "step_gate": job_gate,
                    "job_gate": job_gate,
                }
            )
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict) or not matcher.search(step_text(step)):
                continue
            found.append(
                {
                    "line": step.get("__line__", job_line),
                    "job": str(job_name),
                    "name": str(step.get("name") or step.get("uses") or "run step"),
                    "step_gate": step.get("if", ""),
                    "job_gate": job_gate,
                }
            )
    return found


def _directions(notifier: dict) -> tuple[str, str]:
    return gate_direction(notifier["step_gate"]), gate_direction(notifier["job_gate"])


def is_failure_reachable(notifier: dict) -> bool:
    """True when a notifier actually fires on the failure path: neither its own
    gate nor its job's blocks failure, and at least one of the two points at it.
    """
    directions = _directions(notifier)
    return BLOCKED not in directions and REACHABLE in directions


def is_failure_blocked(notifier: dict) -> bool:
    """True when a gate on the notifier's own path holds ONLY on success — the
    inert-notifier defect, as opposed to a notifier merely gated on something
    unrelated to status (an output, an event name), which is NEUTRAL."""
    return BLOCKED in _directions(notifier)


def violations(
    text: str, matcher: "re.Pattern[str]", require_alert: bool
) -> list[tuple[int, str]]:
    """(1-based line, message) for every cron-alert-coverage violation in one
    workflow's source. An empty list means the workflow is either unscheduled or
    covered."""
    try:
        doc = yaml.load(text, Loader=LineLoader)
    except yaml.YAMLError as err:
        return [
            (
                1,
                "could not parse as YAML "
                f"({str(err).partition(chr(10))[0]}); cannot verify that this "
                "workflow's scheduled failures reach anyone — fix the syntax (or "
                "run actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict) or not has_trigger(doc, "schedule"):
        return []

    schedule_lines = key_block_lines(text, "schedule")
    anchor = _schedule_line(text)
    reason, marker_error = parse_optout_marker(schedule_lines, MARKER)
    if marker_error:
        return [(anchor, marker_error)]
    if reason:
        return []

    notifiers = notifier_steps(doc, matcher)
    if any(is_failure_reachable(n) for n in notifiers):
        return []

    blocked = [n for n in notifiers if is_failure_blocked(n)]
    if blocked:
        return [
            (
                blocked[0]["line"],
                "this scheduled workflow's notification step(s) sit behind a "
                f"success-only gate, so no failure can reach them — {_detail(blocked)}"
                ". That reads as coverage in review and fires exactly never on the "
                "path it exists for. Gate the notifier `if: failure()` (or on a "
                f"`.result != 'success'`), or annotate `# {MARKER}: false  "
                "# <reason>` on the `schedule:` key.",
            )
        ]
    if not require_alert:
        return []
    if notifiers:
        return [
            (
                notifiers[0]["line"],
                "this scheduled workflow's notification step(s) are gated on "
                "something other than status, so a failed run notifies nobody — "
                f"{_detail(notifiers)}. An ungated step does not help either: "
                "GitHub abandons a job at its first failed step. Add `if: "
                f"failure()`, or annotate `# {MARKER}: false  # <reason>` on the "
                "`schedule:` key.",
            )
        ]
    return [
        (
            anchor,
            "this scheduled workflow routes its failures nowhere: a cron has no "
            "PR surface, so its red is visible only to whoever opens the Actions "
            "tab. Add a notification step gated `if: failure()`, or annotate "
            f"`# {MARKER}: false  # <reason>` on the `schedule:` key.",
        )
    ]


def _detail(notifiers: list[dict]) -> str:
    """The gate pair of each named notifier, so the finding says which gate to fix."""
    return "; ".join(
        f"{n['job']} → {n['name']!r} (step if: {str(n['step_gate']) or '<none>'}, "
        f"job if: {str(n['job_gate']) or '<none>'})"
        for n in notifiers
    )


def _schedule_line(text: str) -> int:
    """The 1-based line of the `schedule:` key, the anchor for a workflow-level
    finding; 1 when the key cannot be located."""
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(r"^[ \t]*schedule\s*:", line):
            return num
    return 1


def workflow_files() -> list[Path]:
    return sorted(p for glob in WORKFLOW_GLOBS for p in WORKFLOWS_DIR.glob(glob))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notifier-pattern",
        action="append",
        default=[],
        metavar="REGEX",
        help="an additional regex recognizing a notification step (repeatable); "
        "extends the built-in patterns rather than replacing them",
    )
    parser.add_argument(
        "--require-alert",
        action="store_true",
        help="also fail a scheduled workflow that has no notifier at all "
        "(default: only a notifier no failure can reach is a finding)",
    )
    args = parser.parse_args(argv)
    matcher = notifier_matcher(args.notifier_pattern)

    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        found = violations(
            path.read_text(encoding="utf-8"), matcher, args.require_alert
        )
        for line, message in found:
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} cron-alert-coverage violation(s) found.")
        print(
            "A scheduled workflow has no PR surface — a red nobody is told about "
            "is indistinguishable from a green."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
