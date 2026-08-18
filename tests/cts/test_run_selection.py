"""Tests for ci_truth_serum/run_selection.py — the aggregate that runs the checks a
`--select` expression names, by tier, by tag, or one at a time.

Two layers: unit tests of the selector algebra (`resolve` / `resolve_all` /
`parse_args`), and functional tests of `main` driven against a tmp repo, which
pin the three refusals — no `--select`, an unknown selector, an empty selection —
and the note that names the members no file reached.
"""

from pathlib import Path

from tests._helpers import load_hook

rs = load_hook("run_selection.py", "run_selection")
reg = load_hook("_registry.py", "_registry_for_selection")


# ── resolve ───────────────────────────────────────────────────────────────
def test_all_resolves_to_every_check():
    assert [c.module for c in rs.resolve("all")] == [c.module for c in reg.CHECKS]


def test_tier_resolves_to_that_tier():
    modules = {c.module for c in rs.resolve("tier:1")}
    assert modules == {m for m, _ in reg.TIERS["1"]}


def test_tag_resolves_to_the_carriers():
    modules = {c.module for c in rs.resolve("tag:concurrency")}
    assert "check_static_concurrency" in modules
    assert "check_doc_line_refs" not in modules


def test_check_resolves_by_module_name():
    assert [c.module for c in rs.resolve("check:check_job_timeout")] == [
        "check_job_timeout"
    ]


def test_check_resolves_by_hook_id_too():
    """A consumer reads hook ids out of the manifest, so the kebab spelling must
    work; the module spelling is what `--skip` already takes."""
    assert [c.module for c in rs.resolve("check:check-job-timeout")] == [
        "check_job_timeout"
    ]


def test_an_unknown_tag_raises():
    try:
        rs.resolve("tag:nope")
    except rs.SelectorError as exc:
        assert "unknown tag" in str(exc)
    else:
        raise AssertionError("a typo must stop the run, not select nothing")


def test_an_unknown_tier_raises():
    try:
        rs.resolve("tier:9")
    except rs.SelectorError as exc:
        assert "unknown tier" in str(exc)
    else:
        raise AssertionError("expected SelectorError")


def test_an_unknown_check_raises():
    try:
        rs.resolve("check:check_not_shipped")
    except rs.SelectorError as exc:
        assert "unknown check" in str(exc)
    else:
        raise AssertionError("expected SelectorError")


def test_a_selector_with_no_prefix_raises():
    try:
        rs.resolve("security")
    except rs.SelectorError as exc:
        assert "unknown selector" in str(exc)
    else:
        raise AssertionError("expected SelectorError")


def test_an_unknown_prefix_raises():
    try:
        rs.resolve("group:security")
    except rs.SelectorError as exc:
        assert "unknown selector" in str(exc)
    else:
        raise AssertionError("expected SelectorError")


# ── resolve_all ───────────────────────────────────────────────────────────
def test_selects_union():
    modules = {c.module for c in rs.resolve_all(["tag:alerting", "tag:tests"], [])}
    assert "check_cron_alert_coverage" in modules
    assert "check_drift_guards" in modules


def test_ignore_subtracts():
    modules = {
        c.module for c in rs.resolve_all(["tier:1"], ["check:check-pinned-base-images"])
    }
    assert "check_pinned_base_images" not in modules
    assert "check_pr_paths" in modules


def test_a_check_selected_twice_runs_once():
    chosen = rs.resolve_all(["tag:security", "tag:secrets"], [])
    modules = [c.module for c in chosen]
    assert len(modules) == len(set(modules))
    assert "check_token_fallback" in modules


def test_the_result_keeps_registry_order():
    chosen = [c.module for c in rs.resolve_all(["tag:honesty"], [])]
    order = [c.module for c in reg.CHECKS if c.module in set(chosen)]
    assert chosen == order


# ── parse_args ────────────────────────────────────────────────────────────
def test_parse_args_splits_flags_from_files():
    assert rs.parse_args(["--select", "tier:1", "--ignore", "tag:docs", "a.sh"]) == (
        ["tier:1"],
        ["tag:docs"],
        ["a.sh"],
    )


def test_a_flag_without_a_value_raises():
    try:
        rs.parse_args(["--select"])
    except rs.SelectorError as exc:
        assert "requires an argument" in str(exc)
    else:
        raise AssertionError("expected SelectorError")


def test_a_misspelled_flag_is_not_a_filename():
    """`--selec tag:tests` must stop the run. Read as two paths it would drop
    that selection and run the rest, which is a narrower green than the caller
    asked for."""
    try:
        rs.parse_args(["--select", "tag:alerting", "--selec", "tag:tests"])
    except rs.SelectorError as exc:
        assert "unknown option" in str(exc)
    else:
        raise AssertionError("expected SelectorError")


def test_main_rejects_a_misspelled_flag(capsys):
    assert rs.main(["--select", "tag:alerting", "--selec", "tag:tests"]) == 2
    assert "unknown option" in capsys.readouterr().err


# ── main ──────────────────────────────────────────────────────────────────
def test_main_without_select_exits_two(capsys):
    assert rs.main([]) == 2
    assert "usage: run_selection" in capsys.readouterr().err


def test_main_with_an_unknown_selector_exits_two(capsys):
    assert rs.main(["--select", "tag:nope"]) == 2
    assert "unknown tag" in capsys.readouterr().err


def test_an_empty_selection_exits_two(capsys):
    """The false green this refusal prevents: every selected check ignored away,
    zero checks run, exit 0."""
    assert rs.main(["--select", "tier:1", "--ignore", "tier:1"]) == 2
    assert "matched no checks" in capsys.readouterr().err


def _repo_with_pr_paths_violation(tmp_path: Path) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    # A paths: filter on pull_request — a check-pr-paths violation, tagged honesty.
    (wf / "bad.yaml").write_text(
        "name: x\non:\n  pull_request:\n    paths: ['src/**']\njobs: {}\n"
    )
    return tmp_path


def test_a_tag_selection_flags_a_real_violation(tmp_path, monkeypatch):
    monkeypatch.chdir(_repo_with_pr_paths_violation(tmp_path))
    assert rs.main(["--select", "tag:honesty"]) == 1


def test_ignoring_the_only_check_that_would_fire_passes(tmp_path, monkeypatch):
    """Non-vacuity for the test above: the same tree passes once the check that
    found the violation is ignored, so the exit code came from that check."""
    monkeypatch.chdir(_repo_with_pr_paths_violation(tmp_path))
    assert rs.main(["--select", "tag:honesty", "--ignore", "check:check-pr-paths"]) == 0


def test_the_note_names_the_members_no_file_reached(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(_repo_with_pr_paths_violation(tmp_path))
    rs.main(["--select", "tag:honesty", "--ignore", "check:check-pr-paths"])
    err = capsys.readouterr().err
    assert "did not run" in err
    # A shell lint with no shell file passed is the case; a workflow lint ran.
    assert "check_exit_suppression" in err
    assert "check_folded_scalar_comment" not in err
    # The remedy repeats the caller's own selection, so it is copy-pasteable.
    assert "--select tag:honesty --ignore check:check-pr-paths" in err
