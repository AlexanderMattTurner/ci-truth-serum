#!/usr/bin/env python3
"""Enforce the second cron question: not "did it fail?" but "did it run at all?"

`check-cron-alert-coverage` catches a scheduled workflow that RUNS AND FAILS
without telling anyone. It is structurally blind to the cron that produces no
failure because it produces nothing — and all four ways that happens look
exactly like a healthy quiet week:

  * GitHub DISABLES a dormant repository's `schedule:` triggers after 60 days;
  * an unparseable workflow yields a run with zero jobs (`startup_failure`);
  * a run whose jobs never match their `if:` ends `skipped`;
  * a run superseded every cycle by concurrency ends `cancelled`.

None of those is a failed run, so a failure notifier fires never, correctly, and
the guard has been dead for two months.

`# cron-alert: false` must NOT satisfy this. They are different questions, and
the direction of the reasoning is the point: essentially every real-world
alert opt-out reason is a variant of "a failed fire self-heals next cycle" —
which PRESUMES there is a next cycle. That premise is void exactly when the
schedule stops. So a failure-alert opt-out is not a staleness opt-out; it is an
argument FOR watching staleness, and this lint refuses to read one as the other.

Answering the question at RUNTIME needs a watchdog that queries the Actions API
for each cron's last run — which is not something a pre-commit hook pack can
ship (it has no distribution channel for workflows). What IS static, and what
this lint enforces, is the marker discipline plus the existence of whatever
watchdog a consumer declares:

  * `# cron-stale: false  # <reason>` on the `schedule:` key line or a
    direct-child line of it. Same shape and same mandatory-reason rule as
    `# cron-alert:`: a missing reason, or a negative placeholder ("n/a",
    "none", "not needed"), is a failure — the reason is the whole marker.
  * `--watchdog-workflow NAME` names the workflow that does the runtime
    watching. It must exist under `.github/workflows/` AND itself be
    schedule-triggered; a watchdog that never fires is a vacuous green, which
    is worse than no watchdog at all.
  * `--require-stale-marker` demands a well-formed marker on every scheduled
    workflow. When a valid `--watchdog-workflow` is also declared, the demand
    narrows to that watchdog alone — it is the one cron nothing else watches.
    A watchdog that ALSO fires on a non-`schedule:` trigger (push,
    workflow_run, …) owes no marker either: GitHub's 60-day dormancy disable
    applies to `schedule:` only, so an always-live trigger keeps firing in
    exactly the scenario the marker would wave away — and a consumer whose
    runtime sweep reads the same marker as an opt-out would otherwise be
    forced to drop the watchdog from its own watched set. (An always-live
    trigger excludes the dominant silencing mode; whether the sweep also
    reads its own schedule's history is not statically knowable.)

Without flags the hook is silent in a repo that has not adopted the pattern and
only reports a marker that is present but malformed: a noisy guard gets disabled
and teaches nothing. Globs every workflow like the other workflow lints; the
passed file list is ignored.
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    WORKFLOW_GLOBS,
    has_trigger,
    key_block_lines,
    parse_optout_marker,
    workflow_triggers,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

MARKER = "cron-stale"
ALERT_MARKER = "cron-alert"

# Why the sibling marker is not an answer to this question — quoted at the point
# of failure so the distinction is legible where it actually bites.
NON_SUBSTITUTION = (
    f"a `# {ALERT_MARKER}: false` marker here does NOT answer this: its reason "
    "argues that a failed fire self-heals next cycle, which presumes there IS a "
    "next cycle — the exact premise a stopped schedule voids."
)


def _schedule_line(text: str) -> int:
    """The 1-based line of the `schedule:` key — the anchor for a finding about
    the workflow's schedule; 1 when the key cannot be located."""
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(r"^[ \t]*schedule\s*:", line):
            return num
    return 1


def workflow_files() -> list[Path]:
    return sorted(p for glob in WORKFLOW_GLOBS for p in WORKFLOWS_DIR.glob(glob))


