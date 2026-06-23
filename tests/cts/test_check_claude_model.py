"""Tests for hooks/check_claude_model.py — the pre-commit lint that flags an
``anthropics/claude-code-action`` step with no explicit model (which silently
runs on the action's default Opus tier).

Drives the module's functions directly so every branch (step discovery, the
model-presence detector, the opt-out comment, the order-pairing in check_file,
the actions/-dir glob, and main()'s exit code) is asserted in isolation.
"""

from pathlib import Path

from tests._helpers import load_hook

ccm = load_hook("check_claude_model.py", "check_claude_model")


def _write(dirpath: Path, name: str, body: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / name
    path.write_text(body)
    return path


def _wf(uses_line: str, with_block: str = "") -> str:
    """A minimal one-step workflow whose single step is `uses_line`."""
    body = f"on:\n  push:\njobs:\n  j:\n    steps:\n      - {uses_line}\n"
    return body + with_block


PINNED = _wf(
    "uses: anthropics/claude-code-action@abc123",
    '        with:\n          claude_args: "--model claude-sonnet-4-6 --allowedTools Bash"\n',
)
UNPINNED = _wf(
    "uses: anthropics/claude-code-action@abc123",
    '        with:\n          claude_args: "--allowedTools Bash Read"\n',
)
NO_WITH = _wf("uses: anthropics/claude-code-action@abc123")


# ── uses_action ──────────────────────────────────────────────────────────
def test_uses_action_matches_with_ref():
    assert ccm.uses_action({"uses": "anthropics/claude-code-action@v1.0.154"}) is True


def test_uses_action_matches_without_ref():
    assert ccm.uses_action({"uses": "anthropics/claude-code-action"}) is True


def test_uses_action_ignores_base_action():
    # claude-code-base-action is a different action with separate model handling.
    assert ccm.uses_action({"uses": "anthropics/claude-code-base-action@v1"}) is False


def test_uses_action_ignores_other_and_non_step():
    assert ccm.uses_action({"uses": "actions/checkout@v4"}) is False
    assert ccm.uses_action({"run": "echo hi"}) is False


# ── has_model ────────────────────────────────────────────────────────────
def test_has_model_true_for_model_flag_in_claude_args():
    assert ccm.has_model({"with": {"claude_args": "--model claude-sonnet-4-6"}}) is True


def test_has_model_true_for_model_input():
    assert ccm.has_model({"with": {"model": "claude-sonnet-4-6"}}) is True


def test_has_model_false_when_claude_args_lacks_model():
    assert ccm.has_model({"with": {"claude_args": "--allowedTools Bash"}}) is False


def test_has_model_false_when_no_with():
    assert ccm.has_model({}) is False


def test_has_model_false_when_with_not_mapping():
    assert ccm.has_model({"with": "oops"}) is False


# ── check_file ───────────────────────────────────────────────────────────
def test_check_file_passes_pinned_step(tmp_path):
    assert ccm.check_file(_write(tmp_path, "wf.yaml", PINNED)) == []


def test_check_file_flags_unpinned_step(tmp_path):
    found = ccm.check_file(_write(tmp_path, "wf.yaml", UNPINNED))
    assert len(found) == 1
    line, message = found[0]
    assert line == 6  # the `uses:` line
    assert "defaults to Opus" in message and ccm.OPT_OUT in message


def test_check_file_flags_step_with_no_with_block(tmp_path):
    found = ccm.check_file(_write(tmp_path, "wf.yaml", NO_WITH))
    assert [line for line, _ in found] == [6]


def test_check_file_respects_opt_out_comment(tmp_path):
    body = _wf(
        f"uses: anthropics/claude-code-action@abc123  # {ccm.OPT_OUT}: monitor eval needs default",
        '        with:\n          claude_args: "--allowedTools Bash"\n',
    )
    assert ccm.check_file(_write(tmp_path, "wf.yaml", body)) == []


def test_check_file_ignores_folded_claude_args_with_model(tmp_path):
    # A `>-` folded scalar still resolves to a string containing --model.
    body = _wf(
        "uses: anthropics/claude-code-action@abc123",
        "        with:\n          claude_args: >-\n            --model claude-haiku-4-5\n            --allowedTools Bash\n",
    )
    assert ccm.check_file(_write(tmp_path, "wf.yaml", body)) == []


def test_check_file_pairs_lines_across_multiple_steps(tmp_path):
    # First step pinned, second unpinned — only the second's line is flagged.
    body = (
        "on:\n  push:\njobs:\n  j:\n    steps:\n"
        "      - uses: anthropics/claude-code-action@abc123\n"
        '        with:\n          claude_args: "--model claude-sonnet-4-6"\n'
        "      - uses: anthropics/claude-code-action@abc123\n"
        '        with:\n          claude_args: "--allowedTools Bash"\n'
    )
    found = ccm.check_file(_write(tmp_path, "wf.yaml", body))
    assert [line for line, _ in found] == [9]


def test_check_file_finds_step_in_composite_action(tmp_path):
    body = (
        "name: composite\nruns:\n  using: composite\n  steps:\n"
        "      - uses: anthropics/claude-code-action@abc123\n"
        '        with:\n          claude_args: "--allowedTools Bash"\n'
    )
    found = ccm.check_file(_write(tmp_path, "action.yaml", body))
    assert [line for line, _ in found] == [5]


def test_check_file_ignores_non_mapping_document(tmp_path):
    assert ccm.check_file(_write(tmp_path, "wf.yaml", "- a\n- b\n")) == []


def test_check_file_ignores_workflow_without_action(tmp_path):
    body = "on:\n  push:\njobs:\n  j:\n    steps:\n      - uses: actions/checkout@v4\n"
    assert ccm.check_file(_write(tmp_path, "wf.yaml", body)) == []


# ── main ─────────────────────────────────────────────────────────────────
def _point_at(tmp_path, monkeypatch):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ccm, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ccm, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(ccm, "ACTIONS_DIR", tmp_path / "nonexistent")
    return wf


def test_main_returns_zero_when_clean(tmp_path, monkeypatch, capsys):
    wf = _point_at(tmp_path, monkeypatch)
    _write(wf, "ok.yaml", PINNED)
    assert ccm.main() == 0
    assert "ERROR" not in capsys.readouterr().out


def test_main_reports_and_fails_on_violation(tmp_path, monkeypatch, capsys):
    wf = _point_at(tmp_path, monkeypatch)
    _write(wf, "bad.yaml", UNPINNED)
    _write(wf, "ok.yaml", PINNED)
    assert ccm.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yaml,line=6::" in out
    assert "1 violation(s) found" in out
