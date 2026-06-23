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
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

ACTION = "anthropics/claude-code-action"
OPT_OUT = "allow-default-model"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

# A `uses:` line that references the action exactly (split on `@` excludes the
# longer `claude-code-base-action`, whose model handling is separate).
USES_LINE = re.compile(rf"uses:\s*{re.escape(ACTION)}(?:@|\s|$)")

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


def uses_lines(text: str) -> list[tuple[int, bool]]:
    """1-based line number and opt-out flag for each claude-code-action `uses:` line, in order."""
    return [
        (num, OPT_OUT in line)
        for num, line in enumerate(text.splitlines(), 1)
        if USES_LINE.search(line)
    ]


def check_file(path: Path) -> list[tuple[int, str]]:
    """Return (line, message) for every claude-code-action step missing an explicit model."""
    text = path.read_text()
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        return []
    # Parse order and text order both follow the document, so the Nth using-step
    # lines up with the Nth uses: line — pair them to attach a line number.
    using = [step for step in action_steps(doc) if uses_action(step)]
    located = uses_lines(text)
    violations = []
    for step, (line, opted_out) in zip(using, located):
        if not has_model(step) and not opted_out:
            violations.append((line, MESSAGE))
    return violations


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
            "A claude-code-action step without an explicit --model silently runs "
            "on the action's default (Opus) tier."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
