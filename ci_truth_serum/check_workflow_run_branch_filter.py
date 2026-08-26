#!/usr/bin/env python3
"""Fail a `workflow_run` listener that carries no branch filter.

PROBLEM CLASS — a `workflow_run` listener without `branches:` or
`branches-ignore:` gets a workflow run created for EVERY completion of every
workflow it names, on every branch. The listener's own `if:` runs after run
creation, so a run whose jobs all skip still costs the account one run through
the same path a real run takes. The observed cost: about 3,000 such no-op runs
sat `in_progress` at once, GitHub stopped dispatching `merge_group` check runs
for about 7 hours, and the merge queue merged nothing in that window. Nothing
was red — every one of those runs reported success.

The filter matches the HEAD BRANCH of the triggering run, not the branch this
listener runs from (a `workflow_run` workflow always runs from the default
branch). So a notifier that acts only on default-branch failures declares
`branches: [main]`, and every pull-request completion then creates no run at
all instead of creating one that skips.

Three findings:

  * a `workflow_run:` trigger with neither filter key;
  * a filter that narrows nothing — `branches: ['*']`, `branches: ['**']`, or
    an empty list under either key. It reads as a filter in review and creates
    the same runs the missing one does;
  * a `# unfiltered-listener-ok:` marker that states no reason. The reason is
    the whole marker, because it is the only part a reviewer can check.

A listener that must act on completions from every branch opts out with
`# unfiltered-listener-ok: <reason>` on the `workflow_run:` key line or one of
its direct-child lines. A placeholder such as "n/a" does not count.

Globs every workflow like the other workflow lints; the passed file list is
ignored. Composite actions are not scanned: only a workflow can carry `on:`.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    is_placeholder_reason,
    key_block_lines,
    workflow_files,
    workflow_triggers,
)
from _fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

MARKER = "unfiltered-listener-ok"
FILTER_KEYS = ("branches", "branches-ignore")
# A pattern that every branch name matches, so a list of only these narrows
# nothing. `*` and `**` differ on `/` in a branch name; both accept everything
# a listener would otherwise see.
WILDCARDS = frozenset({"*", "**"})

# An annotation READER (it extracts the reason so a placeholder can be
# rejected), not a boolean opt-out predicate. The lead mirrors
# `_linecheck.annotation_re`: the token may follow the `#` directly, or after
# same-line comment text whose last character cannot belong to a token, so a
# longer slug never satisfies it.
_MARKER_READER = re.compile(
    rf"#(?:[^\r\n]*[^\w\r\n-])?{MARKER}\s*:\s*(?P<reason>[^\r\n]*)$"
)

_KEY_LINE = re.compile(r"^\s*workflow_run\s*:")
_ON_LINE = re.compile(r"^on\s*:")


def trigger_line(text: str) -> int:
    """The 1-based line of the `workflow_run:` key, or of `on:` when the
    trigger is written in the list/scalar spelling that has no key of its own.

    A line scan rather than the parsed document, for the usual reason: PyYAML
    reports a mapping's first CHILD key and discards the rest of the layout.
    """
    lines = text.splitlines()
    for number, line in enumerate(lines, start=1):
        if _KEY_LINE.match(line):
            return number
    for number, line in enumerate(lines, start=1):
        if _ON_LINE.match(line):
            return number
    return 1


def marker_window(text: str) -> list[str]:
    """The lines a `# unfiltered-listener-ok:` marker for this trigger may sit
    on: the `workflow_run:` key line and its direct children, or the `on:`
    block when the trigger carries no key line of its own."""
    return key_block_lines(text, "workflow_run") or key_block_lines(text, "on")


def marker_state(window: list[str]) -> tuple[bool, str | None]:
    """(marker present, error detail) for the marker on WINDOW.

    The detail ("carries no reason" / "states only 'n/a'") is set when a marker
    is present but states a placeholder, which suppresses nothing.
    """
    details = []
    for line in window:
        match = _MARKER_READER.search(line)
        if not match:
            continue
        reason = match.group("reason").strip().lstrip("#").strip()
        if not is_placeholder_reason(reason):
            return True, None
        details.append(f"states only {reason!r}" if reason else "carries no reason")
    if details:
        return True, details[0]
    return False, None


def vacuous_filter(trigger: dict) -> str | None:
    """The name of a filter key in TRIGGER that narrows nothing, else None.

    An empty list under either key excludes no completion. Under `branches:`, a
    list of only `*`/`**` accepts every branch, which is the state the missing
    key already describes.
    """
    for key in FILTER_KEYS:
        if key not in trigger:
            continue
        value = trigger[key]
        patterns = value if isinstance(value, list) else [value]
        if not patterns:
            return key
        if key == "branches" and all(str(p) in WILDCARDS for p in patterns):
            return key
    return None


def unfiltered_reason(trigger: object) -> str | None:
    """Why TRIGGER creates a run for every branch, or None when it filters.

    TRIGGER is the parsed `workflow_run` value, or None for the bare-name
    spelling (`on: [workflow_run]`), which holds no place to put a filter.
    """
    if not isinstance(trigger, dict):
        return (
            "this `workflow_run` trigger is written as a bare name, so it can "
            "carry no `branches:` filter — every completion of every watched "
            "workflow, on every branch, creates a run here. Write it as a "
            "mapping with `workflows:` and `branches:`, or justify with "
            f"`# {MARKER}: <reason>`."
        )
    vacuous = vacuous_filter(trigger)
    if vacuous:
        return (
            f"this `workflow_run` trigger's `{vacuous}:` narrows nothing, so "
            "every completion of every watched workflow still creates a run "
            "here. It reads as a filter in review and creates the runs a "
            "missing filter creates. Name the branches this listener acts on, "
            f"or justify with `# {MARKER}: <reason>`."
        )
    if any(key in trigger for key in FILTER_KEYS):
        return None
    return (
        "this `workflow_run` trigger has no `branches:`/`branches-ignore:` "
        "filter, so every completion of every watched workflow creates a run "
        "here — including the ones this workflow's own `if:` then discards, "
        "because the gate runs after the run exists. At fleet scale those "
        "no-op runs saturate the run-creation path and stall the merge queue. "
        "Add the branches whose completions this listener acts on, or justify "
        f"with `# {MARKER}: <reason>`."
    )


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for the `workflow_run` filter violations in one
    workflow's source. Empty when the workflow has no `workflow_run` trigger."""
    try:
        doc = safe_load(text)
    except yaml.YAMLError as err:
        return [
            (
                1,
                "could not parse as YAML "
                f"({str(err).partition(chr(10))[0]}); cannot tell whether this "
                "workflow listens on `workflow_run` without a branch filter — "
                "fix the syntax (or run actionlint) and re-check.",
            )
        ]
    triggers = workflow_triggers(doc)
    if isinstance(triggers, dict):
        if "workflow_run" not in triggers:
            return []
        trigger = triggers["workflow_run"]
    elif isinstance(triggers, list):
        if "workflow_run" not in triggers:
            return []
        trigger = None
    elif triggers == "workflow_run":
        trigger = None
    else:
        return []

    defect = unfiltered_reason(trigger)
    if defect is None:
        # The trigger names its branches. A marker here suppresses nothing, so
        # its reason is never judged — a stale one is not a finding.
        return []

    line = trigger_line(text)
    present, detail = marker_state(marker_window(text))
    if detail:
        return [
            (
                line,
                f"`# {MARKER}:` on this `workflow_run` trigger {detail}. The "
                "reason IS the marker — write "
                f"`# {MARKER}: <why this listener must see every branch>` so a "
                "reviewer can check the argument instead of the annotation.",
            )
        ]
    if present:
        return []
    return [(line, defect)]


def main() -> int:
    total = 0
    for path in workflow_files(WORKFLOWS_DIR):
        rel = path.relative_to(REPO_ROOT)
        for line, message in violations(path.read_text(encoding="utf-8")):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} unfiltered workflow_run listener(s) found.")
        print(
            "A listener with no branch filter creates a run for every "
            "completion it watches, on every branch — the skipped ones cost "
            "the same run-creation path as the real ones."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
