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

Three hooks are intentionally NOT aggregated, each enabled on its own:
``check-symlinks`` is a ``language: script`` shell hook, not a Python module, so
it cannot run inside this Python aggregate; ``check-lockstep-pins`` is
config-driven (it does nothing without per-repo ``--pair`` args, and the
aggregate passes none), so running it here would hard-error every consumer; and
``check-env-symmetry`` is a whole-tree scan needing a per-project ``--prefix``
arg no aggregate can supply. The contract test in
``tests/cts/test_run_tier.py`` asserts this registry stays in sync with
``.pre-commit-hooks.yaml`` so a newly added hook can't silently escape its tier.
"""

import re
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
SHELL_OR_WORKFLOW_YAML = "shell_or_workflow_yaml"
MARKDOWN = "markdown"
COMMENTED_CODE = "commented_code"
PROSE_OR_COMMENTED_CODE = "prose_or_commented_code"
# check_drift_guards dispatches by extension: `.py` → AST marker pass, else → a
# phrase pass. Its file class is therefore Python plus the JS/TS/shell suites that
# carry copies-agree tests but no @pytest.mark.
DRIFT = "drift"

# The file classes whose `#`/`//` comments the comment lints can read, and the
# prose classes scanned line-by-line.
_COMMENT_TAGS = frozenset({"shell", "python", "javascript", "ts"})
_PROSE_TAGS = frozenset({"markdown", "rst"})

# The workflow/composite-action files a SHELL_OR_WORKFLOW_YAML lint scans for
# inline `run:` blocks (matching the standalone hook's own path routing).
_WORKFLOW_YAML = re.compile(r"(?:^|/)\.github/(?:workflows|actions)/.*\.ya?ml$")

TIERS: dict[str, list[tuple[str, str]]] = {
    "1": [
        ("check_workflow_pipefail", WORKFLOW),
        ("check_exit_suppression", SHELL),
        ("check_stderr_suppression", SHELL),
        ("check_substitution_exit_swallow", SHELL),
        ("check_pipefail_grep_pipe", SHELL),
        ("check_pr_paths", WORKFLOW),
        ("check_pinned_base_images", DOCKERFILE),
        ("check_pinned_downloads", SHELL_OR_DOCKERFILE),
        ("check_frozen_head_sha", WORKFLOW),
        ("check_provenance_repo_url", WORKFLOW),
        ("check_trusted_base", WORKFLOW),
    ],
    "2": [
        ("check_job_timeout", WORKFLOW),
        ("check_always_reporter", WORKFLOW),
        ("check_required_reporter", WORKFLOW),
        ("check_inline_run_length", WORKFLOW),
        ("check_concurrency", WORKFLOW),
        ("check_static_concurrency", WORKFLOW),
        ("check_pending_cancel_concurrency", WORKFLOW),
        ("check_requires_concurrency", WORKFLOW),
        ("check_externalized_markers", WORKFLOW),
        ("check_path_gate_deps", WORKFLOW),
        ("check_failure_notifier_coverage", WORKFLOW),
        ("check_cancellable_required_check", WORKFLOW),
        ("check_token_fallback", WORKFLOW),
        ("check_workflow_secret_names", WORKFLOW),
        ("check_pin_comment_truth", WORKFLOW),
        ("check_stderr_merge_parse", SHELL_OR_WORKFLOW_YAML),
        ("check_echo_fallback", SHELL),
    ],
    "extras": [
        ("check_unnamed_regex_groups", PYTHON),
        ("check_global_stdio_swap", PYTHON),
        ("check_claude_model", WORKFLOW),
        ("check_drift_guards", DRIFT),
        ("check_graceful_handwave", PROSE_OR_COMMENTED_CODE),
        ("check_historical_comments", COMMENTED_CODE),
        ("check_doc_line_refs", MARKDOWN),
        ("check_flag_arity", SHELL),
        ("check_secret_file_perms", SHELL),
        ("check_case_default", SHELL),
        ("check_cron_comment", WORKFLOW),
        ("check_toolchain_skips", PYTHON),
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
    if kind == SHELL_OR_WORKFLOW_YAML:
        return "shell" in tags or bool(
            "yaml" in tags and _WORKFLOW_YAML.search(path.replace("\\", "/"))
        )
    if kind == MARKDOWN:
        return "markdown" in tags
    if kind == COMMENTED_CODE:
        return bool(tags & _COMMENT_TAGS)
    if kind == PROSE_OR_COMMENTED_CODE:
        return bool(tags & (_COMMENT_TAGS | _PROSE_TAGS))
    if kind == DRIFT:
        return bool(tags & {"python", "javascript", "ts", "shell"})
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
        print(
            f"usage: run_tier <{'|'.join(TIERS)}> [--skip <check>]... [files...]",
            file=sys.stderr,
        )
        return 2
    tier, rest = argv[0], argv[1:]

    skips: set[str] = set()
    files: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--skip":
            if i + 1 >= len(rest):
                print("error: --skip requires an argument", file=sys.stderr)
                return 2
            skips.add(rest[i + 1])
            i += 2
        else:
            files.append(rest[i])
            i += 1

    unknown = skips - {mod for mod, _ in TIERS[tier]}
    if unknown:
        print(
            f"error: unknown check(s) for tier {tier!r}: {', '.join(sorted(unknown))}",
            file=sys.stderr,
        )
        print(
            f"  valid: {', '.join(mod for mod, _ in TIERS[tier])}",
            file=sys.stderr,
        )
        return 2

    rc = 0
    for module, kind in TIERS[tier]:
        if module in skips:
            continue
        if run_member(module, kind, files):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
