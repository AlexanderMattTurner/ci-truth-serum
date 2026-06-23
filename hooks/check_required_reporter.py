#!/usr/bin/env python3
"""
Force every always() reporter on a gated workflow to declare whether it is a
required status check.

`check-always-reporter` guarantees a gated workflow *has* an `if: always()`
reporter so it can be a required check without hanging at "Expected — Waiting".
But the workflow YAML (which produces the check) and branch protection (which
decides whether the check blocks merges) drift independently: a freshly added,
green reporter silently escapes the required-status-check set, and nothing in
the repo records that it was meant to. This lint closes that gap.

For every workflow with a pull_request / pull_request_target trigger, each
`if: always()` reporter job must carry an explicit classification comment inside
its job block:

    # required-check: true               -> must be a required status check
    # required-check: false  # <reason>  -> deliberately advisory (reason MANDATORY)

The comment must be trailing on the job's key line, or on its own line within
the job body. An unclassified reporter — or a `false` with no reason — fails.

This lint is the local, deterministic half of a pair: a consumer's apply
workflow derives the required-set from these `required-check: true` annotations
and syncs the branch-protection ruleset. It is opinionated — it assumes the
decide-job + always() reporter architecture. Any `if: always()` job (even a
cleanup job) demands a classification; mark such jobs `false` with a reason.

Opt the whole workflow out with "# not-required-check" on its pull_request:
trigger line (the same marker check-always-reporter honors).
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

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
            return num, OPT_OUT in line
    return 1, False


def _job_blocks(text: str) -> dict[str, tuple[int, str]]:
    """Map each top-level job name to (1-based key line, its source block).

    A block is the job's key line plus every following body line indented deeper
    than the key — it stops at the next line dedented to the job-key indent or
    shallower (a sibling job, an inter-job comment, or the end of `jobs:`). Blank
    lines never terminate a block. Comments thus count as classification only
    when trailing the key line or living inside the indented body.
    """
    lines = text.splitlines()
    jobs_idx = next(
        (i for i, line in enumerate(lines) if re.match(r"^jobs\s*:", line)), None
    )
    if jobs_idx is None:
        return {}

    job_indent = next(
        (
            len(line) - len(line.lstrip())
            for line in lines[jobs_idx + 1 :]
            if line.strip() and not line.lstrip().startswith("#")
        ),
        None,
    )
    if job_indent is None:
        return {}

    blocks: dict[str, tuple[int, str]] = {}
    key = re.compile(rf"^\s{{{job_indent}}}([^\s:#][^:]*?)\s*:")
    i = jobs_idx + 1
    while i < len(lines):
        stripped = lines[i].strip()
        indent = len(lines[i]) - len(lines[i].lstrip())
        if stripped and not stripped.startswith("#") and indent < job_indent:
            break
        match = key.match(lines[i])
        if not (match and indent == job_indent and not stripped.startswith("#")):
            i += 1
            continue
        end = i + 1
        while end < len(lines):
            body = lines[end]
            if body.strip() and len(body) - len(body.lstrip()) <= job_indent:
                break
            end += 1
        name = match.group(1).strip("'\"")  # align with PyYAML's unquoted key
        blocks[name] = (i + 1, "\n".join(lines[i:end]))
        i = end
    return blocks


def _classification_text(block: str) -> str:
    """The lines of a job block where a classification comment may live: the key
    line plus the job's direct-child lines (a trailing comment on a child, or a
    standalone comment at the child indent). Deeper step/run content is excluded
    so a `# required-check:` string buried in a step can't pass as a classification.
    """
    lines = block.splitlines()
    if not lines:
        return ""
    child_indent = next(
        (len(ln) - len(ln.lstrip()) for ln in lines[1:] if ln.strip()), None
    )
    eligible = [lines[0]]
    if child_indent is not None:
        eligible += [
            ln
            for ln in lines[1:]
            if ln.strip() and len(ln) - len(ln.lstrip()) == child_indent
        ]
    return "\n".join(eligible)


def _reporter_names(jobs: dict) -> list[str]:
    """Names of jobs whose `if` is exactly `always()` — the reporter shape."""
    return [
        name
        for name, cfg in jobs.items()
        if isinstance(cfg, dict) and str(cfg.get("if", "")) == "always()"
    ]


def check_file(path: Path) -> list[tuple[int, str]]:
    """Return (line, message) for every unclassified/under-justified reporter."""
    text = path.read_text()
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        return []

    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict):
        return []

    pr_line: int | None = None
    opted_out = False
    for trigger in PR_TRIGGERS:
        if trigger in triggers:
            line, out = _locate_trigger(text, trigger)
            if pr_line is None:
                pr_line = line
            if out:
                opted_out = True
    if pr_line is None or opted_out:
        return []

    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return []

    blocks = _job_blocks(text)
    violations: list[tuple[int, str]] = []
    for name in _reporter_names(jobs):
        line, block = blocks.get(name, (pr_line, ""))
        match = _CLASSIFY.search(_classification_text(block))
        if match is None:
            violations.append((line, _unclassified(name)))
        elif match.group(1) == "false" and not _REASON.search(match.group("rest")):
            violations.append((line, _no_reason(name)))
    return violations


def _unclassified(name: str) -> str:
    return (
        f"always() reporter job '{name}' is unclassified — a green reporter that "
        "nothing ties to branch protection silently escapes the required-check "
        f"set. Add '# {MARKER}: true' if it must be a required status check, or "
        f"'# {MARKER}: false  # <reason>' if it is deliberately advisory. Opt the "
        f"whole workflow out with '# {OPT_OUT}' on its pull_request: trigger."
    )


def _no_reason(name: str) -> str:
    return (
        f"always() reporter job '{name}' is marked '# {MARKER}: false' but gives "
        "no reason — append '# <reason>' explaining why it is deliberately not a "
        "required check."
    )


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        for line, message in check_file(path):
            print(f"::error file={path.relative_to(REPO_ROOT)},line={line}::{message}")
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
