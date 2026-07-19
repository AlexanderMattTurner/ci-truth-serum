"""Tests for hooks/check_requires_concurrency.py — the (opinionated) pre-commit
lint that requires a concurrency block on every pull_request(_target)-triggered
workflow, so a new push cancels the superseded run instead of stacking a second
full run on a capped runner pool. Sibling to check_concurrency, which only
validates a block that is already present; this one requires the block to exist."""

from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, load_hook

rc = load_hook("check_requires_concurrency.py", "check_requires_concurrency")

PLAIN_JOBS = "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n"
WF_CONCURRENCY = (
    "concurrency:\n"
    "  group: ${{ github.workflow }}-${{ github.head_ref || github.ref }}\n"
    "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n"
)
JOB_CONCURRENCY = (
    "jobs:\n"
    "  build:\n"
    "    runs-on: ubuntu-latest\n"
    "    concurrency:\n"
    "      group: build-${{ github.event.pull_request.number }}\n"
    "    steps: []\n"
)


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# ── the violation: PR-triggered, no concurrency anywhere ────────────────────────


@pytest.mark.parametrize(
    "on_block",
    [
        "on:\n  pull_request:\n",  # mapping, empty config
        "on:\n  pull_request:\n    types: [opened]\n",  # mapping, with config
        "on:\n  pull_request_target:\n",  # the _target variant
        "on: pull_request\n",  # scalar shorthand
        "on: [push, pull_request]\n",  # list form
        "on:\n  push:\n    branches: [main]\n  pull_request:\n",  # push + PR mapping
    ],
)
def test_pr_trigger_without_any_concurrency_is_flagged(tmp_path, on_block):
    """Every spelling of a pull_request trigger, with no concurrency block, is an error."""
    body = "name: x\n" + on_block + PLAIN_JOBS
    result = rc.check_file(_write(tmp_path, body))
    assert result is not None
    _line, message = result
    assert "concurrency" in message
    assert "cancelling the superseded" in message


def test_flag_points_at_the_pr_trigger_line(tmp_path):
    """The annotation line is the pull_request trigger, not line 1."""
    body = "name: x\non:\n  push:\n    branches: [main]\n  pull_request:\n" + PLAIN_JOBS
    line, _message = rc.check_file(_write(tmp_path, body))
    # Lines: 1 name, 2 on, 3 push, 4 branches, 5 pull_request.
    assert line == 5


# ── satisfied: concurrency present ──────────────────────────────────────────────


def test_workflow_level_concurrency_satisfies(tmp_path):
    body = "name: x\non:\n  pull_request:\n" + WF_CONCURRENCY + PLAIN_JOBS
    assert rc.check_file(_write(tmp_path, body)) is None


def test_job_level_concurrency_satisfies(tmp_path):
    """Job-level concurrency (the required-check-serialization shape) counts —
    a static workflow-level lock would be banned on a required check, so this
    placement must not be a violation."""
    body = "name: x\non:\n  pull_request:\n" + JOB_CONCURRENCY
    assert rc.check_file(_write(tmp_path, body)) is None


def test_scalar_form_workflow_concurrency_satisfies(tmp_path):
    """concurrency: mygroup (scalar group shorthand) is still a declared block."""
    body = "name: x\non:\n  pull_request:\nconcurrency: my-group\n" + PLAIN_JOBS
    assert rc.check_file(_write(tmp_path, body)) is None


# ── exempt: not PR-triggered ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "on_block",
    [
        "on:\n  push:\n    branches: [main]\n",  # push only
        "on:\n  schedule:\n    - cron: '0 9 * * 1'\n",  # schedule only
        "on:\n  workflow_call:\n",  # reusable — inherits caller's concurrency
        "on: workflow_dispatch\n",  # manual only
        "on: [push, workflow_dispatch]\n",  # list, no PR trigger
    ],
)
def test_non_pr_triggered_workflow_is_exempt(tmp_path, on_block):
    """A workflow with no pull_request trigger doesn't fan out per PR, so it needs
    no concurrency block."""
    body = "name: x\n" + on_block + PLAIN_JOBS
    assert rc.check_file(_write(tmp_path, body)) is None


# ── opt-out ─────────────────────────────────────────────────────────────────────


def test_opt_out_comment_suppresses_the_error(tmp_path):
    body = f"# {rc.OPT_OUT}\nname: x\non:\n  pull_request:\n" + PLAIN_JOBS
    assert rc.check_file(_write(tmp_path, body)) is None


def test_opt_out_token_in_string_value_does_not_suppress(tmp_path):
    """The opt-out counts only inside a real `#` comment — a workflow `name:` that
    literally contains the token must still be flagged (no byte-stream fail-open)."""
    body = f'name: "{rc.OPT_OUT}"\non:\n  pull_request:\n' + PLAIN_JOBS
    assert rc.check_file(_write(tmp_path, body)) is not None


# ── malformed / edge inputs (no crash) ──────────────────────────────────────────


def test_malformed_yaml_is_reported_not_raised(tmp_path):
    """An unparseable workflow is reported as a violation (line None), not a crash."""
    result = rc.check_file(_write(tmp_path, "on: [pull_request\njobs: {\n"))
    assert result is not None
    line, message = result
    assert line is None
    assert "could not parse as YAML" in message


def test_missing_on_key_is_exempt(tmp_path):
    body = "name: x\n" + PLAIN_JOBS
    assert rc.check_file(_write(tmp_path, body)) is None


def test_non_dict_yaml_top_level_is_ignored(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- item1\n- item2\n")
    assert rc.check_file(path) is None


def test_non_mapping_jobs_with_pr_trigger_is_still_flagged(tmp_path):
    """jobs: scalar → no job-level concurrency reachable → the PR workflow still
    owes a workflow-level block."""
    body = "name: x\non:\n  pull_request:\njobs: scalar-not-a-mapping\n"
    assert rc.check_file(_write(tmp_path, body)) is not None


# ── main ────────────────────────────────────────────────────────────────────────


def test_main_reports_violation_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\non:\n  pull_request:\n" + PLAIN_JOBS)
    monkeypatch.setattr(rc, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(rc, "REPO_ROOT", tmp_path)
    assert rc.main() == 1
    out = capsys.readouterr().out
    assert "concurrency" in out
    assert "violation" in out


def test_main_clean_repo_returns_zero(tmp_path, monkeypatch, capsys):
    good = tmp_path / "ok.yaml"
    good.write_text("name: x\non:\n  pull_request:\n" + WF_CONCURRENCY + PLAIN_JOBS)
    monkeypatch.setattr(rc, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(rc, "REPO_ROOT", tmp_path)
    assert rc.main() == 0, capsys.readouterr().out


def test_all_shipped_workflows_pass(monkeypatch, capsys):
    """The repo dogfoods this lint: every ci-truth-serum PR workflow declares
    concurrency (or is a reusable/non-PR workflow), so none are flagged."""
    workflows = REPO_ROOT / ".github" / "workflows"
    monkeypatch.setattr(rc, "REPO_ROOT", REPO_ROOT)
    monkeypatch.setattr(rc, "WORKFLOWS_DIR", workflows)
    assert rc.main() == 0, capsys.readouterr().out
