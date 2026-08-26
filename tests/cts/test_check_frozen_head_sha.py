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
    path.write_text(body, encoding="utf-8")
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
    path.write_text("- a\n- b\n", encoding="utf-8")
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
        + "      - run: git diff ${{ github.event.pull_request.head.sha }}...HEAD\n",
        encoding="utf-8",
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


# ── the env-indirect route: judged by what SPENDS the variable ────────────────
#
# Routing the expression through `env:` is the shape the template-injection lints
# demand, so the binding alone is not the defect. On a 50-workflow tree 41 of the
# 45 uses sat under `env:` and none was a defect. These pin the two positions
# where the frozen value really does mis-scope, and the benign ones beside them.

ENV_BIND = (
    "        env:\n          HEAD_SHA: ${{ github.event.pull_request.head.sha }}\n"
)


def test_env_var_spent_as_a_git_range_endpoint_is_flagged(tmp_path):
    body = HEADER + "      - run: git diff $HEAD_SHA...HEAD\n" + ENV_BIND
    result = fh.check_file(_write(tmp_path, body))
    assert len(result) == 1
    assert "HEAD_SHA" in result[0][1]


def test_braced_env_var_in_a_two_dot_range_is_flagged(tmp_path):
    body = HEADER + "      - run: git log ${HEAD_SHA}..HEAD\n" + ENV_BIND
    assert len(fh.check_file(_write(tmp_path, body))) == 1


def test_job_level_env_reaches_its_steps(tmp_path):
    body = (
        "name: x\non:\n  pull_request:\njobs:\n  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      SHA: ${{ github.event.pull_request.head.sha }}\n"
        "    steps:\n"
        "      - run: git diff $SHA...HEAD\n"
    )
    assert len(fh.check_file(_write(tmp_path, body))) == 1


def test_env_var_spent_as_a_checkout_ref_is_flagged(tmp_path):
    body = (
        HEADER
        + "      - uses: actions/checkout@v4\n"
        + "        with:\n"
        + "          ref: ${{ env.HEAD_SHA }}\n"
        + ENV_BIND
    )
    assert len(fh.check_file(_write(tmp_path, body))) == 1


def test_benign_env_var_use_is_clean(tmp_path):
    """Posting a status on the SHA that triggered the run is correct, and it is
    what most of the 41 measured bindings do."""
    body = (
        HEADER
        + "      - run: gh api repos/o/r/statuses/$HEAD_SHA -f state=success\n"
        + ENV_BIND
    )
    assert fh.check_file(_write(tmp_path, body)) == []


def test_range_inside_a_printed_message_is_clean(tmp_path):
    """The first of the two probes every shell lint survives: the range sits in a
    string an `echo` prints, so no `git` command carries it."""
    body = (
        HEADER
        + '      - run: echo "run git diff $HEAD_SHA...HEAD to see it"\n'
        + ENV_BIND
    )
    assert fh.check_file(_write(tmp_path, body)) == []


def test_range_inside_a_heredoc_body_is_clean(tmp_path):
    """The second probe: a heredoc body is data written to a file, not shell the
    runner executes."""
    body = (
        HEADER
        + "      - run: |\n"
        + "          cat <<'EOF' > doc.txt\n"
        + "          git diff $HEAD_SHA...HEAD\n"
        + "          EOF\n"
        + ENV_BIND
    )
    assert fh.check_file(_write(tmp_path, body)) == []


def test_a_longer_variable_name_is_not_matched(tmp_path):
    """`$HEAD_SHA_BASE` is a different variable; matching it would name the wrong
    binding in the report and flag a range the frozen SHA never reaches."""
    body = HEADER + "      - run: git diff $HEAD_SHA_BASE...HEAD\n" + ENV_BIND
    assert fh.check_file(_write(tmp_path, body)) == []


def test_a_non_bash_shell_is_not_parsed_as_bash(tmp_path):
    body = (
        HEADER
        + "      - shell: pwsh\n"
        + "        run: git diff $HEAD_SHA...HEAD\n"
        + ENV_BIND
    )
    assert fh.check_file(_write(tmp_path, body)) == []


def test_env_route_honours_the_step_opt_out(tmp_path):
    body = (
        HEADER
        + "      - run: git diff $HEAD_SHA...HEAD\n"
        + "        env:\n"
        + "          # frozen-head-ok: the pre-trigger head is the point here\n"
        + "          HEAD_SHA: ${{ github.event.pull_request.head.sha }}\n"
    )
    assert fh.check_file(_write(tmp_path, body)) == []


def test_one_step_earns_one_finding(tmp_path):
    """A step that spends the SHA directly AND through an env var has one edit to
    make, so it must not be reported twice."""
    body = (
        HEADER
        + "      - run: git diff ${{ github.event.pull_request.head.sha }}...HEAD"
        + " && git log $HEAD_SHA...HEAD\n"
        + ENV_BIND
    )
    assert len(fh.check_file(_write(tmp_path, body))) == 1


def test_a_custom_bash_template_shell_is_still_parsed(tmp_path):
    """A `shell:` template is still bash. Skipping it would fail open on the one
    spelling an author writes deliberately."""
    body = (
        HEADER
        + "      - shell: bash --noprofile --norc -eo pipefail {0}\n"
        + "        run: git diff $HEAD_SHA...HEAD\n"
        + ENV_BIND
    )
    assert len(fh.check_file(_write(tmp_path, body))) == 1