def is_scheduled(text: str) -> bool | None:
    """True/False for a parseable workflow, None when the YAML does not parse."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return isinstance(doc, dict) and has_trigger(doc, "schedule")


def violations(text: str, require_marker: bool, watched: bool) -> list[tuple[int, str]]:
    """(1-based line, message) for one workflow's source. WATCHED says a valid
    runtime watchdog covers this file, which excuses a missing marker (never a
    malformed one)."""
    scheduled = is_scheduled(text)
    if scheduled is None:
        return [
            (
                1,
                "could not parse as YAML; cannot verify whether this workflow is "
                "scheduled, so its staleness opt-out cannot be checked — fix the "
                "syntax (or run actionlint) and re-check. An unparseable workflow "
                "is itself one of the ways a cron silently stops running: GitHub "
                "starts the run, finds no jobs, and ends it `startup_failure` — "
                "never a failure anyone is notified about.",
            )
        ]
    if not scheduled:
        return []

    schedule_lines = key_block_lines(text, "schedule")
    reason, marker_error = parse_optout_marker(schedule_lines, MARKER)
    if marker_error:
        return [(_schedule_line(text), marker_error)]
    if reason or watched or not require_marker:
        return []

    alert_reason, _ = parse_optout_marker(schedule_lines, ALERT_MARKER)
    tail = f" Note that {NON_SUBSTITUTION}" if alert_reason else ""
    return [
        (
            _schedule_line(text),
            "no `# cron-stale:` marker covers this schedule, and no runtime "
            "watchdog reaches it — nothing answers whether this cron still "
            "RUNS. A schedule GitHub disabled "
            "for repo dormancy, a run that ends `startup_failure`/`skipped`/"
            "`cancelled` — none of those is a failed run, so a failure notifier "
            f"reports nothing and the silence reads as health. Annotate `# {MARKER}"
            ": false  # <why a stopped schedule here is harmless>`, or declare a "
            f"runtime watchdog with --watchdog-workflow.{tail}",
        )
    ]


def watchdog_violations(name: str) -> list[str]:
    """Findings about the declared watchdog itself. A watchdog that is missing,
    unparseable, or not scheduled reports success while watching nothing — the
    vacuous green this flag exists to make impossible."""
    path = WORKFLOWS_DIR / name
    if not path.exists():
        return [
            f"::error::--watchdog-workflow names {name}, which does not exist under "
            f"{WORKFLOWS_DIR.relative_to(REPO_ROOT)}/. A staleness watchdog that "
            "isn't there reports nothing, forever, and looks identical to one "
            "reporting all-clear."
        ]
    rel = path.relative_to(REPO_ROOT)
    scheduled = is_scheduled(path.read_text(encoding="utf-8"))
    if scheduled is None:
        return [
            f"::error file={rel}::the declared staleness watchdog does not parse as "
            "YAML, so it can never start a job."
        ]
    if not scheduled:
        return [
            f"::error file={rel}::the declared staleness watchdog has no `schedule:` "
            "trigger, so it never fires on its own — nothing would notice a cron "
            "that stopped."
        ]
    return []


def _has_non_schedule_trigger(text: str) -> bool:
    """True when the workflow's `on:` block declares any trigger besides
    `schedule:` — a trigger GitHub's dormancy disable cannot kill, so the
    workflow is not solely cron-driven. Caller guarantees TEXT parses."""
    triggers = workflow_triggers(yaml.safe_load(text))
    if isinstance(triggers, dict):
        return any(str(name) != "schedule" for name in triggers)
    if isinstance(triggers, list):
        return any(str(name) != "schedule" for name in triggers)
    return triggers is not None and str(triggers) != "schedule"


def check_repo(require_marker: bool, watchdog: str | None) -> list[str]:
    """Every staleness-opt-out violation for the repo, as printable messages."""
    found = watchdog_violations(watchdog) if watchdog else []
    watchdog_ok = watchdog is not None and not found
    # A valid schedule-only watchdog covers every cron but its own: nothing
    # watches the watcher, so it is the one file that still owes a marker. But a
    # watchdog that ALSO fires on a non-schedule trigger is covered structurally
    # — dormancy cannot silence it, so demanding a marker would assert something
    # false about the one workflow whose staleness IS observed (and a runtime
    # sweep reading that marker as an opt-out would drop the watchdog from its
    # own watched set — the issue #81 failure).
    self_covered = watchdog_ok and _has_non_schedule_trigger(
        (WORKFLOWS_DIR / watchdog).read_text(encoding="utf-8")
    )
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        watched = watchdog_ok and (path.name != watchdog or self_covered)
        for line, message in violations(
            path.read_text(encoding="utf-8"), require_marker, watched
        ):
            found.append(f"::error file={rel},line={line}::{message}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-stale-marker",
        action="store_true",
        help="fail a scheduled workflow with no `# cron-stale: false  # <reason>` "
        "marker (narrowed to the watchdog itself when --watchdog-workflow is set; "
        "a watchdog that also fires on a non-schedule trigger owes none)",
    )
    parser.add_argument(
        "--watchdog-workflow",
        metavar="FILENAME",
        help="the workflow file under .github/workflows/ that watches every cron "
        "for staleness at runtime; it must exist and be schedule-triggered",
    )
    args = parser.parse_args(argv)

    found = check_repo(args.require_stale_marker, args.watchdog_workflow)
    for message in found:
        print(message)
    if found:
        print(f"\nERROR: {len(found)} cron-staleness-optout violation(s) found.")
        print(
            "A cron that stopped running produces no failure — the one silence a "
            "failure notifier can never break."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
