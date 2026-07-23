#!/usr/bin/env python3
"""
Enforce an explicit model on every anthropics/claude-code-action step.

When a `claude-code-action` step omits `--model` (in `claude_args`) the action
falls back to its built-in default — currently Opus, the most expensive tier.
A workflow that never names a model therefore bills Opus silently: nothing in
the YAML says "Opus", so the cost is invisible until a billing audit finds it.

The fix is to always pin the model the job actually needs (e.g.
`claude_args: "--model claude-sonnet-4-6 …"`), so the choice is explicit and
reviewable and an expensive default can't slip in.

Opt out with a "# allow-default-model" comment on the `uses:` line when a step
is deliberately meant to ride the action's default model.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
    LineLoader,
    workflow_files as _workflow_files,
)

ACTION = "anthropics/claude-code-action"
OPT_OUT = "allow-default-model"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

# A source line whose YAML key is `uses:` referencing the action exactly. Anchored
# after optional block-sequence `- ` and leading indent, so a commented-out or
# example `# uses: …` line never matches (the `#` is not consumed). Splitting on
# `@` excludes the longer `claude-code-base-action`, whose model handling differs.
USES_LINE = re.compile(rf"^-?\s*uses:\s*{re.escape(ACTION)}(?:@|\s|$)")

MESSAGE = (
    f"{ACTION} step has no explicit model; the action defaults to Opus (~5x the "
    "Sonnet cost). Add '--model <id>' to claude_args (or a 'model:' input), or "
    f"'# {OPT_OUT}' on the uses: line to ride the default deliberately."
)


def uses_action(step: dict) -> bool:
    """True if a step invokes claude-code-action (ignoring any @ref suffix)."""
    return (
        isinstance(step, dict)
        and str(step.get("uses", "")).split("@", 1)[0].strip() == ACTION
    )


def has_model(step: dict) -> bool:
    """True if the step names a model — via `--model` in claude_args or a `model:` input."""
    with_ = step.get("with") or {}
    if not isinstance(with_, dict):
        return False
    return "--model" in str(with_.get("claude_args", "")) or "model" in with_


def action_steps(doc: dict) -> list[dict]:
    """Every step (workflow jobs.*.steps and composite-action runs.steps), in document order."""
    steps: list[dict] = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if isinstance(job, dict) and isinstance(job.get("steps"), list):
                steps += [s for s in job["steps"] if isinstance(s, dict)]
    runs = doc.get("runs")
    if isinstance(runs, dict) and isinstance(runs.get("steps"), list):
        steps += [s for s in runs["steps"] if isinstance(s, dict)]
    return steps


def _uses_line(source_lines: list[str], start: int) -> int:
    """The 1-based line of this step's `uses: <action>` key, scanning forward from
    the step's own start line. Anchoring to the step's start (not a global grep of
    every `uses:` in the file) is what fixes the misalignment: a commented/example
    `uses:` line elsewhere can no longer shift which step a violation is pinned to."""
    for i in range(start - 1, len(source_lines)):
        if USES_LINE.match(source_lines[i].lstrip()):
            return i + 1
    return start


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every claude-code-action step missing an explicit model.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text()
    try:
        doc = yaml.load(text, Loader=LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify "
                "claude-code-action model pinning — fix the syntax (or run "
                "actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []

    # LineLoader tags each step mapping with `__line__` (its first key's source
    # line); the step's own `uses:` line is found by scanning from there, so each
    # violation is anchored to its real step instead of a positional text pairing.
    source_lines = text.splitlines()
    violations: list[tuple[int | None, str]] = []
    for step in action_steps(doc):
        if not uses_action(step):
            continue
        line = _uses_line(source_lines, step.get("__line__", 1))
        opted_out = 1 <= line <= len(source_lines) and annotated(
            source_lines[line - 1], OPT_OUT, require_reason=False
        )
        if not has_model(step) and not opted_out:
            violations.append((line, MESSAGE))
    return violations


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
            "A claude-code-action step without an explicit --model silently runs "
            "on the action's default (Opus) tier."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
