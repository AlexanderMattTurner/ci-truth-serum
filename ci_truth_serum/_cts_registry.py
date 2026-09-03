"""The one place that says which checks this pack ships, and what each one is about.

Every aggregated check has three pieces of metadata here: the tier that says how
opinionated it is (``1`` honesty + identity, ``2`` opinionated, ``extras``
off-theme), the file class it reads, and its TAGS — the defect it is about, such
as ``security`` or ``concurrency``. A check carries as many tags as apply, so
``check_token_fallback`` answers to both ``secrets`` and ``security``.

Tiers and tags are different axes on purpose. The tier says how much of your CI
architecture the check assumes, so it decides whether the check can run at all.
The tag says what the check is FOR, so it decides whether you want it. A
consumer selects on either, or on both, through ``run_selection``.

The tag vocabulary is closed (``TAGS``). A new value needs an entry there and a
row in the README table, which the tests pin.
"""

from typing import NamedTuple

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
# check_relative_imports resolves a specifier the way Node's ESM loader does, so it
# reads JS/TS and nothing else — a Python file has no relative module specifier.
JS = "js"
# check_conclusion_coverage asks one question of three surfaces, because the
# defect is three copies of one answer drifting apart: a workflow expression, a
# shell test, and a Python comparison.
SHELL_PYTHON_OR_WORKFLOW_YAML = "shell_python_or_workflow_yaml"

# The closed tag vocabulary. Each value names the defect a check is about, never
# the file it reads — the file class is the `kind` field, and a `shell` tag would
# only restate it.
HONESTY = "honesty"
SUPPLY_CHAIN = "supply-chain"
SECURITY = "security"
SECRETS = "secrets"
REQUIRED_CHECKS = "required-checks"
CONCURRENCY = "concurrency"
SCHEDULING = "scheduling"
ALERTING = "alerting"
DOCS = "docs"
TESTS = "tests"
AGENTS = "agents"
CORRECTNESS = "correctness"
MAINTAINABILITY = "maintainability"
COST = "cost"

TAGS: frozenset[str] = frozenset(
    {
        HONESTY,
        SUPPLY_CHAIN,
        SECURITY,
        SECRETS,
        REQUIRED_CHECKS,
        CONCURRENCY,
        SCHEDULING,
        ALERTING,
        DOCS,
        TESTS,
        AGENTS,
        CORRECTNESS,
        MAINTAINABILITY,
        COST,
    }
)


class Check(NamedTuple):
    """One aggregated check: its module, its tier, the files it reads, its tags."""

    module: str
    tier: str
    kind: str
    tags: frozenset[str]

    @property
    def hook_id(self) -> str:
        """The `.pre-commit-hooks.yaml` id, which is the module name in kebab case."""
        return self.module.replace("_", "-")


def _check(module: str, tier: str, kind: str, *tags: str) -> Check:
    return Check(module, tier, kind, frozenset(tags))


