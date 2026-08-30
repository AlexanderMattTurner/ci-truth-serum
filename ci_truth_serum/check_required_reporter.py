#!/usr/bin/env python3
"""
Force every always() reporter on a gated workflow to declare whether it is a
required status check.

`check-always-reporter` guarantees a gated workflow carries one of two shapes
that fail closed when the gate itself fails: an `if: always()` reporter, or a
fail-closed twin. But the workflow YAML (which produces the check) and branch
protection (which decides whether the check blocks merges) drift independently:
a freshly added, green reporter silently escapes the required-status-check set,
and nothing in the repo records that it was meant to. This lint closes that gap.

For every workflow with a pull_request / pull_request_target trigger, each
`if: always()` reporter job — and each fail-closed twin
(`if: always() && needs.<gate>.result != 'success'`), which is the check run
branch protection reads when a gate fails — must carry an explicit
classification comment inside its job block:

    # required-check: true               -> must be a required status check
    # required-check: false  # <reason>  -> deliberately advisory (reason MANDATORY)

The comment must be trailing on the job's key line, or on its own line within
the job body. An unclassified reporter — or a `false` with no reason — fails.

A workflow whose only fail-closed shape is a twin needs the same comment on
each gated WORK job. The twin is SKIPPED on a healthy run, so the check runs
branch protection reads there are the work jobs' own. Leave one unclassified
and the ruleset requires nothing that a real failure can turn red.

This lint is the local, deterministic half of a pair: a consumer's apply
workflow derives the required-set from these `required-check: true` annotations
and syncs the branch-protection ruleset. It is opinionated — it assumes the
decide-job + always() reporter architecture. Any `if: always()` job (even a
cleanup job) demands a classification; mark such jobs `false` with a reason.

Opt the whole workflow out with "# not-required-check" on its pull_request:
trigger line (the same marker check-always-reporter honors).

A second, unrelated rule shares this file because both police the same
annotation: a job with a `uses:` key (a reusable-workflow call) may never
itself carry `# required-check: true`. GitHub reports that job's check run as
`<caller job name> / <called job name>`, never the job's own `name:` — the
name the apply workflow registers — so the ruleset would require a context
nothing reports and every PR would hang. The marker belongs on a thin
caller-local reporter job that `needs:` the call instead. Unlike the
reporter-classification rule above, this one applies to every workflow file:
the apply workflow reads `# required-check: true` from any job, on any
trigger, with no opt-out, so this check has the same unconditional scope.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
    _classification_text,
    _job_blocks,
    _marked_jobs,
    decide_gate_names,
    gated_work_jobs,
    has_always_reporter,
    has_fail_closed_twin,
    is_always_reporter,
    is_fail_closed_twin,
    workflow_files as _workflow_files,
)
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "not-required-check"
MARKER = "required-check"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
PR_TRIGGERS = ("pull_request", "pull_request_target")

# `# required-check: true|false` anywhere in a job block; group(rest) is the
# remainder of that source line, where a `false` must carry its `# <reason>`.
_CLASSIFY = re.compile(rf"#\s*{MARKER}\s*:\s*(true|false)\b(?P<rest>.*)")
# A non-empty trailing comment justifying an advisory classification.
_REASON = re.compile(r"#\s*\S")


def _locate_trigger(text: str, trigger: str) -> tuple[int, bool]:
    """Return (1-based line number, opted-out) for the first occurrence of trigger."""
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(rf"^\s*{trigger}\s*:", line):
            return num, annotated(line, OPT_OUT, require_reason=False)
    return 1, False


def _trigger_names(triggers: object) -> set[str]:
    """The set of trigger names an `on:` value declares, across every spelling
    (scalar / list / mapping) — so list-form `on: [pull_request, push]` is not
    silently skipped. Mirrors check_requires_concurrency's `_is_pr_triggered`."""
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        return {t for t in triggers if isinstance(t, str)}
    if isinstance(triggers, dict):
        return {k for k in triggers if isinstance(k, str)}
    return set()


