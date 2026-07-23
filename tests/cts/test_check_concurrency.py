"""Tests for ci_truth_serum/check_concurrency.py — the (opinionated) pre-commit lint that
requires every workflow with a concurrency: block to set cancel-in-progress:
explicitly (any value), preventing the silent false default."""

from pathlib import Path

from tests._helpers import REPO_ROOT, load_hook

cc = load_hook("check_concurrency.py", "check_concurrency")


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# ── check_file ────────────────────────────────────────────────────────────────


def test_no_concurrency_block_is_clean(tmp_path):
    """Reusable/simple workflows without concurrency: are exempt."""
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    assert cc.check_file(path) == []


def test_concurrency_with_cancel_in_progress_true_is_clean(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: x-${{ github.ref }}\n  cancel-in-progress: true\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    assert cc.check_file(path) == []


def test_concurrency_with_cancel_in_progress_false_is_clean(tmp_path):
    """Explicit false is allowed — the point is it must be explicit."""
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\nconcurrency:\n"
        "  group: release\n  cancel-in-progress: false\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    assert cc.check_file(path) == []


def test_concurrency_with_expression_is_clean(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: x\n  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    assert cc.check_file(path) == []


def test_concurrency_without_cancel_in_progress_is_an_error(tmp_path):
    """Missing cancel-in-progress is the violation this check exists to catch."""
    path = _write(
        tmp_path,
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: x-${{ github.ref }}\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    result = cc.check_file(path)
    assert len(result) == 1
    line, message = result[0]
    assert line == 4  # the top-level concurrency: key line
    assert "cancel-in-progress" in message
    assert "silently defaults" in message


def test_job_level_concurrency_without_cancel_in_progress_is_an_error(tmp_path):
    """A JOB-level concurrency block that omits cancel-in-progress is flagged too —
    the workflow-level-only check used to let this fail open."""
    path = _write(
        tmp_path,
        "name: x\non:\n  pull_request:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    concurrency:\n"
        "      group: build-${{ github.ref }}\n"
        "    steps: []\n",
    )
    result = cc.check_file(path)
    assert len(result) == 1
    line, message = result[0]
    assert line == 7  # the job-level concurrency: key line
    assert "job 'build'" in message
    assert "cancel-in-progress" in message


def test_job_level_concurrency_with_cancel_in_progress_is_clean(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  pull_request:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    concurrency:\n"
        "      group: build-${{ github.ref }}\n"
        "      cancel-in-progress: true\n"
        "    steps: []\n",
    )
    assert cc.check_file(path) == []


def test_workflow_and_job_level_both_flagged(tmp_path):
    """Both a bare workflow-level block and a bare job-level block are reported."""
    path = _write(
        tmp_path,
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: wf-${{ github.ref }}\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    concurrency:\n"
        "      group: build-${{ github.ref }}\n"
        "    steps: []\n",
    )
    result = cc.check_file(path)
    assert sorted(line for line, _ in result) == [4, 9]


def test_malformed_yaml_is_reported_not_raised(tmp_path):
    """An unparseable workflow is reported as a violation (line None), never a
    traceback — matching the sibling YAML lints."""
    path = _write(tmp_path, "on: [pull_request\nconcurrency: {\n")
    result = cc.check_file(path)
    assert len(result) == 1
    line, message = result[0]
    assert line is None
    assert "could not parse as YAML" in message


def test_opt_out_comment_suppresses_the_error(tmp_path):
    path = _write(
        tmp_path,
        f"# {cc.OPT_OUT}\nname: x\non:\n  push:\nconcurrency:\n"
        "  group: x\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    assert cc.check_file(path) == []


def test_opt_out_token_in_string_value_does_not_suppress(tmp_path):
    """The opt-out only counts inside a real `#` comment — a group value that
    happens to contain the token must still be flagged (no byte-stream fail-open)."""
    path = _write(
        tmp_path,
        f"name: x\non:\n  pull_request:\nconcurrency:\n"
        f'  group: "{cc.OPT_OUT}"\n'
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    result = cc.check_file(path)
    assert len(result) == 1
    assert "cancel-in-progress" in result[0][1]


def test_non_dict_concurrency_is_ignored(tmp_path):
    """concurrency: somestring — unusual but not our problem."""
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\nconcurrency: my-group\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    assert cc.check_file(path) == []


# ── check_file: non-dict YAML ─────────────────────────────────────────────────


def test_non_dict_yaml_top_level_is_ignored(tmp_path):
    """A YAML file whose top-level element is a list (not a workflow dict) is exempt."""
    path = tmp_path / "list.yaml"
    path.write_text("- item1\n- item2\n")
    assert cc.check_file(path) == []


# ── main: violation path ──────────────────────────────────────────────────────


def test_main_reports_violation_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    """main() prints an error and returns 1 when a workflow omits cancel-in-progress."""
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: x-${{ github.ref }}\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n"
    )
    monkeypatch.setattr(cc, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(cc, "REPO_ROOT", tmp_path)
    rc = cc.main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "cancel-in-progress" in out
    assert "violation" in out


# ── main: repo-wide pass ──────────────────────────────────────────────────────


def test_own_ci_workflow_passes():
    """ci-truth-serum's own CI workflow sets cancel-in-progress explicitly, so the
    repo dogfoods its own lint. Scoped to the product's workflow (not the inherited
    template's, which template-sync may rewrite independently)."""
    ci = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
    assert cc.check_file(ci) == []