CHECKS: tuple[Check, ...] = (
    # ── Tier 1 · honesty + identity (default-on) ──
    _check("check_workflow_pipefail", "1", WORKFLOW, HONESTY),
    _check("check_exit_suppression", "1", SHELL, HONESTY),
    _check("check_stderr_suppression", "1", SHELL, HONESTY),
    _check("check_substitution_exit_swallow", "1", SHELL, HONESTY),
    _check("check_argument_exit_swallow", "1", SHELL, HONESTY),
    _check("check_soft_timeout", "1", SHELL, HONESTY, COST),
    _check("check_flock_fixed_fd", "1", SHELL, HONESTY, CONCURRENCY),
    _check("check_pipefail_grep_pipe", "1", SHELL, HONESTY),
    _check("check_folded_scalar_comment", "1", WORKFLOW, HONESTY),
    _check("check_runner_var_foreign_shell", "1", WORKFLOW, HONESTY),
    _check("check_gh_slurp_jq", "1", SHELL_OR_WORKFLOW_YAML, HONESTY),
    _check("check_truncating_pr_json", "1", SHELL_PYTHON_OR_WORKFLOW_YAML, HONESTY),
    _check("check_pr_paths", "1", WORKFLOW, HONESTY, REQUIRED_CHECKS),
    _check("check_pinned_base_images", "1", DOCKERFILE, SUPPLY_CHAIN),
    _check("check_pinned_downloads", "1", SHELL_OR_DOCKERFILE, SUPPLY_CHAIN),
    _check("check_versionless_install", "1", SHELL_OR_WORKFLOW_YAML, SUPPLY_CHAIN),
    _check("check_frozen_head_sha", "1", WORKFLOW, HONESTY, SECURITY),
    _check("check_ready_for_review", "1", WORKFLOW, HONESTY, REQUIRED_CHECKS),
    _check("check_provenance_repo_url", "1", WORKFLOW, SUPPLY_CHAIN),
    _check("check_trusted_base", "1", WORKFLOW, SECURITY),
    _check("check_untrusted_exec", "1", WORKFLOW, SECURITY),
    _check("check_unscoped_tool_grant", "1", WORKFLOW, SECURITY, AGENTS),
    # ── Tier 2 · opinionated ──
    _check("check_job_timeout", "2", WORKFLOW, COST),
    _check("check_uncached_download", "2", WORKFLOW, COST),
    _check("check_always_reporter", "2", WORKFLOW, REQUIRED_CHECKS),
    _check("check_required_reporter", "2", WORKFLOW, REQUIRED_CHECKS),
    _check("check_required_event_closure", "2", WORKFLOW, REQUIRED_CHECKS),
    _check("check_inline_run_length", "2", WORKFLOW, MAINTAINABILITY),
    _check("check_concurrency", "2", WORKFLOW, CONCURRENCY),
    _check("check_static_concurrency", "2", WORKFLOW, CONCURRENCY, REQUIRED_CHECKS),
    _check("check_pending_cancel_concurrency", "2", WORKFLOW, CONCURRENCY),
    _check("check_collapsing_job_group", "2", WORKFLOW, CONCURRENCY, COST),
    _check("check_requires_concurrency", "2", WORKFLOW, CONCURRENCY, COST),
    _check("check_externalized_markers", "2", WORKFLOW, MAINTAINABILITY),
    _check("check_path_gate_deps", "2", WORKFLOW, REQUIRED_CHECKS),
    _check("check_reusable_permissions", "2", WORKFLOW, SECURITY),
    _check("check_failure_notifier_coverage", "2", WORKFLOW, ALERTING),
    _check("check_failure_only_diagnostics", "2", WORKFLOW, ALERTING, CORRECTNESS),
    _check(
        "check_cancellable_required_check", "2", WORKFLOW, CONCURRENCY, REQUIRED_CHECKS
    ),
    _check(
        "check_conclusion_coverage",
        "2",
        SHELL_PYTHON_OR_WORKFLOW_YAML,
        REQUIRED_CHECKS,
    ),
    _check("check_token_fallback", "2", WORKFLOW, SECRETS, SECURITY),
    _check("check_workflow_secret_names", "2", WORKFLOW, SECRETS),
    _check("check_pin_comment_truth", "2", WORKFLOW, SUPPLY_CHAIN),
    _check("check_divergent_action_pins", "2", WORKFLOW, SUPPLY_CHAIN),
    _check("check_stderr_merge_parse", "2", SHELL_OR_WORKFLOW_YAML, HONESTY),
    _check("check_echo_fallback", "2", SHELL, HONESTY),
    _check("check_bare_return_status", "2", SHELL, HONESTY, CORRECTNESS),
    _check("check_bare_mkdir", "2", SHELL, HONESTY, CORRECTNESS),
    _check("check_env_arith", "2", SHELL, CORRECTNESS),
    _check("check_curl_retry", "2", SHELL, CORRECTNESS, COST),
    _check("check_retry_loop", "2", SHELL, MAINTAINABILITY),
    _check("check_unbounded_waits", "2", SHELL, CORRECTNESS, COST),
    _check("check_shell_source_declarations", "2", SHELL, CORRECTNESS, MAINTAINABILITY),
    _check(
        "check_sparse_checkout_closure", "2", WORKFLOW, CORRECTNESS, REQUIRED_CHECKS
    ),
    # ── Extras · off-theme bonus ──
    _check("check_unnamed_regex_groups", "extras", PYTHON, MAINTAINABILITY),
    _check(
        "check_replacement_expansion", "extras", JS_OR_PYTHON, CORRECTNESS, SECURITY
    ),
    _check("check_unpaged_all", "extras", JS_OR_PYTHON, HONESTY, CORRECTNESS),
    _check("check_global_stdio_swap", "extras", PYTHON, MAINTAINABILITY),
    _check("check_claude_model", "extras", WORKFLOW, AGENTS, SUPPLY_CHAIN),
    _check("check_drift_guards", "extras", DRIFT, TESTS),
    _check("check_graceful_handwave", "extras", PROSE_OR_COMMENTED_CODE, DOCS),
    _check("check_historical_comments", "extras", COMMENTED_CODE, DOCS),
    _check("check_doc_line_refs", "extras", MARKDOWN, DOCS),
    _check("check_workflow_refs", "extras", REFERENCING_TEXT, DOCS),
    _check("check_flag_arity", "extras", SHELL, CORRECTNESS),
    _check("check_secret_file_perms", "extras", SHELL, SECRETS, SECURITY),
    _check("check_case_default", "extras", SHELL, CORRECTNESS),
    _check("check_cron_comment", "extras", WORKFLOW, SCHEDULING, DOCS),
    _check("check_cron_alert_coverage", "extras", WORKFLOW, SCHEDULING, ALERTING),
    _check("check_external_clock_targets", "extras", WORKFLOW, SCHEDULING),
    _check("check_multi_cron_gating", "extras", WORKFLOW, SCHEDULING),
    _check("check_unused_reusable_input", "extras", WORKFLOW, MAINTAINABILITY),
    _check(
        "check_workflow_run_branch_filter", "extras", WORKFLOW, SECURITY, CORRECTNESS
    ),
    _check("check_toolchain_skips", "extras", PYTHON, TESTS, HONESTY),
    _check("check_stray_tool_markup", "extras", PROSE_OR_COMMENTED_CODE, AGENTS, DOCS),
    _check("check_test_predicate_shadow", "extras", SHELL, TESTS),
    _check("check_dead_shell_functions", "extras", SHELL, MAINTAINABILITY),
    _check("check_cwd_scoped_git", "extras", PYTHON, CORRECTNESS),
    _check("check_unspecified_encoding", "extras", PYTHON, CORRECTNESS),
    _check(
        "check_duplicate_module_constant",
        "extras",
        PYTHON,
        CORRECTNESS,
        MAINTAINABILITY,
    ),
    _check("check_duplicate_class_names", "extras", PYTHON, MAINTAINABILITY),
    _check("check_big_tuple_annotations", "extras", PYTHON, MAINTAINABILITY),
    _check("check_unreset_module_state", "extras", PYTHON, TESTS, CORRECTNESS),
    _check("check_sleep_as_sync", "extras", PYTHON, TESTS),
    _check("check_positional_git_argv", "extras", PYTHON, TESTS),
    _check("check_test_helper_kwargs", "extras", PYTHON, TESTS),
    _check("check_wall_clock_assertions", "extras", JS_OR_PYTHON, TESTS),
    _check("check_relative_imports", "extras", JS, CORRECTNESS),
    _check("check_path_shadowed_interpreter", "extras", WORKFLOW, AGENTS, CORRECTNESS),
)

# The tier view the `check-tier1` / `check-tier2` / `check-extras` aggregates run.
TIERS: dict[str, list[tuple[str, str]]] = {
    tier: [(c.module, c.kind) for c in CHECKS if c.tier == tier]
    for tier in ("1", "2", "extras")
}


def by_tag(tag: str) -> list[Check]:
    """Every check carrying TAG, in registry order."""
    return [c for c in CHECKS if tag in c.tags]
