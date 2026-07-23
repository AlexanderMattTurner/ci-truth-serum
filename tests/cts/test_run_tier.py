"""Tests for ci_truth_serum/run_tier.py — the per-tier aggregate runner that lets a
consumer enable a whole tier (check-tier1/2/extras) with one id.

Two layers:
  * a **contract** test pinning the in-code TIERS registry to the live
    `.pre-commit-hooks.yaml` (every Python check sits in exactly the tier its
    name prefix declares; only `check-symlinks`, a shell hook, is unaggregated),
    so a newly added hook can't silently escape its tier; and
  * functional tests of `matches`/`run_member`/`main` (file routing, the
    skip-when-no-files-of-kind path, exit-code aggregation, the usage guard),
    driven through a real subprocess against a tmp repo.
"""

from pathlib import Path

import yaml

from tests._helpers import REPO_ROOT, load_hook

rt = load_hook("run_tier.py", "run_tier")

MANIFEST = yaml.safe_load((REPO_ROOT / ".pre-commit-hooks.yaml").read_text())

# Map a name-prefix (the manifest encodes the tier in `name:`) to a TIERS key.
PREFIX_TIER = {
    "honesty": "1",
    "identity": "1",
    "security": "1",
    "opinionated": "2",
    "extra": "extras",
}
# Hooks intentionally left out of every aggregate, each enabled on its own:
# `check-symlinks` (a language:script shell hook, not a Python module),
# `check-lockstep-pins` (config-driven; hard-errors without per-repo `--pair`
# args), and `check-env-symmetry` (a whole-tree scan needing a per-project
# `--prefix` arg no aggregate can supply).
UNAGGREGATED = {"check-symlinks", "check-lockstep-pins", "check-env-symmetry"}


