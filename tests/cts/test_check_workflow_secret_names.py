"""Tests for ci_truth_serum/check_workflow_secret_names.py — the round-trip contract
between the `secrets.*` / `vars.*` names workflows reference and the checked-in
`.github/workflow-secrets.txt` allowlist.

Drives ``referenced_names()`` / ``parse_allowlist()`` / ``check_repo()`` for the
rules and ``main()`` for discovery and the exit-code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_workflow_secret_names.py", "check_workflow_secret_names")


# ── referenced_names ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text, expected",
    [
        ("t: ${{ secrets.MY_TOKEN }}", {"MY_TOKEN"}),
        ("o: ${{ vars.TEMPLATE_ORG }}", {"TEMPLATE_ORG"}),
        ("t: ${{ secrets.A || secrets.B }}\nv: ${{ vars.C }}", {"A", "B", "C"}),
        ("t: ${{ secrets.GITHUB_TOKEN }}", set()),  # implicit, never listed
        ("t: ${{ github.token }}", set()),  # context form, implicit
        ("plain text, no refs", set()),
        ("x: secrets.lower_name", {"lower_name"}),
        ("x: secrets.9BAD", set()),  # not a valid secret name
    ],
)
def test_referenced_names(text: str, expected: set) -> None:
    assert mod.referenced_names(text) == expected


# ── parse_allowlist ──────────────────────────────────────────────────────
def test_parse_allowlist_comments_blanks_and_trailing_scope_notes() -> None:
    text = (
        "# header comment\n"
        "\n"
        "ANTHROPIC_API_KEY  # org scope\n"
        "NTFY_URL\n"
        "  SPACED_NAME  \n"
    )
    assert mod.parse_allowlist(text) == {"ANTHROPIC_API_KEY", "NTFY_URL", "SPACED_NAME"}


# ── check_repo ───────────────────────────────────────────────────────────
def test_in_sync_passes() -> None:
    assert mod.check_repo({"A", "B"}, "A\nB\n") == []


def test_no_refs_and_no_file_passes() -> None:
    assert mod.check_repo(set(), None) == []


def test_missing_file_with_refs_prints_the_fix() -> None:
    msgs = mod.check_repo({"B", "A"}, None)
    assert len(msgs) == 1
    assert "does not exist" in msgs[0]
    # corrected content is sorted and complete
    assert "A\nB\n" in msgs[0]


def test_unlisted_reference_fails_and_prints_the_fix() -> None:
    msgs = mod.check_repo({"A", "TYPO"}, "A\n")
    assert len(msgs) == 1
    assert "referenced but not listed" in msgs[0] and "TYPO" in msgs[0]
    assert "A\nTYPO\n" in msgs[0]


def test_stale_entry_fails_both_directions() -> None:
    msgs = mod.check_repo({"A"}, "A\nGONE\n")
    assert len(msgs) == 1
    assert "no longer referenced" in msgs[0] and "GONE" in msgs[0]


def test_refs_empty_but_file_lists_names_fails() -> None:
    msgs = mod.check_repo(set(), "GHOST\n")
    assert len(msgs) == 1 and "GHOST" in msgs[0]


# ── main ─────────────────────────────────────────────────────────────────
def _repo(tmp_path, monkeypatch, workflow_text: str, allowlist: str | None):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "a.yaml").write_text(workflow_text)
    if allowlist is not None:
        (tmp_path / ".github" / "workflow-secrets.txt").write_text(allowlist)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(mod, "ACTIONS_DIR", tmp_path / ".github" / "actions")


def test_main_in_sync_repo_passes(tmp_path, monkeypatch) -> None:
    _repo(tmp_path, monkeypatch, "t: ${{ secrets.ONE }}\n", "ONE\n")
    assert mod.main() == 0


def test_main_out_of_sync_repo_fails_with_fix(tmp_path, monkeypatch, capsys) -> None:
    _repo(tmp_path, monkeypatch, "t: ${{ secrets.ONE }}\n", "OTHER\n")
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "::error file=" in out and "ONE\n" in out


def test_main_scans_composite_actions_too(tmp_path, monkeypatch) -> None:
    _repo(tmp_path, monkeypatch, "name: x\n", None)
    act = tmp_path / ".github" / "actions" / "n"
    act.mkdir(parents=True)
    (act / "action.yaml").write_text("x: ${{ secrets.FROM_ACTION }}\n")
    assert mod.main() == 1  # referenced but no allowlist file


def test_main_github_token_alone_needs_no_allowlist(tmp_path, monkeypatch) -> None:
    _repo(tmp_path, monkeypatch, "t: ${{ secrets.GITHUB_TOKEN }}\n", None)
    assert mod.main() == 0
