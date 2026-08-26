"""Tests for ci_truth_serum/run_tier.py — the per-tier aggregate runner that lets a
consumer enable a whole tier (check-tier1/2/extras) with one id.

Two layers:
  * a **contract** test pinning the in-code TIERS registry to the live
    `.pre-commit-hooks.yaml` (every Python check sits in exactly the tier its
    name prefix declares; only `check-absolute-symlinks`, a shell hook, is
    unaggregated), so a newly added hook can't silently escape its tier; and
  * functional tests of `matches`/`selected_files`/`run_check`/`main` (file
    routing, the report naming the members that had no file to scan, exit-code
    aggregation, the usage guard), driven through a real subprocess against a
    tmp repo.
"""

from pathlib import Path

import yaml

from tests._helpers import REPO_ROOT, load_hook

rt = load_hook("run_tier.py", "run_tier")

MANIFEST = yaml.safe_load(
    (REPO_ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
)

# Map a name-prefix (the manifest encodes the tier in `name:`) to a TIERS key.
PREFIX_TIER = {
    "honesty": "1",
    "identity": "1",
    "security": "1",
    "opinionated": "2",
    "extra": "extras",
}
# Hooks intentionally left out of every aggregate, each enabled on its own:
# `check-absolute-symlinks` (a language:script shell hook, not a Python module),
# `check-lockstep-pins` (config-driven; hard-errors without per-repo `--pair`
# args), and `check-env-symmetry` (a whole-tree scan needing a per-project
# `--prefix` arg no aggregate can supply).
UNAGGREGATED = {
    "check-absolute-symlinks",
    "check-lockstep-pins",
    "check-env-symmetry",
}


def _python_member_hooks() -> list[dict]:
    """Manifest hooks that are individual Python lints (not the aggregates, not the shell hook)."""
    return [
        h
        for h in MANIFEST
        if h["entry"].startswith("python -m ci_truth_serum.")
        and not h["entry"].startswith("python -m ci_truth_serum.run_tier")
        and not h["entry"].startswith("python -m ci_truth_serum.run_selection")
    ]


# ── contract: registry ⇄ manifest ────────────────────────────────────────
def test_registry_covers_every_python_hook_in_its_declared_tier():
    registry = {(tier, mod) for tier, members in rt.TIERS.items() for mod, _ in members}
    expected = set()
    for hook in _python_member_hooks():
        if hook["id"] in UNAGGREGATED:
            continue
        prefix = hook["name"].split(":", 1)[0]
        module = hook["entry"].split()[-1].removeprefix("ci_truth_serum.")
        expected.add((PREFIX_TIER[prefix], module))
    assert registry == expected


def test_unaggregated_hooks_exist_and_appear_in_no_tier():
    # check-absolute-symlinks must exist, be a script hook, and appear in no tier;
    # check-lockstep-pins and check-env-symmetry must exist, demand their args,
    # and appear in no tier.
    symlinks = next(h for h in MANIFEST if h["id"] == "check-absolute-symlinks")
    assert symlinks["entry"].endswith(".sh") and symlinks["language"] == "script"
    lockstep = next(h for h in MANIFEST if h["id"] == "check-lockstep-pins")
    assert lockstep["entry"] == "python -m ci_truth_serum.check_lockstep_pins"
    assert lockstep["pass_filenames"] is False and lockstep["always_run"] is True
    env_symmetry = next(h for h in MANIFEST if h["id"] == "check-env-symmetry")
    assert env_symmetry["entry"].startswith(
        "python -m ci_truth_serum.check_env_symmetry"
    )
    all_modules = {mod for members in rt.TIERS.values() for mod, _ in members}
    assert "check_absolute_symlinks" not in all_modules
    assert "check_lockstep_pins" not in all_modules
    assert "check_env_symmetry" not in all_modules


def test_every_aggregate_id_has_a_tier():
    aggregate_tiers = {
        h["entry"].split()[-1]
        for h in MANIFEST
        if h["entry"].startswith("python -m ci_truth_serum.run_tier")
    }
    assert aggregate_tiers == set(rt.TIERS)


# ── matches ───────────────────────────────────────────────────────────────
def test_matches_shell(tmp_path):
    p = tmp_path / "s.sh"
    p.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    assert rt.matches(str(p), rt.SHELL) is True
    assert rt.matches(str(p), rt.PYTHON) is False


def test_matches_python(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("x = 1\n", encoding="utf-8")
    assert rt.matches(str(p), rt.PYTHON) is True


def test_matches_dockerfile(tmp_path):
    p = tmp_path / "Dockerfile"
    p.write_text("FROM scratch\n", encoding="utf-8")
    assert rt.matches(str(p), rt.DOCKERFILE) is True
    assert rt.matches(str(p), rt.SHELL_OR_DOCKERFILE) is True


def test_matches_shell_or_dockerfile_accepts_shell(tmp_path):
    p = tmp_path / "x.bash"
    p.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    assert rt.matches(str(p), rt.SHELL_OR_DOCKERFILE) is True


def test_matches_shell_or_workflow_yaml(tmp_path):
    sh = tmp_path / "s.sh"
    sh.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    assert rt.matches(str(sh), rt.SHELL_OR_WORKFLOW_YAML) is True
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name in ("ci.yaml", "ci.yml"):
        p = wf / name
        p.write_text("jobs: {}\n", encoding="utf-8")
        assert rt.matches(str(p), rt.SHELL_OR_WORKFLOW_YAML) is True, name
    other_yaml = tmp_path / "data.yaml"
    other_yaml.write_text("x: 1\n", encoding="utf-8")
    assert rt.matches(str(other_yaml), rt.SHELL_OR_WORKFLOW_YAML) is False


def test_matches_markdown(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# heading\n", encoding="utf-8")
    assert rt.matches(str(p), rt.MARKDOWN) is True
    assert rt.matches(str(p), rt.COMMENTED_CODE) is False


def test_matches_commented_code_accepts_each_language(tmp_path):
    for name, body in [
        ("s.sh", "#!/usr/bin/env bash\n"),
        ("m.py", "x = 1\n"),
        ("j.js", "let x = 1;\n"),
        ("t.ts", "let x = 1;\n"),
    ]:
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        assert rt.matches(str(p), rt.COMMENTED_CODE) is True, name
        assert rt.matches(str(p), rt.PROSE_OR_COMMENTED_CODE) is True, name


def test_matches_drift_accepts_python_js_ts_shell_only(tmp_path):
    for name, body in [
        ("m.py", "x = 1\n"),
        ("c.test.mjs", "test('x', () => {});\n"),
        ("j.js", "let x = 1;\n"),
        ("t.ts", "let x = 1;\n"),
        ("s.sh", "#!/usr/bin/env bash\n"),
    ]:
        p = tmp_path / name
        p.write_text(body, encoding="utf-8")
        assert rt.matches(str(p), rt.DRIFT) is True, name
    md = tmp_path / "notes.md"
    md.write_text("# heading\n", encoding="utf-8")
    assert rt.matches(str(md), rt.DRIFT) is False


def test_matches_prose_or_commented_code_accepts_prose(tmp_path):
    for name in ("notes.md", "doc.rst"):
        p = tmp_path / name
        p.write_text("text\n", encoding="utf-8")
        assert rt.matches(str(p), rt.PROSE_OR_COMMENTED_CODE) is True, name
    p = tmp_path / "Dockerfile"
    p.write_text("FROM scratch\n", encoding="utf-8")
    assert rt.matches(str(p), rt.PROSE_OR_COMMENTED_CODE) is False


# ── selected_files / run_check ────────────────────────────────────────────
def test_a_content_lint_with_no_file_of_its_kind_cannot_run(tmp_path):
    """None, not an empty list: the caller must tell 'nothing to scan' from a
    pass, because both would otherwise be exit 0."""
    p = tmp_path / "m.py"
    p.write_text("x = 1\n", encoding="utf-8")
    assert rt.selected_files(rt.SHELL, [str(p)]) is None


def test_a_content_lint_receives_only_the_files_of_its_kind(tmp_path):
    py = tmp_path / "m.py"
    py.write_text("x = 1\n", encoding="utf-8")
    sh = tmp_path / "s.sh"
    sh.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    assert rt.selected_files(rt.SHELL, [str(py), str(sh)]) == [str(sh)]


def test_a_workflow_lint_ignores_the_files_and_still_runs():
    """It self-discovers `.github/*`, so it takes no arguments and is never the
    skipped case, whatever the commit touched."""
    assert rt.selected_files(rt.WORKFLOW, ["ignored.py"]) == []
    assert rt.selected_files(rt.WORKFLOW, []) == []


def test_run_check_spawns_the_module_with_its_files(monkeypatch):
    captured = {}

    class _Done:
        returncode = 0

    def _fake(cmd, check):
        captured["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(rt.subprocess, "run", _fake)
    assert rt.run_check("check_pr_paths", []) == 0
    assert captured["cmd"][1:] == ["-m", "ci_truth_serum.check_pr_paths"]


def test_run_check_reports_the_module_exit_code(monkeypatch):
    class _Done:
        returncode = 1

    monkeypatch.setattr(rt.subprocess, "run", lambda cmd, check: _Done())
    assert rt.run_check("check_pr_paths", []) == 1


# ── main ──────────────────────────────────────────────────────────────────
def test_main_rejects_unknown_tier(capsys):
    assert rt.main(["nope"]) == 2
    assert "usage: run_tier" in capsys.readouterr().err


def test_main_rejects_missing_tier(capsys):
    assert rt.main([]) == 2


# ── --skip ────────────────────────────────────────────────────────────────
def test_skip_removes_named_member(tmp_path, monkeypatch):
    # A shell file triggers both SHELL members in tier 1 (check_exit_suppression
    # and check_stderr_suppression). Skipping one should leave the other called.
    shell_file = tmp_path / "s.sh"
    shell_file.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")

    called: list[str] = []

    class _Done:
        returncode = 0

    def _fake(cmd, check):
        # cmd = [sys.executable, "-m", "ci_truth_serum.<module>", ...]
        called.append(cmd[2].removeprefix("ci_truth_serum."))
        return _Done()

    monkeypatch.setattr(rt.subprocess, "run", _fake)
    rc = rt.main(["1", "--skip", "check_exit_suppression", str(shell_file)])
    assert rc == 0
    assert "check_exit_suppression" not in called
    # check_stderr_suppression is a SHELL peer that was NOT skipped
    assert "check_stderr_suppression" in called


def test_skip_unknown_name_exits_nonzero(capsys):
    rc = rt.main(["1", "--skip", "check_does_not_exist"])
    assert rc == 2
    assert "unknown" in capsys.readouterr().err


def test_a_misspelled_flag_is_not_a_filename(capsys):
    """`--skp <name>` read as two paths would run the tier with the check the
    caller meant to drop still in it."""
    rc = rt.main(["1", "--skp", "check_exit_suppression"])
    assert rc == 2
    assert "unknown option" in capsys.readouterr().err


def test_skip_without_argument_exits_nonzero(capsys):
    rc = rt.main(["1", "--skip"])
    assert rc == 2
    assert "requires an argument" in capsys.readouterr().err


# ── --check-arg ───────────────────────────────────────────────────────────
def _record_argv(monkeypatch) -> dict[str, list[str]]:
    """Capture the argv each member subprocess would have received."""
    seen: dict[str, list[str]] = {}

    class _Done:
        returncode = 0

    def _fake(cmd, check):
        seen[cmd[2].removeprefix("ci_truth_serum.")] = cmd[3:]
        return _Done()

    monkeypatch.setattr(rt.subprocess, "run", _fake)
    return seen


def test_check_arg_reaches_only_its_member_and_precedes_the_files(
    tmp_path, monkeypatch
):
    shell_file = tmp_path / "s.sh"
    shell_file.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    seen = _record_argv(monkeypatch)

    rc = rt.main(
        [
            "2",
            "--check-arg",
            "check_retry_loop=--wrapper=retry_cmd",
            "--check-arg",
            "check_retry_loop=--retry-helper=bin/lib/retry.bash",
            str(shell_file),
        ]
    )

    assert rc == 0
    assert seen["check_retry_loop"] == [
        "--wrapper=retry_cmd",
        "--retry-helper=bin/lib/retry.bash",
        str(shell_file),
    ]
    # A SHELL peer in the same tier gets the files and none of the flags.
    assert seen["check_curl_retry"] == [str(shell_file)]


def test_check_arg_reaches_a_workflow_member_that_takes_no_files(monkeypatch):
    """A WORKFLOW member self-discovers `.github/*` and is passed no files, so
    its flags are the whole argv."""
    seen = _record_argv(monkeypatch)

    rc = rt.main(
        ["2", "--check-arg", "check_failure_notifier_coverage=--require-notifier"]
    )

    assert rc == 0
    assert seen["check_failure_notifier_coverage"] == ["--require-notifier"]


def test_check_arg_for_a_check_outside_the_tier_exits_nonzero(capsys):
    rc = rt.main(["1", "--check-arg", "check_does_not_exist=--flag"])
    assert rc == 2
    assert "unknown" in capsys.readouterr().err


def test_check_arg_without_an_equals_exits_nonzero(capsys):
    """`--check-arg check_retry_loop --wrapper=x` would otherwise read the
    check name as the flag and the flag as a filename."""
    rc = rt.main(["2", "--check-arg", "check_retry_loop"])
    assert rc == 2
    assert "<check>=<flag>" in capsys.readouterr().err


def test_check_arg_with_an_empty_half_exits_nonzero(capsys):
    rc = rt.main(["2", "--check-arg", "check_retry_loop="])
    assert rc == 2
    assert "<check>=<flag>" in capsys.readouterr().err


def test_check_arg_without_argument_exits_nonzero(capsys):
    rc = rt.main(["2", "--check-arg"])
    assert rc == 2
    assert "requires an argument" in capsys.readouterr().err


def test_check_arg_on_a_skipped_check_exits_nonzero(capsys):
    """Silently dropping the flags would leave the caller believing they
    configured a check that never ran."""
    rc = rt.main(
        [
            "2",
            "--skip",
            "check_retry_loop",
            "--check-arg",
            "check_retry_loop=--wrapper=retry_cmd",
        ]
    )
    assert rc == 2
    assert "--skip removes" in capsys.readouterr().err


def test_a_flag_value_may_itself_contain_an_equals(tmp_path, monkeypatch):
    """The split is on the FIRST `=`, so `--wrapper=a=b` survives intact."""
    shell_file = tmp_path / "s.sh"
    shell_file.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    seen = _record_argv(monkeypatch)

    assert (
        rt.main(["2", "--check-arg", "check_retry_loop=--wrapper=a=b", str(shell_file)])
        == 0
    )
    assert seen["check_retry_loop"][0] == "--wrapper=a=b"


def _tmp_repo_with_pr_paths_violation(tmp_path: Path) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    # A paths: filter on pull_request — a Tier 1 (check-pr-paths) violation.
    (wf / "bad.yaml").write_text(
        "name: x\non:\n  pull_request:\n    paths: ['src/**']\njobs: {}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_main_tier1_flags_a_real_violation(tmp_path, monkeypatch):
    # Real subprocess wiring: run_tier shells out to the installed hooks package,
    # which self-discovers .github/workflows under cwd.
    repo = _tmp_repo_with_pr_paths_violation(tmp_path)
    monkeypatch.chdir(repo)
    assert rt.main(["1"]) == 1


def test_a_hand_run_with_no_files_says_which_checks_did_not_run(
    tmp_path, monkeypatch, capsys
):
    """The failure this note exists to prevent. `run_tier 1` with no arguments
    runs every workflow lint and exits 0, so the run reads as a clean tier while
    every content lint sat out. The note is the only thing that says otherwise."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ok.yaml").write_text(
        "name: x\non:\n  push:\n    branches: [main]\n  pull_request:\njobs: {}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert rt.main(["1"]) == 0
    err = capsys.readouterr().err
    assert "did not run" in err
    # Names each one, so the reader can see the shell lints are the gap.
    assert "check_exit_suppression" in err
    assert "check_pinned_base_images" in err
    # A workflow lint DID run, so it must not appear in the unscanned list.
    assert "check_pr_paths" not in err
    # The command that scans the whole tree, which is the remedy.
    assert "git ls-files -z | xargs -0" in err


def test_the_whole_tree_remedy_is_absent_when_files_were_passed(
    tmp_path, monkeypatch, capsys
):
    """A repository with no Dockerfile leaves the Dockerfile lint unscanned even
    on a whole-tree run. Naming it is right; telling the caller to scan the whole
    tree is not, because that is what they just did."""
    monkeypatch.setattr(
        rt,
        "TIERS",
        {"1": [("check_pinned_base_images", rt.DOCKERFILE)]},
    )
    py = tmp_path / "m.py"
    py.write_text("x = 1\n", encoding="utf-8")
    assert rt.main(["1", str(py)]) == 0
    err = capsys.readouterr().err
    assert "check_pinned_base_images" in err
    assert "git ls-files" not in err


def test_a_run_that_scans_every_member_prints_no_note(monkeypatch, capsys):
    """Non-vacuity for the note: it must be absent when nothing sat out, or it
    would be noise on every run and get ignored."""
    monkeypatch.setattr(rt, "TIERS", {"1": [("check_pr_paths", rt.WORKFLOW)]})

    class _Done:
        returncode = 0

    monkeypatch.setattr(rt.subprocess, "run", lambda cmd, check: _Done())
    assert rt.main(["1"]) == 0
    assert "did not run" not in capsys.readouterr().err


def test_a_skipped_member_is_not_reported_as_unscanned(monkeypatch, capsys):
    """`--skip` is the caller's own choice, already visible in their command, so
    listing it as unscanned would bury the members they did not choose."""
    monkeypatch.setattr(
        rt,
        "TIERS",
        {"1": [("check_pr_paths", rt.WORKFLOW), ("check_exit_suppression", rt.SHELL)]},
    )

    class _Done:
        returncode = 0

    monkeypatch.setattr(rt.subprocess, "run", lambda cmd, check: _Done())
    assert rt.main(["1", "--skip", "check_exit_suppression"]) == 0
    assert "did not run" not in capsys.readouterr().err


def test_main_tier1_passes_on_clean_repo(tmp_path, monkeypatch):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    # Clean: a branches filter on PUSH is ignored (only pull_request is checked),
    # and the pull_request trigger itself carries no paths/branches filter.
    (tmp_path / ".github" / "workflows" / "ok.yaml").write_text(
        "name: x\non:\n  push:\n    branches: [main]\n  pull_request:\njobs: {}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert rt.main(["1"]) == 0
