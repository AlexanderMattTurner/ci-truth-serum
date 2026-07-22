#!/usr/bin/env python3
"""
Enforce explicit cancel-in-progress on every GitHub Actions concurrency group.

A `concurrency:` block without `cancel-in-progress:` silently defaults to false —
queued runs pile up and new pushes don't cancel old ones. The default was safe
when GitHub only allowed one concurrent run, but with the `group:` key queuing
multiple runs per PR it is rarely the right choice and is never obvious from the
YAML.

This script rejects any workflow whose `concurrency:` block omits the key
entirely. Setting it explicitly — to `true`, `false`, or an expression like
`${{ github.event_name == 'pull_request' }}` — is always an acceptable fix.

Reusable workflows and composite actions without a `concurrency:` block are
exempt: they inherit cancellation from their caller. This lint is opinionated
(Tier 2): some teams deliberately want queue-don't-cancel, which is exactly why
making the choice explicit — rather than dictating a value — is the rule.
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _job_blocks,
    concurrency_line,
    job_concurrency_line,
    opted_out,
)

OPT_OUT = "cancel-in-progress-not-required"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

_MESSAGE = (
    "concurrency: block is missing cancel-in-progress — it silently defaults "
    "to false, queuing runs instead of cancelling on new pushes. Set it "
    "explicitly to true, false, or an expression such as "
    "'${{ github.event_name == \"pull_request\" }}'."
)


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every concurrency block — workflow-level OR
    job-level — that omits cancel-in-progress.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify "
                "cancel-in-progress on concurrency blocks — fix the syntax (or run "
                "actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict) or opted_out(text, OPT_OUT):
        return []

    violations: list[tuple[int | None, str]] = []
    conc = doc.get("concurrency")
    if isinstance(conc, dict) and "cancel-in-progress" not in conc:
        violations.append((concurrency_line(text), _MESSAGE))

    blocks = _job_blocks(text)
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for name, cfg in jobs.items():
            if not isinstance(cfg, dict):
                continue
            job_conc = cfg.get("concurrency")
            if isinstance(job_conc, dict) and "cancel-in-progress" not in job_conc:
                block = blocks.get(str(name))
                fallback = block[0] if block else concurrency_line(text)
                line = job_concurrency_line(block, fallback)
                violations.append((line, f"job '{name}': {_MESSAGE}"))
    return violations


def main() -> int:
    files = sorted(WORKFLOWS_DIR.glob("*.yaml")) + sorted(WORKFLOWS_DIR.glob("*.yml"))
    total = 0
    for path in files:
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