def _python_member_hooks() -> list[dict]:
    """Manifest hooks that are individual Python lints (not the aggregates, not the shell hook)."""
    return [
        h
        for h in MANIFEST
        if h["entry"].startswith("python -m ci_truth_serum.")
        and not h["entry"].startswith("python -m ci_truth_serum.run_tier")
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
    # check-symlinks must exist, be a script hook, and appear in no tier;
    # check-lockstep-pins and check-env-symmetry must exist, demand their args,
    # and appear in no tier.
    symlinks = next(h for h in MANIFEST if h["id"] == "check-symlinks")
    assert symlinks["entry"].endswith(".sh") and symlinks["language"] == "script"
    lockstep = next(h for h in MANIFEST if h["id"] == "check-lockstep-pins")
    assert lockstep["entry"] == "python -m ci_truth_serum.check_lockstep_pins"
    assert lockstep["pass_filenames"] is False and lockstep["always_run"] is True
    env_symmetry = next(h for h in MANIFEST if h["id"] == "check-env-symmetry")
    assert env_symmetry["entry"].startswith(
        "python -m ci_truth_serum.check_env_symmetry"
    )
    all_modules = {mod for members in rt.TIERS.values() for mod, _ in members}
    assert "check_symlinks" not in all_modules
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
    p.write_text("#!/usr/bin/env bash\necho hi\n")
    assert rt.matches(str(p), rt.SHELL) is True
    assert rt.matches(str(p), rt.PYTHON) is False


def test_matches_python(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("x = 1\n")
    assert rt.matches(str(p), rt.PYTHON) is True


def test_matches_dockerfile(tmp_path):
    p = tmp_path / "Dockerfile"
    p.write_text("FROM scratch\n")
    assert rt.matches(str(p), rt.DOCKERFILE) is True
    assert rt.matches(str(p), rt.SHELL_OR_DOCKERFILE) is True


def test_matches_shell_or_dockerfile_accepts_shell(tmp_path):
    p = tmp_path / "x.bash"
    p.write_text("#!/usr/bin/env bash\n")
    assert rt.matches(str(p), rt.SHELL_OR_DOCKERFILE) is True


def test_matches_shell_or_workflow_yaml(tmp_path):
    sh = tmp_path / "s.sh"
    sh.write_text("#!/usr/bin/env bash\n")
    assert rt.matches(str(sh), rt.SHELL_OR_WORKFLOW_YAML) is True
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name in ("ci.yaml", "ci.yml"):
        p = wf / name
        p.write_text("jobs: {}\n")
        assert rt.matches(str(p), rt.SHELL_OR_WORKFLOW_YAML) is True, name
    other_yaml = tmp_path / "data.yaml"
    other_yaml.write_text("x: 1\n")
    assert rt.matches(str(other_yaml), rt.SHELL_OR_WORKFLOW_YAML) is False


def test_matches_markdown(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# heading\n")
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
        p.write_text(body)
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
        p.write_text(body)
        assert rt.matches(str(p), rt.DRIFT) is True, name
    md = tmp_path / "notes.md"
    md.write_text("# heading\n")
    assert rt.matches(str(md), rt.DRIFT) is False


def test_matches_prose_or_commented_code_accepts_prose(tmp_path):
    for name in ("notes.md", "doc.rst"):
        p = tmp_path / name
        p.write_text("text\n")
        assert rt.matches(str(p), rt.PROSE_OR_COMMENTED_CODE) is True, name
    p = tmp_path / "Dockerfile"
    p.write_text("FROM scratch\n")
    assert rt.matches(str(p), rt.PROSE_OR_COMMENTED_CODE) is False


# ── run_member ────────────────────────────────────────────────────────────
def test_run_member_skips_content_lint_with_no_matching_files(tmp_path, monkeypatch):
    # A python file given to a SHELL member → no shell files → skipped (rc 0),
    # and no subprocess is spawned.
    called = False

    def _boom(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("subprocess should not run")

    monkeypatch.setattr(rt.subprocess, "run", _boom)
    p = tmp_path / "m.py"
    p.write_text("x = 1\n")
    assert rt.run_member("check_exit_suppression", rt.SHELL, [str(p)]) == 0
    assert called is False


def test_run_member_workflow_ignores_files_and_runs(monkeypatch):
    captured = {}

    class _Done:
        returncode = 0

    def _fake(cmd, check):
        captured["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(rt.subprocess, "run", _fake)
    assert rt.run_member("check_pr_paths", rt.WORKFLOW, ["ignored.py"]) == 0
    # WORKFLOW members get no file args appended — just `-m ci_truth_serum.<module>`.
    assert captured["cmd"][1:] == ["-m", "ci_truth_serum.check_pr_paths"]


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
    shell_file.write_text("#!/usr/bin/env bash\necho hi\n")

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


def test_skip_without_argument_exits_nonzero(capsys):
    rc = rt.main(["1", "--skip"])
    assert rc == 2
    assert "requires an argument" in capsys.readouterr().err


def _tmp_repo_with_pr_paths_violation(tmp_path: Path) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    # A paths: filter on pull_request — a Tier 1 (check-pr-paths) violation.
    (wf / "bad.yaml").write_text(
        "name: x\non:\n  pull_request:\n    paths: ['src/**']\njobs: {}\n"
    )
    return tmp_path


def test_main_tier1_flags_a_real_violation(tmp_path, monkeypatch):
    # Real subprocess wiring: run_tier shells out to the installed hooks package,
    # which self-discovers .github/workflows under cwd.
    repo = _tmp_repo_with_pr_paths_violation(tmp_path)
    monkeypatch.chdir(repo)
    assert rt.main(["1"]) == 1


def test_main_tier1_passes_on_clean_repo(tmp_path, monkeypatch):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    # Clean: a branches filter on PUSH is ignored (only pull_request is checked),
    # and the pull_request trigger itself carries no paths/branches filter.
    (tmp_path / ".github" / "workflows" / "ok.yaml").write_text(
        "name: x\non:\n  push:\n    branches: [main]\n  pull_request:\njobs: {}\n"
    )
    monkeypatch.chdir(tmp_path)
    assert rt.main(["1"]) == 0
