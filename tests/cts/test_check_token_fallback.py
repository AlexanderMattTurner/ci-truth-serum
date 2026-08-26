"""Tests for ci_truth_serum/check_token_fallback.py — the lint that bans
`${{ secrets.A || secrets.B }}` in token positions (a `token:` input or a
`GITHUB_TOKEN`/`GH_TOKEN` env var), where the fallback silently switches the
workflow's push identity the day the first secret is set.

Drives ``violations()`` for the line rules and ``main()`` for discovery and the
exit-code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_token_fallback.py", "check_token_fallback")

FALLBACK = "${{ secrets.SYNC_TOKEN || secrets.GITHUB_TOKEN }}"


@pytest.mark.parametrize(
    "line",
    [
        f"          token: {FALLBACK}",
        f"          github-token: {FALLBACK}",
        f"          github_token: {FALLBACK}",
        f"          GITHUB_TOKEN: {FALLBACK}",
        f"          GH_TOKEN: {FALLBACK}",
        "  gh_token: ${{secrets.A||secrets.B}}",  # tight spacing still matches
        f"          Token: {FALLBACK}",  # key match is case-insensitive
    ],
)
def test_fallback_in_token_position_is_flagged(line: str) -> None:
    assert mod.violations(f"jobs:\n  x:\n    steps:\n      - env:\n{line}\n") == [5]


# Legitimate corpus: none of these are a secret-to-secret fallback in a token
# position, so the negative corpus must produce ZERO findings.
@pytest.mark.parametrize(
    "line",
    [
        "          token: ${{ secrets.ONE_TOKEN }}",  # single secret
        "          token: ${{ secrets.PAT || github.token }}",  # visible fallback target
        "          NTFY_URL: ${{ secrets.URL_A || secrets.URL_B }}",  # not a token key
        f"          my-token-helper: {FALLBACK}",  # key only contains 'token'
        f"          # token: {FALLBACK}",  # commented out
        "          token: ${{ inputs.token || secrets.PAT }}",  # left side not a secret
    ],
)
def test_non_token_or_non_fallback_lines_pass(line: str) -> None:
    assert mod.violations(f"jobs:\n  x:\n    steps:\n      - env:\n{line}\n") == []


def test_opt_out_same_line_and_preceding_line() -> None:
    same = f"    token: {FALLBACK} # token-fallback-ok: fork fallback is designed\n"
    above = (
        f"    # token-fallback-ok: fork fallback is designed\n    token: {FALLBACK}\n"
    )
    assert mod.violations(same) == []
    assert mod.violations(above) == []


def test_multiple_hits_report_each_line() -> None:
    text = f"    token: {FALLBACK}\n    ok: 1\n    GH_TOKEN: {FALLBACK}\n"
    assert mod.violations(text) == [1, 3]


def test_main_flags_violation_and_reports_file(tmp_path, monkeypatch, capsys) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "bad.yaml").write_text(
        f"jobs:\n  j:\n    env:\n      token: {FALLBACK}\n", encoding="utf-8"
    )
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(mod, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yaml,line=4::" in out


def test_main_passes_clean_repo_and_scans_actions(tmp_path, monkeypatch) -> None:
    wf = tmp_path / ".github" / "workflows"
    act = tmp_path / ".github" / "actions" / "a"
    wf.mkdir(parents=True)
    act.mkdir(parents=True)
    (wf / "ok.yaml").write_text(
        "jobs:\n  j:\n    env:\n      token: ${{ secrets.X }}\n", encoding="utf-8"
    )
    (act / "action.yaml").write_text(
        "runs:\n  using: composite\n  steps: []\n", encoding="utf-8"
    )
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(mod, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    assert mod.main() == 0


# ── comments are not uses of the idiom ───────────────────────────────────
def _run_main(tmp_path, monkeypatch, workflow_text: str) -> int:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "w.yaml").write_text(workflow_text, encoding="utf-8")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(mod, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    return mod.main()


def test_main_trailing_comment_discussing_the_idiom_is_not_a_use(
    tmp_path, monkeypatch
) -> None:
    text = (
        "jobs:\n  j:\n    steps:\n      - env:\n"
        "          token: ${{ secrets.PINNED }}"
        "  # Deliberately not a `secrets.A || secrets.B` expression\n"
    )
    assert _run_main(tmp_path, monkeypatch, text) == 0


def test_main_real_fallback_is_still_flagged(tmp_path, monkeypatch) -> None:
    text = f"jobs:\n  j:\n    env:\n      token: {FALLBACK}\n"
    assert _run_main(tmp_path, monkeypatch, text) == 1


@pytest.mark.parametrize("quote", ['"', "'"])
def test_fallback_after_a_quoted_hash_is_still_flagged(quote: str) -> None:
    # The false-negative guard: a `#` inside a quoted scalar is content, so the
    # fallback after it on the same line must still be seen. A naive cut at the
    # first `#` would blind the lint to exactly this line.
    line = f"      token: {quote}#42 ${{{{ secrets.A || secrets.B }}}}{quote}"
    assert mod.violations(f"jobs:\n  j:\n    env:\n{line}\n") == [4]


def test_fallback_after_a_hash_comment_line_in_a_run_block_is_still_flagged() -> None:
    # `#` inside a block scalar opens no YAML comment, so the block's later
    # lines are still scanned rather than swallowed to end of file.
    text = (
        "jobs:\n  j:\n    steps:\n      - run: |\n"
        "          # writing the config\n"
        f"          token: {FALLBACK}\n"
    )
    assert mod.violations(text) == [6]


def test_hash_led_comment_line_inside_a_run_block_is_not_a_token_position() -> None:
    text = (
        "jobs:\n  j:\n    steps:\n      - run: |\n"
        f"          # token: {FALLBACK}\n"
        "          echo done\n"
    )
    assert mod.violations(text) == []


def test_unparseable_workflow_is_still_scanned(tmp_path, monkeypatch) -> None:
    # A file PyYAML cannot tokenize is scanned unstripped rather than skipped —
    # a broken workflow must not become a hole in the lint.
    text = f"a: 'unterminated\njobs:\n  j:\n    env:\n      token: {FALLBACK}\n"
    assert _run_main(tmp_path, monkeypatch, text) == 1


def test_main_flags_composite_action_files(tmp_path, monkeypatch, capsys) -> None:
    act = tmp_path / ".github" / "actions" / "a"
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    act.mkdir(parents=True)
    (act / "action.yml").write_text(
        f"runs:\n  using: composite\n  steps:\n    - env:\n        GH_TOKEN: {FALLBACK}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", tmp_path / ".github" / "workflows")
    monkeypatch.setattr(mod, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    assert mod.main() == 1
    assert "action.yml" in capsys.readouterr().out
