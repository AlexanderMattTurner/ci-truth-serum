#!/usr/bin/env python3
"""One SSOT for the question "is this workflow's failure routed to a human?".

Two lints act on the answer from opposite ends — check_cron_alert_coverage asks
it of a scheduled workflow, check_failure_notifier_coverage asks it of every
push/schedule workflow to decide which ones the tree notifier still has to
watch — so the answer lives here once. Computing it twice from different sources
is how a repo ends up told that a workflow it deliberately opted out of must be
watched anyway, and that a workflow which already pages on its own must page
twice.

A failure reaches a human through exactly four routes, and any one of them is
enough:

  1. SELF-NOTIFY — the workflow contains its own notification step that a
     failure can actually reach (`is_failure_reachable`).
  2. WATCHED — some failure-notifier workflow lists this workflow's display name
     in `on.workflow_run.workflows`.
  3. PR SURFACE — the workflow also triggers on `pull_request` /
     `pull_request_target`, so a failure shows up as a check on the PR that
     caused it, in front of the author and the reviewer.
  4. OPT-OUT — a `# cron-alert: false  # <reason>` marker on the `schedule:` or
     `push:` key states why nobody needs to know.

Routes 2 and 3 are credited ASYMMETRICALLY, which is the whole reason the two
callers need distinct predicates rather than one boolean:

  * `unrouted_scheduled` (check_cron_alert_coverage) does NOT credit the PR
    surface: a cron fire has no PR, so a check run on some unrelated pull
    request says nothing about the 3am run that failed.
  * `needs_tree_notifier` (check_failure_notifier_coverage) does NOT credit
    being watched: that caller is computing which workflows the notifier list
    OUGHT to contain, so crediting the current contents would make the check
    vacuous — every list would already cover itself.

REACHABILITY is the load-bearing half of route 1. A notification step gated
`if: success()`, or living in a job gated `needs.build.result == 'success'`,
looks exactly like coverage in review and fires exactly never on the path it
exists for. So a notifier is credited only when its own gate, or its job's, is
failure-directed:

  * `failure()`, `cancelled()`, or `always()` anywhere in the expression; or
  * a `.result` / `.conclusion` / `.outcome` comparison pointing AT failure —
    `!= 'success'`, `== 'failure'`, `== 'cancelled'`, `== 'timed_out'`.

A gate whose only such comparison is `== 'success'` (or `!= 'failure'`), or that
calls `success()`, BLOCKS the failure path and disqualifies the notifier even if
a sibling clause mentions a result. An UNGATED trailing step is not credited
either: GitHub abandons a job at its first failed step, so a notify step with no
`if:` never runs on the failure it is meant to report.

What counts as a notification is configurable, never hardcoded to one vendor:
the patterns live in `_linecheck.NOTIFIER_PATTERNS` and both lints extend them
through `--notifier-pattern`.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    has_trigger,
    key_block_lines,
    parse_optout_marker,
    step_text,
    unwrap_expression,
    workflow_triggers,
)

MARKER = "cron-alert"

# The triggers whose failures this doctrine covers, and the keys an opt-out
# marker may be attached to. `schedule:` is where the marker normally sits; a
# push-only workflow has no schedule key, and would otherwise have no way to say
# "this failure deliberately goes nowhere" at all.
MONITORED_TRIGGERS = ("push", "schedule")

# The triggers that put a failure in front of a human with no notification at
# all: a failed check run on the pull request that caused it.
PR_TRIGGERS = ("pull_request", "pull_request_target")

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


def names_a_sink(doc: dict, matcher: "re.Pattern[str]") -> bool:
    """True when any text in the workflow that can name a notification sink does.

    Scans the workflow's own `name:`, then each job's `name:`/`uses:` and each of
    its steps — gate-blind on purpose, because this answers "what kind of
    workflow is this?" (notifier discovery), not "does a failure reach it?".
    """
    if matcher.search(str(doc.get("name", ""))):
        return True
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return False
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if matcher.search(step_text(job)):
            return True
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        if any(
            isinstance(step, dict) and matcher.search(step_text(step)) for step in steps
        ):
            return True
    return False


def is_notifier(doc: object, matcher: "re.Pattern[str]") -> bool:
    """True when DOC is a failure-notifier workflow: it observes other workflows
    (`on.workflow_run`) AND names a notification sink.

    Both halves are required. Without the trigger test, an ordinary CI workflow
    with a Slack step is mistaken for the notifier; without the sink test, an
    unrelated `workflow_run` consumer (an artifact collector, a coverage
    uploader) is held to the notifier's coverage invariant.
    """
    if not isinstance(doc, dict) or not has_trigger(doc, "workflow_run"):
        return False
    return names_a_sink(doc, matcher)


def notifier_list(doc: object) -> list[str] | None:
    """The notifier's `on.workflow_run.workflows` list, or None when the
    document doesn't carry one (a malformed notifier is a finding)."""
    triggers = workflow_triggers(doc)
    workflow_run = triggers.get("workflow_run") if isinstance(triggers, dict) else None
    workflows = (
        workflow_run.get("workflows") if isinstance(workflow_run, dict) else None
    )
    if isinstance(workflows, list) and all(isinstance(w, str) for w in workflows):
        return workflows
    return None


