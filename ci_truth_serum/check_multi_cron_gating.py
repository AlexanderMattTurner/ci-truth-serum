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
     flagged only when the `if:` names no declared cron at all. A gate that
     compares `github.event.schedule` to a cron the file does NOT declare is
     its own finding: the job never fires on it, which is usually a typo;
  2. every declared cron must be answered by some job — named in a gated
     job's `if:`, or covered by a job that runs on every fire (see 3).
     At-least-once, not exactly-once: two jobs answering one cron, and one
     job naming several crons, are both legitimate;
  3. a job that runs on every cron must carry `# multi-cron-ok: <reason>`
     on its key line or a direct-child line — the reason is REQUIRED, and a
     negative placeholder ("n/a") does not count, because the reason is the
     only thing a reviewer can check. "Runs on every cron" means: no `if:`
     (or a vacuous `if: always()` / `if: true`) and no `needs:`. A job
     whose only gate is `needs:` inherits its dependencies' gating — GitHub
     skips a job whose needed job was skipped — so it is exempt, and it
     answers no cron of its own. `if: always()` defeats that inheritance,
     so it counts as running on every cron even beside `needs:`.

The `if:` expressions are GitHub expression syntax, not YAML, so they are
inspected as strings, narrowly: a job "names" a cron when the cron appears
as a quoted literal in its `if:` outside a `!=` comparison (a negated cron
is the one the job excludes), and the bare event-name test is recognized
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
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    _classification_text,
    _job_blocks,
    is_placeholder_reason,
    unwrap_expression,
    workflow_files,
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

# A `github.event.schedule == '<literal>'` comparison, either operand order.
# What it captures is checked against the DECLARED crons: an equality against
# a cron the file never declares is a gate that can never fire.
_SCHEDULE_EQ = re.compile(
    r"github\.event\.schedule\s*==\s*(?P<q>['\"])(?P<lit>[^'\"]*)(?P=q)"
    r"|(?P<q2>['\"])(?P<lit2>[^'\"]*)(?P=q2)\s*==\s*github\.event\.schedule"
)

# An annotation READER (it extracts the reason so a placeholder can be
# rejected), not a boolean opt-out predicate — the value grammar is this
# module's own, like `# gate-deps: <paths>`. The lead mirrors
# `_cts_linecheck.annotation_re`: the token may follow the `#` directly or after
# same-line comment text whose last character cannot belong to a token, so a
# longer slug (`# not-multi-cron-ok:`) never satisfies this marker.
_MARKER_READER = re.compile(
    rf"#(?:[^\r\n]*[^\w\r\n-])?{MARKER}\s*:\s*(?P<reason>[^\r\n]*)$"
)


def _names(expr: str, cron: str) -> bool:
    """True when the `if:` expression EXPR names CRON: the cron appears as a
    quoted literal outside a `!=` comparison. The comparison shape is
    otherwise unconstrained, so `contains(fromJSON(...))` counts — but a
    negated occurrence names the cron the job EXCLUDES, never coverage."""
    for quote in ("'", '"'):
        literal = f"{quote}{cron}{quote}"
        start = 0
        while (idx := expr.find(literal, start)) != -1:
            start = idx + 1
            before = expr[:idx].rstrip()
            after = expr[idx + len(literal) :].lstrip()
            if before.endswith("!=") or after.startswith("!="):
                continue
            return True
    return False


def _phantom_literals(expr: str, declared: set[str]) -> list[str]:
    """The literals EXPR compares `github.event.schedule` equal to that are
    not in DECLARED — each is a gate that can never fire, usually a typo."""
    literals = []
    for match in _SCHEDULE_EQ.finditer(expr):
        lit = match.group("lit")
        if lit is None:
            lit = match.group("lit2")
        if lit not in declared and lit not in literals:
            literals.append(lit)
    return literals


def _marker_state(scoped_lines: str) -> tuple[bool, str | None]:
    """(marker present, error detail) for a job's classification lines.

    The detail ("carries no reason" / "states only 'n/a'") is set when the
    marker is present but its reason is a negative placeholder — the reason is
    the entire point of the marker, because it is the only thing a reviewer
    can actually check."""
    details = []
    for line in scoped_lines.splitlines():
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
    declared = {cron for cron, _ in crons}
    if len(declared) < 2:
        # One distinct cron (however many times listed): the bare
        # `github.event_name == 'schedule'` gate is unambiguous.
        return []
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
        block = blocks.get(str(name))
        line = block[0] if block else 1
        expr = None if cfg.get("if") is None else unwrap_expression(cfg["if"])
        # `always()` and `true` gate nothing, so those jobs are judged as
        # ungated rather than credited with an `if:` they don't really have.
        if expr is not None and expr.lower() not in ("always()", "true"):
            named = {cron for cron in declared if _names(expr, cron)}
            answered |= named
            for phantom in _phantom_literals(expr, declared):
                found.append(
                    (
                        line,
                        f"job '{name}' compares `github.event.schedule` to "
                        f"'{phantom}', a cron this workflow does not declare — "
                        "the comparison can never be true, so the job never "
                        "fires on it. Declare that cron, or fix the "
                        "comparison to one of the declared crons.",
                    )
                )
            if not named and _BARE_SCHEDULE.search(expr):
                # This gate accepts every schedule fire, so it answers every
                # cron — in exactly the undifferentiated way flagged here.
                answered |= declared
                found.append(
                    (
                        line,
                        f"job '{name}' gates on `github.event_name == "
                        f"'schedule'` alone, but this workflow declares "
                        f"{len(declared)} crons and GitHub starts every "
                        "schedule-eligible job on EVERY cron fire — this job "
                        "runs for all of them. Name the cron(s) it answers to "
                        f"instead: `github.event.schedule == '{example}'`.",
                    )
                )
            continue
        if (expr is None or expr.lower() == "true") and cfg.get("needs"):
            # A `needs:`-gated job runs only when its needed jobs ran (GitHub
            # skips it when a dependency was skipped, and a plain `if:` has
            # success() implied), so their gates decide which crons reach it
            # and it answers none of its own. `always()` defeats that
            # inheritance, which is why it does not take this exit.
            continue
        # This job starts on EVERY cron fire; whether that is deliberate is
        # what the marker records.
        answered |= declared
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
                    f"job '{name}' has no cron-distinguishing `if:` in a "
                    f"workflow with {len(declared)} crons, so every one of "
                    "them starts it. "
                    "Gate it on the cron(s) it answers to (`if: "
                    f"github.event.schedule == '{example}'`), or annotate "
                    f"`# {MARKER}: <reason>` on the job if it genuinely "
                    "answers to every schedule.",
                )
            )

    reported: set[str] = set()
    for cron, cron_line in crons:
        if cron in answered or cron in reported:
            continue
        reported.add(cron)
        found.append(
            (
                cron_line,
                f"cron '{cron}' is declared but no job's `if:` names it — "
                "it fires and starts none of the schedule-gated jobs, a "
                "schedule nobody answers to. Gate a job on "
                f"`github.event.schedule == '{cron}'`, or delete the cron.",
            )
        )
    return sorted(found)


def main() -> int:
    total = 0
    for path in workflow_files(WORKFLOWS_DIR):
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
