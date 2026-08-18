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

A content lint with no file of its kind cannot run, and the run says so on
stderr, naming each one. Pre-commit always passes the changed files, so this
reports on a commit that touches only one language. It also catches the hand
run: ``run_tier 1`` with no arguments still runs every workflow lint and exits
0, which without the note reads as a clean tier rather than a partial one.

Three hooks are intentionally NOT aggregated, each enabled on its own:
``check-absolute-symlinks`` is a ``language: script`` shell hook, not a Python
module, so it cannot run inside this Python aggregate; ``check-lockstep-pins`` is
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
# check_workflow_refs reads narration wherever it lives, and a workflow's own
# `#` comments are one of the densest sources of sibling-workflow citations —
# hence prose + commented code + YAML.
REFERENCING_TEXT = "referencing_text"
# check_drift_guards dispatches by extension: `.py` → AST marker pass, else → a
# phrase pass. Its file class is therefore Python plus the JS/TS/shell suites that
# carry copies-agree tests but no @pytest.mark.
DRIFT = "drift"
# check_replacement_expansion reads a call's argument list in two languages, so it
# takes the source files of both: JS/TS (including JSX/TSX) and Python.
JS_OR_PYTHON = "js_or_python"
# check_conclusion_coverage asks one question of three surfaces, because the
# defect is three copies of one answer drifting apart: a workflow expression, a
# shell test, and a Python comparison.
SHELL_PYTHON_OR_WORKFLOW_YAML = "shell_python_or_workflow_yaml"

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
        ("check_argument_exit_swallow", SHELL),
        ("check_soft_timeout", SHELL),
        ("check_pipefail_grep_pipe", SHELL),
        ("check_folded_scalar_comment", WORKFLOW),
        ("check_gh_slurp_jq", SHELL_OR_WORKFLOW_YAML),
        ("check_pr_paths", WORKFLOW),
        ("check_pinned_base_images", DOCKERFILE),
        ("check_pinned_downloads", SHELL_OR_DOCKERFILE),
        ("check_versionless_install", SHELL_OR_WORKFLOW_YAML),
        ("check_frozen_head_sha", WORKFLOW),
        ("check_ready_for_review", WORKFLOW),
        ("check_provenance_repo_url", WORKFLOW),
        ("check_trusted_base", WORKFLOW),
        ("check_untrusted_exec", WORKFLOW),
        ("check_unscoped_tool_grant", WORKFLOW),
    ],
    "2": [
        ("check_job_timeout", WORKFLOW),
        ("check_uncached_download", WORKFLOW),
        ("check_always_reporter", WORKFLOW),
        ("check_required_reporter", WORKFLOW),
        ("check_required_event_closure", WORKFLOW),
        ("check_inline_run_length", WORKFLOW),
        ("check_concurrency", WORKFLOW),
        ("check_static_concurrency", WORKFLOW),
        ("check_pending_cancel_concurrency", WORKFLOW),
        ("check_requires_concurrency", WORKFLOW),
        ("check_externalized_markers", WORKFLOW),
        ("check_path_gate_deps", WORKFLOW),
        ("check_reusable_permissions", WORKFLOW),
        ("check_failure_notifier_coverage", WORKFLOW),
        ("check_cancellable_required_check", WORKFLOW),
        ("check_conclusion_coverage", SHELL_PYTHON_OR_WORKFLOW_YAML),
        ("check_token_fallback", WORKFLOW),
        ("check_workflow_secret_names", WORKFLOW),
        ("check_pin_comment_truth", WORKFLOW),
        ("check_divergent_action_pins", WORKFLOW),
        ("check_stderr_merge_parse", SHELL_OR_WORKFLOW_YAML),
        ("check_echo_fallback", SHELL),
        ("check_bare_return_status", SHELL),
    ],
    "extras": [
        ("check_unnamed_regex_groups", PYTHON),
        ("check_replacement_expansion", JS_OR_PYTHON),
        ("check_unpaged_all", JS_OR_PYTHON),
        ("check_global_stdio_swap", PYTHON),
        ("check_claude_model", WORKFLOW),
        ("check_drift_guards", DRIFT),
        ("check_graceful_handwave", PROSE_OR_COMMENTED_CODE),
        ("check_historical_comments", COMMENTED_CODE),
        ("check_doc_line_refs", MARKDOWN),
        ("check_workflow_refs", REFERENCING_TEXT),
        ("check_flag_arity", SHELL),
        ("check_secret_file_perms", SHELL),
        ("check_case_default", SHELL),
        ("check_cron_comment", WORKFLOW),
        ("check_cron_alert_coverage", WORKFLOW),
        ("check_external_clock_targets", WORKFLOW),
        ("check_multi_cron_gating", WORKFLOW),
        ("check_unused_reusable_input", WORKFLOW),
        ("check_workflow_run_branch_filter", WORKFLOW),
        ("check_toolchain_skips", PYTHON),
        ("check_stray_tool_markup", PROSE_OR_COMMENTED_CODE),
        ("check_test_predicate_shadow", SHELL),
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
    if kind == REFERENCING_TEXT:
        return bool(tags & (_COMMENT_TAGS | _PROSE_TAGS | {"yaml"}))
    if kind == SHELL_PYTHON_OR_WORKFLOW_YAML:
        return bool(tags & {"shell", "python"}) or bool(
            "yaml" in tags and _WORKFLOW_YAML.search(path.replace("\\", "/"))
        )
    if kind == JS_OR_PYTHON:
        return bool(tags & {"python", "javascript", "jsx", "ts", "tsx"})
    if kind == DRIFT:
        return bool(tags & {"python", "javascript", "ts", "shell"})
    return False


def selected_files(kind: str, files: list[str]) -> list[str] | None:
    """The file arguments a KIND member receives, or None when it cannot run.

    A workflow lint self-discovers `.github/*` and ignores FILES, so it always
    runs and takes no arguments. A content lint reads only what it is given, so
    with no committed file of its kind it has nothing to scan. The caller must be
    able to tell that case from a pass — they are the same exit code, and a run
    that reports one as the other is the false green this pack exists to refuse.
    """
    if kind == WORKFLOW:
        return []
    return [f for f in files if matches(f, kind)] or None


def run_check(module: str, argv: list[str]) -> int:
    """Run one member check as its own subprocess; return its exit code."""
    return subprocess.run(
        [sys.executable, "-m", f"ci_truth_serum.{module}", *argv], check=False
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
    unscanned: list[str] = []
    for module, kind in TIERS[tier]:
        if module in skips:
            continue
        argv = selected_files(kind, files)
        if argv is None:
            unscanned.append(module)
            continue
        if run_check(module, argv):
            rc = 1

    # A member that never ran and a member that passed leave the same exit code.
    # This note is what separates them: a hand run with no file arguments runs
    # every workflow lint, reports 0, and reads as a clean tier without it.
    if unscanned:
        print(
            f"note: these tier {tier} checks did not run, because no file of "
            f"their kind was passed: {', '.join(unscanned)}",
            file=sys.stderr,
        )
        # Only an empty file list has this remedy. When the caller DID pass
        # files, the checks above sat out because the repository holds no file
        # of their kind, and re-running over the whole tree changes nothing.
        if not files:
            print(
                "  to scan the whole tree: git ls-files -z | xargs -0 python -m "
                f"ci_truth_serum.run_tier {tier}",
                file=sys.stderr,
            )
    return rc


if __name__ == "__main__":
    sys.exit(main())