def watched_names(docs: list[dict], matcher: "re.Pattern[str]") -> set[str]:
    """Every workflow display name any notifier in DOCS watches — the UNION,
    since two notifiers may legitimately split the tree between them."""
    watched: set[str] = set()
    for doc in docs:
        if not is_notifier(doc, matcher):
            continue
        watched |= set(notifier_list(doc) or [])
    return watched


def optout_marker(text: str) -> tuple[str | None, str | None]:
    """The `# cron-alert: false  # <reason>` opt-out on this workflow's monitored
    trigger keys, as `(reason, error)` — see `_linecheck.parse_optout_marker`.

    The marker is read from the `schedule:` and `push:` key blocks, so one
    marker means the same thing to both lints: this workflow's failures are
    deliberately routed nowhere. A marker buried in a job or a step is not a
    classification of the trigger and does not count.
    """
    lines = [line for key in MONITORED_TRIGGERS for line in key_block_lines(text, key)]
    return parse_optout_marker(lines, MARKER)


def has_pr_surface(doc: object) -> bool:
    """True when a failure of this workflow lands as a check run on a pull
    request, which is a human-visible surface needing no notification."""
    return has_trigger(doc, *PR_TRIGGERS)


def self_notifies(doc: dict, matcher: "re.Pattern[str]") -> bool:
    """True when the workflow carries its own notification step a failure can
    actually reach."""
    return any(is_failure_reachable(n) for n in notifier_steps(doc, matcher))


@dataclass(frozen=True)
class Routing:
    """Which of the four routes to a human this one workflow has.

    `marker_error` is set when an opt-out marker is present but states no usable
    reason; `opted_out` is then False, because a marker nobody can check is not
    a decision anyone made.
    """

    self_notify: bool
    watched: bool
    pr_surface: bool
    opted_out: bool
    marker_error: str | None


def routing(
    doc: dict, text: str, matcher: "re.Pattern[str]", watched: set[str] | None = None
) -> Routing:
    """How this workflow's failures reach a human, if they do.

    WATCHED is the set of names the tree's notifiers list (`watched_names`);
    membership is judged on the workflow's `name:`, so an unnamed workflow is
    never credited — which is correct, since the notifier list would have to
    carry GitHub's file-path fallback instead, and that is separately flagged.
    """
    reason, marker_error = optout_marker(text)
    name = doc.get("name")
    return Routing(
        self_notify=self_notifies(doc, matcher),
        watched=isinstance(name, str) and name in (watched or set()),
        pr_surface=has_pr_surface(doc),
        opted_out=bool(reason),
        marker_error=marker_error,
    )


def unrouted_scheduled(route: Routing) -> bool:
    """True when a SCHEDULED workflow's failures reach nobody.

    The PR surface is deliberately not a route here: a cron fire has no pull
    request, so a check run on some unrelated PR reports nothing about the run
    that failed at 3am.
    """
    return not (route.self_notify or route.watched or route.opted_out)


def needs_tree_notifier(route: Routing) -> bool:
    """True when this workflow's failures reach nobody unless a notifier watches
    it — the residual the notifier's `workflows:` list has to cover.

    Being watched is deliberately not a route here: this is the predicate that
    decides what the list must contain, and crediting the list's current
    contents would make it assert only that it covers itself.
    """
    return not (route.self_notify or route.pr_surface or route.opted_out)
