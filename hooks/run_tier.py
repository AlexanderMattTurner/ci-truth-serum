#!/usr/bin/env python3
"""
Run every check in a ci-truth-serum tier under a single hook id.

Consumers enable one aggregate — ``check-tier1`` / ``check-tier2`` /
``check-extras`` — instead of listing each lint, so a check added to that tier
later is picked up with no change to the consumer's ``.pre-commit-config.yaml``.

Each member runs exactly as its standalone hook would: the workflow lints
self-discover ``.github/{workflows,actions}`` (the passed file list is ignored),
and the content lints receive only the committed files of their kind (shell /
python / Dockerfile), classified with ``identify`` — the same library pre-commit
uses for its own ``types:`` filtering.

``check-symlinks`` is intentionally NOT aggregated: it is a ``language: script``
shell hook, not a Python module, so it cannot run inside this Python aggregate.
Enable it on its own if you want it. The contract test in
``tests/cts/test_run_tier.py`` asserts this registry stays in sync with
``.pre-commit-hooks.yaml`` so a newly added hook can't silently escape its tier.
"""

import subprocess
import sys

from identify import identify

# Selector kinds: WORKFLOW ignores the file list and self-discovers .github/*;
# the rest name the committed-file class a content lint should receive.
WORKFLOW = "workflow"
SHELL = "shell"
PYTHON = "python"
DOCKERFILE = "dockerfile"
SHELL_OR_DOCKERFILE = "shell_or_dockerfile"

TIERS: dict[str, list[tuple[str, str]]] = {
    "1": [
        ("check_workflow_pipefail", WORKFLOW),
        ("check_exit_suppression", SHELL),
        ("check_stderr_suppression", SHELL),
        ("check_pr_paths", WORKFLOW),
        ("check_pinned_base_images", DOCKERFILE),
        ("check_pinned_downloads", SHELL_OR_DOCKERFILE),
    ],
    "2": [
        ("check_always_reporter", WORKFLOW),
        ("check_required_reporter", WORKFLOW),
        ("check_inline_run_length", WORKFLOW),
        ("check_concurrency", WORKFLOW),
        ("check_static_concurrency", WORKFLOW),
    ],
    "extras": [
        ("check_unnamed_regex_groups", PYTHON),
        ("check_global_stdio_swap", PYTHON),
        ("check_claude_model", WORKFLOW),
    ],
}


def matches(path: str, kind: str) -> bool:
    """True if PATH is a file of the class a KIND-selector content lint wants."""
    tags = identify.tags_from_path(path)
    if kind == SHELL:
        return "shell" in tags
    if kind == PYTHON:
        return "python" in tags
    if kind == DOCKERFILE:
        return "dockerfile" in tags
    if kind == SHELL_OR_DOCKERFILE:
        return "shell" in tags or "dockerfile" in tags
    return False


def run_member(module: str, kind: str, files: list[str]) -> int:
    """Run one member check as its own subprocess; return its exit code.

    A content lint with no committed file of its kind has nothing to do, so it is
    skipped; a workflow lint always runs (it self-discovers, ignoring `files`).
    """
    if kind == WORKFLOW:
        argv = []
    else:
        argv = [f for f in files if matches(f, kind)]
        if not argv:
            return 0
    return subprocess.run(
        [sys.executable, "-m", f"hooks.{module}", *argv], check=False
    ).returncode


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in TIERS:
        print(f"usage: run_tier <{'|'.join(TIERS)}> [files...]", file=sys.stderr)
        return 2
    tier, files = argv[0], argv[1:]
    rc = 0
    for module, kind in TIERS[tier]:
        if run_member(module, kind, files):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
