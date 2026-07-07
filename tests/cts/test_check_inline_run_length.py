"""Tests for hooks/check_inline_run_length.py — the pre-commit lint that forces
substantial inline `run:` shell out to .github/scripts/*.sh (where
shellcheck/shfmt/shellharden can lint it) by failing any block over the line
limit that does not opt out."""

from pathlib import Path

from tests._helpers import REPO_ROOT, load_hook

cl = load_hook("check_inline_run_length.py", "check_inline_run_length")


# ── _significant_lines ────────────────────────────────────────────────────────


def test_significant_lines_ignores_blanks_and_pure_comments():
    script = "echo a\n\n  \n# a comment\n   # indented comment\necho b\n"
    assert cl._significant_lines(script) == 2


def test_significant_lines_counts_trailing_comment_lines():
    """A comment after code is still a code line."""
    assert cl._significant_lines("echo a  # note\n") == 1


def test_significant_lines_empty_is_zero():
    assert cl._significant_lines("") == 0


# ── _check_script ─────────────────────────────────────────────────────────────


def _body(n: int) -> str:
    """A run body of n significant lines."""
    return "\n".join(f"echo line{i}" for i in range(n))


def test_block_at_the_limit_is_clean():
    assert cl._check_script(_body(cl.MAX_LINES), "job x (run step 0)") == []


def test_block_one_over_the_limit_is_flagged():
    msgs = cl._check_script(_body(cl.MAX_LINES + 1), "job x (run step 0)")
    assert len(msgs) == 1
    assert f"{cl.MAX_LINES + 1} significant lines" in msgs[0]
    assert ".github/scripts/" in msgs[0]


def test_oversized_block_with_opt_out_is_clean():
    body = _body(cl.MAX_LINES + 5) + f"\n# {cl.ALLOW}: one-off, cannot move"
    assert cl._check_script(body, "job x (run step 0)") == []


def test_non_string_run_is_ignored():
    assert cl._check_script(None, "job x (run step 0)") == []


# ── analyze ───────────────────────────────────────────────────────────────────


def test_analyze_flags_job_and_composite_steps():
    over = _body(cl.MAX_LINES + 2)
    doc = {
        "jobs": {"build": {"steps": [{"run": over}, {"run": "echo ok"}]}},
        "runs": {"steps": [{"run": over}]},
    }
    msgs = [m for _line, m in cl.analyze(doc)]
    assert len(msgs) == 2
    assert any("job build" in m for m in msgs)
    assert any("composite action" in m for m in msgs)


def test_analyze_tolerates_non_mapping_jobs_and_uses_actions():
    """A step that just invokes an external script is always clean."""
    doc = {"runs": {"steps": [{"run": "bash .github/scripts/x.sh"}]}}
    assert cl.analyze(doc) == []


def test_analyze_non_dict_is_clean():
    assert cl.analyze(["not", "a", "doc"]) == []


def test_analyze_skips_non_dict_job_and_stepless_job():
    """A job that isn't a mapping, or one with no `steps:`, contributes nothing."""
    doc = {"jobs": {"bad": "not-a-mapping", "empty": {}}}
    assert cl.analyze(doc) == []


# ── check_file ────────────────────────────────────────────────────────────────


def test_check_file_reports_line_and_message(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cl, "REPO_ROOT", tmp_path)
    wf = tmp_path / "wf.yaml"
    wf.write_text(
        "jobs:\n  build:\n    steps:\n      - run: |\n"
        + "".join(f"          echo line{i}\n" for i in range(cl.MAX_LINES + 1))
    )
    out = cl.check_file(wf)
    assert len(out) == 1
    line, message = out[0]
    assert line == 4  # the `- run: |` step line
    assert "job build (run step 0)" in message


def test_check_file_reports_unparseable_yaml(tmp_path: Path):
    # Unparseable input can't be verified, so it must not silently read as
    # "no violations" — that would be the exact false-green this tool exists to catch.
    bad = tmp_path / "bad.yaml"
    bad.write_text("jobs: [unbalanced\n")
    out = cl.check_file(bad)
    assert len(out) == 1
    line, message = out[0]
    assert line is None
    assert "could not parse as YAML" in message


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_clean_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(cl, "workflow_files", lambda: [])
    assert cl.main() == 0
    assert capsys.readouterr().out == ""


def test_main_reports_and_fails_on_violation(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(cl, "REPO_ROOT", tmp_path)
    wf = tmp_path / "wf.yaml"
    wf.write_text(
        "jobs:\n  build:\n    steps:\n      - run: |\n"
        + "".join(f"          echo line{i}\n" for i in range(cl.MAX_LINES + 1))
    )
    monkeypatch.setattr(cl, "workflow_files", lambda: [wf])
    assert cl.main() == 1
    out = capsys.readouterr().out
    # The annotation is now navigable: it carries file= and line= attributes.
    assert "::error file=wf.yaml,line=4::" in out
    assert "oversized inline run: block(s)" in out


# ── repo-wide: the shipped workflows must pass ────────────────────────────────


def test_own_ci_workflow_passes_the_check():
    """ci-truth-serum's own CI workflow must satisfy the limit, so the repo
    dogfoods its own lint. Scoped to the product's workflow (not the inherited
    template's, which template-sync may rewrite independently)."""
    ci = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
    assert cl.check_file(ci) == [], cl.check_file(ci)
