#!/usr/bin/env python3
"""
Flag `github.event.pull_request.head.sha` in a step's `run:` or `with:` value.

The event payload is frozen at trigger time: `github.event.pull_request.head.sha`
is the head commit as it stood when the workflow was *queued*, not the commit the
job actually checked out. A force-push or the routine autofix-amend (which
rewrites the PR head and force-pushes) moves the real head after the trigger
fires, so a step that scopes a diff/range to that stale SHA silently mis-scopes:
`git diff <frozen>...HEAD` spans the whole branch history (the frozen SHA is no
longer an ancestor), and `actions/checkout` `ref: <frozen>` fetches a commit that
may no longer exist. The correct head is derived from the checkout itself —
`git rev-parse HEAD` after `actions/checkout` — never from the event payload.

This lint scans every workflow/composite-action step's inline `run:` script and
its `with:` input values for the frozen expression and fails on each hit. Only
`head.sha` is matched: `github.event.pull_request.base.sha` (the correct base for
a range) and `github.event.pull_request.head.ref` (a branch name, re-resolved on
checkout) are legitimate and untouched.

A genuine use — pinning `--force-with-lease=<ref>:<frozen>` so a concurrent push
rejects the amend, where the *point* is to compare against the pre-trigger head —
opts out with a `# frozen-head-ok: <reason>` comment anywhere in the step block.
The reason is mandatory.

Not scanned: step/job `env:` values (a head.sha routed through an env var then
used in `run:`) — out of scope to keep false positives low; the direct `run:` /
`with:` sites are where this bug has recurred.

Globs every workflow + composite action like check_inline_run_length; argv is
ignored.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import LineLoader as _LineLoader  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
ALLOW = "frozen-head-ok"

# The frozen head-SHA context. `\b` after `sha` keeps `head.sha` from also
# matching a hypothetical `head.sha_short`; `base.sha` and `head.ref` never match.
FROZEN = re.compile(r"github\.event\.pull_request\.head\.sha\b")
# The opt-out: `# frozen-head-ok: <reason>` with a non-empty reason after the colon.
_ALLOW_RE = re.compile(rf"#\s*{ALLOW}\s*:\s*\S")

_MESSAGE = (
    "step uses github.event.pull_request.head.sha in run:/with: — the event "
    "payload is frozen at trigger time, so a force-push / autofix-amend moves the "
    "real head and this mis-scopes the range (git diff <frozen>...HEAD spans the "
    "whole branch; checkout ref:<frozen> fetches a stale/absent commit). Derive "
    "the head from the checkout instead (git rev-parse HEAD after actions/checkout)."
    f" If comparing against the pre-trigger head is the point (e.g. a "
    f"--force-with-lease pin), annotate the step with '# {ALLOW}: <reason>'."
)


def _step_candidates(step: dict) -> list[str]:
    """Every string a step exposes to this lint: its inline `run:` script and each
    scalar value under `with:`."""
    candidates: list[str] = []
    run = step.get("run")
    if isinstance(run, str):
        candidates.append(run)
    with_inputs = step.get("with")
    if isinstance(with_inputs, dict):
        candidates += [v for v in with_inputs.values() if isinstance(v, str)]
    return candidates


def _step_block(lines: list[str], start_1based: int) -> str:
    """The source lines of the step beginning at START_1BASED: that line plus every
    following line indented deeper than it (blank lines included), stopping at the
    next line indented the same or shallower — i.e. the next list item / sibling
    key. Used to find an opt-out comment scoped to the offending step, including a
    `#` comment trailing a `with:` value that PyYAML would have discarded."""
    i = start_1based - 1
    if i < 0 or i >= len(lines):
        return ""
    base_indent = len(lines[i]) - len(lines[i].lstrip())
    block = [lines[i]]
    j = i + 1
    while j < len(lines):
        line = lines[j]
        if line.strip() and (len(line) - len(line.lstrip())) <= base_indent:
            break
        block.append(line)
        j += 1
    return "\n".join(block)


def _iter_steps(container: object) -> list[dict]:
    """The step dicts of a job/composite-action `steps:` list."""
    steps = container.get("steps") if isinstance(container, dict) else None
    if not isinstance(steps, list):
        return []
    return [s for s in steps if isinstance(s, dict)]


def _all_steps(doc: dict) -> list[dict]:
    """Every step across all jobs plus a composite action's `runs.steps`."""
    steps: list[dict] = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            steps += _iter_steps(job)
    steps += _iter_steps(doc.get("runs"))
    return steps


def find_violations(text: str) -> list[tuple[int | None, str]]:
    """(line, message) for every step whose `run:`/`with:` uses the frozen head
    SHA without an opt-out. An unparseable workflow is reported as a violation
    (line None) rather than passed as clean — a false-green on the file under test."""
    try:
        doc = yaml.load(text, Loader=_LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify frozen "
                "head-SHA usage — fix the syntax (or run actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []

    lines = text.splitlines()
    violations: list[tuple[int | None, str]] = []
    for step in _all_steps(doc):
        if not any(FROZEN.search(c) for c in _step_candidates(step)):
            continue
        line = step.get("__line__")
        block = _step_block(lines, line) if isinstance(line, int) else ""
        if _ALLOW_RE.search(block):
            continue
        violations.append((line, _MESSAGE))
    return violations


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """(line, message) for every frozen-head-SHA violation in PATH."""
    return find_violations(path.read_text())


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
        print(f"\nERROR: {total} frozen head-SHA usage(s) found.")
        print(
            "The frozen event SHA mis-scopes diff ranges after a force-push; "
            "derive the head from the checkout (git rev-parse HEAD)."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
