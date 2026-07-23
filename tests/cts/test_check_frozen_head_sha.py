"""Tests for ci_truth_serum/check_frozen_head_sha.py — the lint that flags
`github.event.pull_request.head.sha` in a step's run:/with: value. The event
payload is frozen at trigger time, so a force-push / autofix-amend moves the real
head and a range scoped to that SHA mis-scopes to the whole branch history."""

from pathlib import Path

from tests._helpers import REPO_ROOT, load_hook

fh = load_hook("check_frozen_head_sha.py", "check_frozen_head_sha")

HEADER = "name: x\non:\n  pull_request:\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# ── violations ────────────────────────────────────────────────────────────────


def test_frozen_head_sha_in_run_is_flagged(tmp_path):
    body = HEADER + (
        "      - run: git diff ${{ github.event.pull_request.head.sha }}...HEAD\n"
    )
    result = fh.check_file(_write(tmp_path, body))
    assert len(result) == 1
    line, message = result[0]
    assert "frozen" in message
    assert line is not None


def test_frozen_head_sha_in_with_value_is_flagged(tmp_path):
    body = HEADER + (
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.sha }}\n"
    )
    result = fh.check_file(_write(tmp_path, body))
    assert len(result) == 1
    assert "frozen" in result[0][1]


def test_two_violating_steps_flagged_separately(tmp_path):
    body = HEADER + (
        "      - run: git log ${{ github.event.pull_request.head.sha }}\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.sha }}\n"
    )
    result = fh.check_file(_write(tmp_path, body))
    assert len(result) == 2


def test_composite_action_run_step_is_flagged(tmp_path):
    body = (
        "name: c\ndescription: d\nruns:\n  using: composite\n  steps:\n"
        "    - shell: bash\n"
        "      run: echo ${{ github.event.pull_request.head.sha }}\n"
    )
    result = fh.check_file(_write(tmp_path, body, name="action.yaml"))
    assert len(result) == 1


# ── false-positive guards: only head.sha, only run:/with: ─────────────────────


def test_head_ref_is_not_flagged(tmp_path):
    """head.ref is a branch name re-resolved on checkout, not the frozen SHA."""
    body = HEADER + (
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.ref }}\n"
    )
    assert fh.check_file(_write(tmp_path, body)) == []


def test_base_sha_is_not_flagged(tmp_path):
    """base.sha is the correct anchor for a PR diff range."""
    body = HEADER + (
        "      - run: git diff ${{ github.event.pull_request.base.sha }}...HEAD\n"
    )
    assert fh.check_file(_write(tmp_path, body)) == []


def test_head_sha_in_env_is_not_flagged(tmp_path):
    """env: is deliberately out of scope (documented gap) — only run:/with: scanned."""
    body = HEADER + (
        "      - env:\n"
        "          H: ${{ github.event.pull_request.head.sha }}\n"
        "        run: echo hi\n"
    )
    assert fh.check_file(_write(tmp_path, body)) == []


# ── opt-out ───────────────────────────────────────────────────────────────────


def test_opt_out_in_run_block_suppresses(tmp_path):
    body = HEADER + (
        "      - run: |\n"
        "          # frozen-head-ok: comparing against the pre-trigger head on purpose\n"
        "          git push --force-with-lease=refs/x:${{ github.event.pull_request.head.sha }}\n"
    )
    assert fh.check_file(_write(tmp_path, body)) == []


def test_opt_out_trailing_a_with_value_suppresses(tmp_path):
    """A `#` comment trailing a with: value is discarded by PyYAML, so the opt-out
    is found via the step's source block, not the parsed value."""
    body = HEADER + (
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ github.event.pull_request.head.sha }}  # frozen-head-ok: exact head pin\n"
    )
    assert fh.check_file(_write(tmp_path, body)) == []


def test_opt_out_without_reason_does_not_suppress(tmp_path):
    """The reason is mandatory — a bare `# frozen-head-ok` still fails."""
    body = HEADER + (
        "      - run: |\n"
        "          # frozen-head-ok\n"
        "          git diff ${{ github.event.pull_request.head.sha }}...HEAD\n"
    )
    result = fh.check_file(_write(tmp_path, body))
    assert len(result) == 1


def test_opt_out_scoped_to_the_owning_step(tmp_path):
    """An opt-out in one step must not license a frozen SHA in a sibling step."""
    body = HEADER + (
        "      - run: |\n"
        "          # frozen-head-ok: legit here\n"
        "          echo ${{ github.event.pull_request.head.sha }}\n"
        "      - run: git diff ${{ github.event.pull_request.head.sha }}...HEAD\n"
    )
    result = fh.check_file(_write(tmp_path, body))
    assert len(result) == 1


# ── structural edge cases ─────────────────────────────────────────────────────


def test_malformed_yaml_is_reported_not_raised(tmp_path):
    result = fh.check_file(_write(tmp_path, "on: [pull_request\njobs: {\n"))
    assert len(result) == 1
    line, message = result[0]
    assert line is None
    assert "could not parse as YAML" in message


def test_non_dict_top_level_is_ignored(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n")
    assert fh.check_file(path) == []


def test_step_without_run_or_with_is_clean(tmp_path):
    body = HEADER + "      - uses: actions/checkout@v4\n"
    assert fh.check_file(_write(tmp_path, body)) == []


# ── _step_block unit coverage ─────────────────────────────────────────────────


def test_step_block_stops_at_next_sibling():
    lines = [
        "    steps:",
        "      - run: a",
        "        with:",
        "          x: y",
        "      - run: b",
    ]
    block = fh._step_block(lines, 2)  # 1-based line of `- run: a`
    assert "run: a" in block
    assert "x: y" in block
    assert "run: b" not in block


def test_step_block_out_of_range_returns_empty():
    assert fh._step_block(["a"], 99) == ""


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_reports_violation_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "bad.yaml").write_text(
        HEADER
        + "      - run: git diff ${{ github.event.pull_request.head.sha }}...HEAD\n"
    )
    monkeypatch.setattr(fh, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(fh, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(fh, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    rc = fh.main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "frozen" in out


def test_all_shipped_workflows_pass(monkeypatch, capsys):
    """The repo dogfoods this lint: no shipped workflow uses the frozen head SHA
    (base.sha and head.ref are used where a base/branch is needed)."""
    monkeypatch.setattr(fh, "REPO_ROOT", REPO_ROOT)
    monkeypatch.setattr(fh, "WORKFLOWS_DIR", REPO_ROOT / ".github" / "workflows")
    monkeypatch.setattr(fh, "ACTIONS_DIR", REPO_ROOT / ".github" / "actions")
    assert fh.main() == 0, capsys.readouterr().out
