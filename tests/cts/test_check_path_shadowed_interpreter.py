"""Tests for ci_truth_serum/check_path_shadowed_interpreter.py — the workflow
lint that bans a bare `python`/`python3` command word in any `run:` step that
executes after an agent action already rewrote `$GITHUB_PATH`.

Neither source repo ships a test file for this check, so every case here is
written from the module's own stated rules: the docstring's PATH-rebuild
argument, the exact word-boundary shapes it must spare, the two false-
negative admissions, and the opt-out contract.
"""

from pathlib import Path

import pytest

from tests._helpers import load_hook

mod = load_hook("check_path_shadowed_interpreter.py", "check_path_shadowed_interpreter")

AGENT = ("anthropics/claude-code-action",)


def _workflow(steps: str) -> str:
    return f"jobs:\n  build:\n    steps:\n{steps}"


AGENT_STEP = "      - uses: anthropics/claude-code-action@v1\n"


# ── bare_python_words ─────────────────────────────────────────────────────
def test_flags_a_bare_python3_command():
    assert mod.bare_python_words("python3 script.py") == [(1, "python3 script.py")]


def test_flags_a_bare_python_command():
    assert mod.bare_python_words("python script.py") == [(1, "python script.py")]


def test_spares_a_pathed_interpreter():
    assert mod.bare_python_words(".venv/bin/python3 script.py") == []


def test_spares_a_dotted_minor_version():
    assert mod.bare_python_words("python3.11 script.py") == []


def test_spares_a_name_that_merely_starts_with_python():
    assert mod.bare_python_words("python-dateutil-check") == []


def test_spares_a_flag_shaped_word():
    assert mod.bare_python_words("build-tool --python 3.12") == []


def test_flags_a_bare_default_expansion():
    assert mod.bare_python_words('"${PYTHON:-python3}" script.py') != []


def test_spares_a_pathed_default_expansion():
    assert mod.bare_python_words('"${PYTHON:-.venv/bin/python3}" script.py') == []


def test_does_not_fire_on_a_message_string():
    # A word inside a message a command PRINTS is not an executed command.
    assert mod.bare_python_words('gb_warn "run python3 now"') == []


def test_does_not_fire_on_a_heredoc_body():
    src = "cat <<'EOF' > doc.txt\npython3 script.py\nEOF\n"
    assert mod.bare_python_words(src) == []


def test_does_not_fire_on_a_comment():
    assert mod.bare_python_words("# python3 script.py\necho hi") == []


def test_reports_the_commands_own_line_number():
    src = "echo one\npython3 script.py\necho three\n"
    assert mod.bare_python_words(src) == [(2, "python3 script.py")]


def test_flags_every_bare_command_in_a_multi_line_script():
    src = "python3 a.py\npython b.py\n"
    assert [line for line, _ in mod.bare_python_words(src)] == [1, 2]


# ── violations(): the downstream-of-agent gate ────────────────────────────
def test_violations_flags_a_step_after_the_agent_step():
    text = _workflow(AGENT_STEP + "      - run: python3 script.py\n")
    found = mod.violations(text, AGENT)
    assert len(found) == 1
    line, message = found[0]
    assert line == text.splitlines().index("      - run: python3 script.py") + 1
    assert "reaches Python by bare name" in message


def test_violations_ignores_a_step_before_the_agent_step():
    text = _workflow("      - run: python3 script.py\n" + AGENT_STEP)
    assert mod.violations(text, AGENT) == []


def test_violations_ignores_a_bare_python_with_no_agent_step_at_all():
    text = _workflow("      - run: python3 script.py\n")
    assert mod.violations(text, AGENT) == []


def test_violations_flags_every_step_after_the_agent_step():
    text = _workflow(
        AGENT_STEP + "      - run: python3 a.py\n" + "      - run: python b.py\n"
    )
    assert len(mod.violations(text, AGENT)) == 2


def test_violations_respects_the_opt_out_on_the_line():
    text = _workflow(
        AGENT_STEP
        + "      - run: |\n"
        + "          python3 script.py  # allow-path-shadowed-interpreter: pinned\n"
    )
    assert mod.violations(text, AGENT) == []


