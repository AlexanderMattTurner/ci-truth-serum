#!/usr/bin/env python3
"""Fail a multi-cron workflow whose jobs cannot tell one cron from another.

GitHub does not route a scheduled run to one job: every cron a workflow file
declares starts EVERY job in that file that is eligible for the schedule
event. A job gated `github.event_name == 'schedule'` therefore runs for all
of them, and adding a second cron silently multiplies what the first one
runs — nothing fails. The observed case: a weekly cron added to an eval
workflow that already had one also started the other job, a paid eval, on a
schedule nobody asked for, and the bill was the only signal.

Scope: only a workflow declaring two or more crons. With a single cron,
`github.event_name == 'schedule'` is unambiguous, so those files are exempt.

For a multi-cron workflow, three obligations:

  1. no job `if:` may gate on the bare event-name test — a schedule-gated
     job names its cron (`github.event.schedule == '13 5 * * 1'`). The
     event-name test beside a named cron is redundant but harmless, so it is
     flagged only when the `if:` names no declared cron at all;
  2. every declared cron must be answered by some job — named in a gated
     job's `if:`, or covered by a job that runs on every fire (see 3).
     At-least-once, not exactly-once: two jobs answering one cron, and one
     job naming several crons, are both legitimate;
  3. a job with no `if:` runs on every cron. It must carry
     `# multi-cron-ok: <reason>` on its key line or a direct-child line —
     the reason is REQUIRED, and a negative placeholder ("n/a") does not
     count, because the reason is the only thing a reviewer can check.

The `if:` expressions are GitHub expression syntax, not YAML, so they are
inspected as strings, narrowly: a job "names" a cron when the cron appears
as a quoted literal in its `if:`, and the bare event-name test is recognized
as an `==` between `github.event_name` and the literal `schedule` (either
operand order, either quote style).

Globs every workflow like the other workflow lints; the passed file list is
ignored. Composite actions are not scanned: only a workflow can carry
`on.schedule:`.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    WORKFLOW_GLOBS,
    _classification_text,
    _job_blocks,
    is_placeholder_reason,
    workflow_triggers,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

MARKER = "multi-cron-ok"

# The bare event-name test inside a GitHub `if:` expression — string
# inspection, because the expression is GitHub syntax, not YAML. Assumes the
# `==`-comparison spellings (either operand order, either quote style); a
# `!=` excludes schedule fires and is out of scope.
_BARE_SCHEDULE = re.compile(
    r"github\.event_name\s*==\s*['\"]schedule['\"]"
    r"|['\"]schedule['\"]\s*==\s*github\.event_name"
)

# An annotation READER (it extracts the reason so a placeholder can be
# rejected), not a boolean opt-out predicate — the value grammar is this
# module's own, like `# gate-deps: <paths>`.
_MARKER_READER = re.compile(rf"#\s*{MARKER}\s*:\s*(?P<reason>[^\r\n]*)$")


def _names(expr: str, cron: str) -> bool:
    """True when the `if:` expression EXPR names CRON: the cron appears as a
    quoted literal, whatever the surrounding comparison — so a
    `contains(fromJSON(...), github.event.schedule)` shape still counts."""
    return f"'{cron}'" in expr or f'"{cron}"' in expr


def _marker_state(scoped_lines: str) -> tuple[bool, str | None]:
    """(marker present, error detail) for a job's classification lines.

    The detail ("carries no reason" / "states only 'n/a'") is set when the
    marker is present but its reason is a negative placeholder — the reason is
    the entire point of the marker, because it is the only thing a reviewer
    can actually check."""
    for line in scoped_lines.splitlines():
        match = _MARKER_READER.search(line)
        if not match:
            continue
        reason = match.group("reason").strip().lstrip("#").strip()
        if is_placeholder_reason(reason):
            detail = f"states only {reason!r}" if reason else "carries no reason"
            return True, detail
        return True, None
    return False, None


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for every multi-cron-gating violation in one
    workflow's source. Empty when the workflow declares fewer than two crons."""
    try:
        doc = yaml.load(text, Loader=LineLoader)
    except yaml.YAMLError as err:
        return [
            (
                1,
                "could not parse as YAML "
                f"({str(err).partition(chr(10))[0]}); cannot verify that this "
                "workflow's jobs can tell one cron from another — fix the "
                "syntax (or run actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []
    triggers = workflow_triggers(doc)
    if not isinstance(triggers, dict):
        return []
    schedule = triggers.get("schedule")
    if not isinstance(schedule, list):
        return []
    crons = [
        (str(entry["cron"]), entry.get("__line__", 1))
        for entry in schedule
        if isinstance(entry, dict) and "cron" in entry
    ]
    if len(crons) < 2:
        return []  # one cron: `github.event_name == 'schedule'` is unambiguous
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []

    blocks = _job_blocks(text)
    example = crons[0][0]
    answered: set[str] = set()
    found: list[tuple[int, str]] = []
    for name, cfg in jobs.items():
        if name == "__line__" or not isinstance(cfg, dict):
            continue
        line = blocks.get(str(name), (1, ""))[0]
        if cfg.get("if") is not None:
            expr = str(cfg["if"])
            named = {cron for cron, _ in crons if _names(expr, cron)}
            answered |= named
            if not named and _BARE_SCHEDULE.search(expr):
                # This gate accepts every schedule fire, so it answers every
                # cron — in exactly the undifferentiated way flagged here.
                answered.update(cron for cron, _ in crons)
                found.append(
                    (
                        line,
                        f"job '{name}' gates on `github.event_name == "
                        f"'schedule'` alone, but this workflow declares "
                        f"{len(crons)} crons and GitHub starts every "
                        "schedule-eligible job on EVERY cron fire — this job "
                        "runs for all of them. Name the cron(s) it answers to "
                        f"instead: `github.event.schedule == '{example}'`.",
                    )
                )
            continue
        # No `if:` — every cron fire starts this job; whether that is
        # deliberate is what the marker records.
        answered.update(cron for cron, _ in crons)
        block = blocks.get(str(name))
        present, detail = _marker_state(_classification_text(block[1]) if block else "")
        if detail:
            found.append(
                (
                    line,
                    f"`# {MARKER}:` on job '{name}' {detail}. The reason IS "
                    f"the marker — write `# {MARKER}: <why this job answers "
                    "to every cron>` so a reviewer can check the argument "
                    "instead of the annotation.",
                )
            )
        elif not present:
            found.append(
                (
                    line,
                    f"job '{name}' has no `if:` in a workflow with "
                    f"{len(crons)} crons, so every one of them starts it. "
                    "Gate it on the cron(s) it answers to (`if: "
                    f"github.event.schedule == '{example}'`), or annotate "
                    f"`# {MARKER}: <reason>` on the job if it genuinely "
                    "answers to every schedule.",
                )
            )

    for cron, cron_line in crons:
        if cron not in answered:
            found.append(
                (
                    cron_line,
                    f"cron '{cron}' is declared but no job's `if:` names it — "
                    "it fires and starts none of the schedule-gated jobs, a "
                    "schedule nobody answers to. Gate a job on "
                    f"`github.event.schedule == '{cron}'`, or delete the cron.",
                )
            )
    return found


def workflow_files() -> list[Path]:
    return sorted(p for glob in WORKFLOW_GLOBS for p in WORKFLOWS_DIR.glob(glob))


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in violations(path.read_text(encoding="utf-8")):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} multi-cron-gating violation(s) found.")
        print(
            "GitHub starts every schedule-eligible job on every cron fire — "
            "a job that cannot name its cron runs on all of them."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
