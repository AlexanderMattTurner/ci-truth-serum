#!/usr/bin/env python3
"""Fail a scheduled workflow whose failures reach nobody.

A workflow triggered by `on.schedule:` has no PR surface: no check run, no
reviewer, nothing blocking a merge. Its red lands in an Actions tab nobody
opens, so a scheduled guard that has been failing for six weeks is
indistinguishable from one that has been passing. Every scheduled workflow must
therefore EITHER route its failures to a human, OR carry an explicit opt-out
marker that states why nobody needs to know.

"Routed to a human" is `_failure_routing`, shared with
check_failure_notifier_coverage so the two lints cannot demand contradictory
things of the same workflow. A scheduled workflow satisfies this check when any
of these holds:

  * it carries its own notification step a failure can REACH;
  * a failure-notifier workflow in the tree lists its `name:` in
    `on.workflow_run.workflows`, so the notifier pages on its behalf; or
  * it carries a reasoned `# cron-alert: false` marker.

The PR surface — the fourth route in `_failure_routing`, and the reason the
notifier lint excuses a workflow that also runs on `pull_request` — is
deliberately NOT accepted here: a cron fire has no pull request, so a green
check on somebody's PR says nothing about the run that failed at 3am.

The load-bearing half of the first route is REACHABILITY, not presence. A
notification step gated `if: success()`, or living in a job gated
`needs.build.result == 'success'`, looks exactly like coverage in review and
fires exactly never on the path it exists for — the inert-feature bug wearing a
fix's clothes. `_failure_routing.gate_direction` is what decides; its docstring
carries the full gate grammar.

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

The opt-out is `# cron-alert: false  # <reason>` on the `schedule:` (or `push:`)
key line or one of its direct-child lines. A missing reason, or a negative
placeholder ("n/a", "none", "not needed"), is a failure: the reason is the
entire point of the marker, because it is the only thing a reviewer can
actually check.

The check FAILS CLOSED, like check_failure_notifier_coverage's notifier
discovery: by default every scheduled workflow must reach a human by one of the
three routes above, and a repo that deliberately routes its crons nowhere says
so with `--allow-unrouted`. A silent pass on a tree where nothing is routed is
the one outcome this check must never produce — it verified nothing, and
"verified nothing" is indistinguishable from "everything is covered" in exactly
the tree where the distinction matters.

`--allow-unrouted` is a narrower silence, not a mute: a malformed
`# cron-alert:` marker and a notifier a SUCCESS-ONLY gate blocks are still
findings under it, because claiming coverage you do not have is worse than
claiming none. Only the un-adopted case — a scheduled workflow with no notifier
at all — goes quiet.

Globs every workflow like the other workflow lints; the passed file list is
ignored.
"""

import argparse
import re
import sys
from collections.abc import Collection
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    has_trigger,
    notifier_matcher,
    workflow_files,
)
from _failure_routing import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    is_failure_blocked,
    MARKER,
    notifier_steps,
    routing,
    unrouted_scheduled,
    watched_names,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
# Composite actions are NOT scanned: only a workflow can carry `on.schedule:`.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def violations(
    text: str,
    matcher: "re.Pattern[str]",
    require_alert: bool,
    watched: Collection[str] = (),
) -> list[tuple[int, str]]:
    """(1-based line, message) for every cron-alert-coverage violation in one
    workflow's source. An empty list means the workflow is either unscheduled or
    covered.

    WATCHED is the set of workflow display names the tree's failure notifiers
    list; a workflow in it is already paged for by the notifier and needs no
    notification of its own. `main` fills it in from the tree — the default
    empty set means a caller that passes one workflow in isolation simply gets
    no credit for that route, never a wrong verdict about the other three.
    """
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

    anchor = _schedule_line(text)
    route = routing(doc, text, matcher, set(watched))
    if route.marker_error:
        return [(anchor, route.marker_error)]
    if not unrouted_scheduled(route):
        return []

    # Past this point nothing routes this workflow's failures anywhere. What is
    # left is to say WHY the notification it does have (if any) does not count.
    notifiers = notifier_steps(doc, matcher)

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
            "tab. Add a notification step gated `if: failure()`, list this "
            "workflow's `name:` in a failure notifier's "
            f"`on.workflow_run.workflows`, or annotate `# {MARKER}: false  "
            "# <reason>` on the `schedule:` key.",
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


def tree_watched_names(matcher: "re.Pattern[str]") -> set[str]:
    """Every workflow display name the tree's failure notifiers already watch.

    A workflow the notifier pages for needs no notification of its own, so this
    check must see the same tree check_failure_notifier_coverage does — the two
    lints reading different inputs is what let them demand opposite things of
    one workflow. A file the parser rejects is skipped here and reported by
    `violations` on its own line.
    """
    docs = []
    for path in workflow_files(WORKFLOWS_DIR):
        try:
            doc = yaml.load(path.read_text(encoding="utf-8"), Loader=LineLoader)
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict):
            docs.append(doc)
    return watched_names(docs, matcher)


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
        "--allow-unrouted",
        action="store_true",
        help="pass a scheduled workflow that routes its failures nowhere at all; "
        "without it an unrouted cron is a failure, because a check that verifies "
        "nothing must not report success. A malformed marker and a notifier "
        "blocked by a success-only gate remain findings either way",
    )
    args = parser.parse_args(argv)
    require_alert = not args.allow_unrouted
    matcher = notifier_matcher(args.notifier_pattern)
    watched = tree_watched_names(matcher)

    total = 0
    for path in workflow_files(WORKFLOWS_DIR):
        rel = path.relative_to(REPO_ROOT)
        found = violations(
            path.read_text(encoding="utf-8"), matcher, require_alert, watched
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
        if require_alert:
            print(
                "A repo that deliberately routes its scheduled failures nowhere "
                "passes --allow-unrouted; it keeps failing on a malformed marker "
                "and on a notifier a success-only gate blocks."
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
