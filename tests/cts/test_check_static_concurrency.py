"""Tests for hooks/check_static_concurrency.py — the (opinionated) pre-commit
lint that forbids a static workflow-level concurrency lock (a group with no
per-ref/PR key) on a workflow that backs a required check (decide gate +
always() reporter), which can strand the check at 'Expected — Waiting' when a
sibling ref cancels a pending run wholesale."""

from pathlib import Path

from tests._helpers import REPO_ROOT, load_hook

sc = load_hook("check_static_concurrency.py", "check_static_concurrency")

# A workflow that backs a required check: decide gate + an always() reporter.
REQUIRED_CHECK_JOBS = (
    "jobs:\n"
    "  decide:\n"
    "    uses: ./.github/workflows/decide-reusable.yaml\n"
    "  work:\n"
    "    needs: decide\n"
    "    if: needs.decide.outputs.run == 'true'\n"
    "    runs-on: ubuntu-latest\n"
    "    steps: []\n"
    "  report:\n"
    "    needs: [decide, work]\n"
    "    if: always()\n"
    "    runs-on: ubuntu-latest\n"
    "    steps: []\n"
)
PLAIN_JOBS = "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n"


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# ── check_file ────────────────────────────────────────────────────────────────


def test_static_group_on_required_check_is_an_error(tmp_path):
    """A static workflow-level group on a decide+always() workflow can hang the
    required check at 'Expected — Waiting'."""
    body = (
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: my-static-lock\n  cancel-in-progress: false\n" + REQUIRED_CHECK_JOBS
    )
    result = sc.check_file(_write(tmp_path, body))
    assert result is not None
    _line, message = result
    assert "static" in message
    assert "Expected — Waiting" in message


def test_per_ref_group_on_required_check_is_clean(tmp_path):
    """A per-ref group on the same required-check shape is safe — a run is only
    superseded by a newer run of the same ref, which reports."""
    body = (
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: x-${{ github.head_ref || github.ref }}\n  cancel-in-progress: true\n"
        + REQUIRED_CHECK_JOBS
    )
    assert sc.check_file(_write(tmp_path, body)) is None


def test_static_group_without_reporter_shape_is_clean(tmp_path):
    """A globally-serialized workflow that is NOT a required check (no decide gate
    + always() reporter) keeps its static lock — e.g. release/tag workflows."""
    body = (
        "name: x\non:\n  push:\nconcurrency:\n"
        "  group: tag-release\n  cancel-in-progress: false\n" + PLAIN_JOBS
    )
    assert sc.check_file(_write(tmp_path, body)) is None


def test_static_group_with_only_decide_gate_is_clean(tmp_path):
    """Decide gate but no always() reporter → not the required-check shape."""
    jobs = (
        "jobs:\n"
        "  decide:\n"
        "    uses: ./.github/workflows/decide-reusable.yaml\n"
        "  work:\n"
        "    needs: decide\n"
        "    if: needs.decide.outputs.run == 'true'\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: []\n"
    )
    body = (
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: my-static-lock\n  cancel-in-progress: false\n" + jobs
    )
    assert sc.check_file(_write(tmp_path, body)) is None


def test_opt_out_comment_suppresses_the_error(tmp_path):
    body = (
        f"# {sc.OPT_OUT}\nname: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: my-static-lock\n  cancel-in-progress: false\n" + REQUIRED_CHECK_JOBS
    )
    assert sc.check_file(_write(tmp_path, body)) is None


def test_groupless_concurrency_is_not_flagged(tmp_path):
    """A map-form concurrency block with no group (unusual, but the absent group
    must not be mislabeled 'static') on a required-check workflow is clean."""
    body = (
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  cancel-in-progress: true\n" + REQUIRED_CHECK_JOBS
    )
    assert sc.check_file(_write(tmp_path, body)) is None


def test_no_concurrency_block_is_clean(tmp_path):
    body = "name: x\non:\n  pull_request:\n" + REQUIRED_CHECK_JOBS
    assert sc.check_file(_write(tmp_path, body)) is None


def test_non_dict_concurrency_is_ignored(tmp_path):
    """concurrency: somestring — unusual but not our problem."""
    body = "name: x\non:\n  push:\nconcurrency: my-group\n" + REQUIRED_CHECK_JOBS
    assert sc.check_file(_write(tmp_path, body)) is None


def test_non_mapping_jobs_is_ignored(tmp_path):
    """jobs: scalar → not a dict → can't be a required-check shape."""
    body = (
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: my-static-lock\njobs: scalar-not-a-mapping\n"
    )
    assert sc.check_file(_write(tmp_path, body)) is None


def test_non_dict_yaml_top_level_is_ignored(tmp_path):
    """A YAML file whose top-level element is a list (not a workflow dict) is exempt."""
    path = tmp_path / "list.yaml"
    path.write_text("- item1\n- item2\n")
    assert sc.check_file(path) is None


# ── _concurrency_line fallback ────────────────────────────────────────────────


def test_concurrency_line_returns_1_when_no_match():
    """Text with no top-level concurrency: key falls back to line 1."""
    assert sc._concurrency_line("name: x\njobs: {}\n") == 1


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_reports_violation_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: x\non:\n  pull_request:\nconcurrency:\n"
        "  group: my-static-lock\n  cancel-in-progress: false\n" + REQUIRED_CHECK_JOBS
    )
    monkeypatch.setattr(sc, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(sc, "REPO_ROOT", tmp_path)
    rc = sc.main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "static" in out
    assert "violation" in out


def test_all_shipped_workflows_pass(monkeypatch, capsys):
    """The repo dogfoods this lint: its static-group workflows (template-sync,
    tag-release) back no required check, so none are flagged."""
    workflows = REPO_ROOT / ".github" / "workflows"
    monkeypatch.setattr(sc, "REPO_ROOT", REPO_ROOT)
    monkeypatch.setattr(sc, "WORKFLOWS_DIR", workflows)
    assert sc.main() == 0, capsys.readouterr().out