def test_violations_respects_the_opt_out_on_the_comment_line_above():
    text = _workflow(
        AGENT_STEP
        + "      - run: |\n"
        + "          # allow-path-shadowed-interpreter: pinned upstream\n"
        + "          python3 script.py\n"
    )
    assert mod.violations(text, AGENT) == []


def test_violations_refuses_an_opt_out_with_no_reason():
    text = _workflow(
        AGENT_STEP
        + "      - run: |\n"
        + "          python3 script.py  # allow-path-shadowed-interpreter\n"
    )
    assert mod.violations(text, AGENT) != []


def test_violations_reports_malformed_yaml():
    found = mod.violations("jobs: {\n", AGENT)
    assert found and found[0][0] == 1
    assert "could not parse as YAML" in found[0][1]


def test_violations_ignores_a_non_mapping_document():
    assert mod.violations("- a\n- b\n", AGENT) == []


def test_violations_ignores_a_document_with_no_jobs():
    assert mod.violations("on:\n  push:\n", AGENT) == []


# ── the two admitted false negatives ──────────────────────────────────────
def test_does_not_expand_a_local_composite_action():
    # The bare python3 lives inside the COMPOSITE's own action.yaml, which this
    # check never opens — a known blind spot, not a crash.
    text = _workflow(AGENT_STEP + "      - uses: ./.github/actions/x\n")
    assert mod.violations(text, AGENT) == []


def test_does_not_follow_a_referenced_script_file():
    # The bare python3 lives inside run.sh, which the run: text only NAMES.
    text = _workflow(AGENT_STEP + "      - run: bash .github/scripts/run.sh\n")
    assert mod.violations(text, AGENT) == []


# ── --agent-action: repeatable, extends the default ───────────────────────
def test_default_agent_action_is_claude_code_action():
    text = _workflow(AGENT_STEP + "      - run: python3 script.py\n")
    assert mod.violations(text, mod.DEFAULT_AGENT_ACTIONS) != []


def test_a_second_agent_action_extends_rather_than_replaces():
    text = _workflow(
        "      - uses: some-org/other-agent@v1\n" + "      - run: python3 x.py\n"
    )
    assert mod.violations(text, ("some-org/other-agent",)) != []
    # The house action is still recognized alongside the added one.
    text2 = _workflow(AGENT_STEP + "      - run: python3 x.py\n")
    assert (
        mod.violations(text2, mod.DEFAULT_AGENT_ACTIONS + ("some-org/other-agent",))
        != []
    )


# ── main(): argv/exit-code contract, empty-scan notice ────────────────────
def _write(dirpath: Path, name: str, body: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / name
    path.write_text(body, encoding="utf-8")
    return path


def test_main_returns_one_on_a_violation(tmp_path: Path, capsys: pytest.CaptureFixture):
    wf = tmp_path / ".github" / "workflows"
    _write(wf, "w.yaml", _workflow(AGENT_STEP + "      - run: python3 x.py\n"))
    assert mod.main(["--repo-root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/w.yaml,line=" in out
    assert "1 path-shadowed-interpreter violation(s) found" in out


def test_main_returns_zero_when_clean(tmp_path: Path, capsys: pytest.CaptureFixture):
    wf = tmp_path / ".github" / "workflows"
    _write(
        wf, "w.yaml", _workflow(AGENT_STEP + "      - run: .venv/bin/python3 x.py\n")
    )
    assert mod.main(["--repo-root", str(tmp_path)]) == 0
    assert "ERROR" not in capsys.readouterr().out


def test_main_notes_a_tree_with_no_workflow_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    assert mod.main(["--repo-root", str(tmp_path)]) == 0
    assert "scanned nothing" in capsys.readouterr().err


def test_main_extends_agent_actions_with_a_repeated_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    wf = tmp_path / ".github" / "workflows"
    _write(
        wf,
        "w.yaml",
        _workflow(
            "      - uses: some-org/other-agent@v1\n" + "      - run: python3 x.py\n"
        ),
    )
    assert mod.main(["--repo-root", str(tmp_path)]) == 0
    assert (
        mod.main(
            ["--repo-root", str(tmp_path), "--agent-action", "some-org/other-agent"]
        )
        == 1
    )