def _reporter_names(jobs: dict) -> list[str]:
    """Names of jobs that report for a gate: an always() reporter (bare or
    ${{ }}-wrapped) or a fail-closed twin. Both produce the check run that
    branch protection reads on a run whose gate failed, so both demand a
    classification."""
    return [
        name
        for name, cfg in jobs.items()
        if isinstance(cfg, dict)
        and (
            is_always_reporter(cfg.get("if", ""))
            or is_fail_closed_twin(cfg.get("if", ""))
        )
    ]


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every unclassified/under-justified reporter.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text(encoding="utf-8")
    try:
        doc = safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify "
                "required-check reporter classification — fix the syntax (or run "
                "actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []

    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return []

    blocks = _job_blocks(text)
    violations: list[tuple[int, str]] = []

    # Applies to every workflow file, regardless of trigger or the
    # not-required-check opt-out below: sync-required-checks reads
    # `# required-check: true` from every job in every workflow
    # (`_marked_jobs`, unfiltered by trigger), so a `uses:` job carrying it
    # poisons the ruleset even off a pull_request trigger.
    for name in _marked_jobs(blocks, jobs):
        if "uses" in jobs[name]:
            line, _block = blocks.get(name, (1, ""))
            violations.append((line, _uses_job_required(name)))

    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    names = _trigger_names(triggers)

    pr_line: int | None = None
    opted_out = False
    for trigger in PR_TRIGGERS:
        if trigger in names:
            line, out = _locate_trigger(text, trigger)
            if pr_line is None:
                pr_line = line
            if out:
                opted_out = True
    if pr_line is None or opted_out:
        return violations

    for name in _reporter_names(jobs):
        line, block = blocks.get(name, (pr_line, ""))
        defect = _classification_defect(block)
        if defect == "unclassified":
            violations.append((line, _unclassified(name)))
        elif defect == "no-reason":
            violations.append((line, _no_reason(name)))

    # A twin-only workflow reports through its WORK jobs on every healthy run,
    # because the twin is skipped there. Each work job therefore owes the same
    # classification the reporter owes.
    gate_names = decide_gate_names(jobs)
    if gate_names and not has_always_reporter(jobs) and has_fail_closed_twin(jobs):
        for name in gated_work_jobs(jobs, gate_names):
            line, block = blocks.get(name, (pr_line, ""))
            defect = _classification_defect(block)
            if defect == "unclassified":
                violations.append((line, _unclassified_work(name)))
            elif defect == "no-reason":
                violations.append((line, _no_reason(name)))
    return violations


def _classification_defect(block: str) -> str | None:
    """'unclassified', 'no-reason', or None for a job block's marker comment."""
    match = _CLASSIFY.search(_classification_text(block))
    if match is None:
        return "unclassified"
    if match.group(1) == "false" and not _REASON.search(match.group("rest")):
        return "no-reason"
    return None


def _unclassified(name: str) -> str:
    return (
        f"reporter job '{name}' (always() reporter or fail-closed twin) is "
        "unclassified — a green reporter that "
        "nothing ties to branch protection silently escapes the required-check "
        f"set. Add '# {MARKER}: true' if it must be a required status check, or "
        f"'# {MARKER}: false  # <reason>' if it is deliberately advisory. Opt the "
        f"whole workflow out with '# {OPT_OUT}' on its pull_request: trigger."
    )


def _unclassified_work(name: str) -> str:
    return (
        f"gated work job '{name}' is unclassified, and this workflow fails closed "
        "through a twin rather than an always() reporter. The twin is SKIPPED on "
        "every healthy run, so the check run branch protection reads there is this "
        f"job's own. Add '# {MARKER}: true' if it must be a required status check, "
        f"or '# {MARKER}: false  # <reason>' if it is deliberately advisory."
    )


def _no_reason(name: str) -> str:
    return (
        f"reporter job '{name}' is marked '# {MARKER}: false' but gives "
        "no reason — append '# <reason>' explaining why it is deliberately not a "
        "required check."
    )


def _uses_job_required(name: str) -> str:
    return (
        f"job '{name}' calls a reusable workflow (`uses:`) and is marked "
        f"'# {MARKER}: true' — GitHub reports that job's check run as "
        "'<caller job name> / <called job name>', never the job's own name:, so "
        "the ruleset would require a context nothing reports and every PR would "
        "hang. Move the marker to a thin caller-local reporter job that `needs:` "
        f"'{name}' instead."
    )


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        print(
            "An unclassified always() reporter silently escapes the "
            "required-status-check set."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
