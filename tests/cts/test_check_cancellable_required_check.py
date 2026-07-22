"""Tests for hooks/check_cancellable_required_check.py — the (opinionated)
pre-commit lint that forbids a static *cancellable* workflow-level concurrency
group on a workflow that declares a required check (via a `# required-check: true`
marker), which can strand the check at 'Expected — Waiting' when a sibling ref
cancels the run — and its workflow-level always() reporter — wholesale.

Distinct from check_static_concurrency: that lint keys off the decide+always()
*heuristic* shape and fires regardless of cancel-in-progress; this one keys off
the explicit required-check marker (catching required checks with no decide gate)
and fires only when the static group is also cancellable."""

from pathlib import Path

from tests._helpers import REPO_ROOT, load_hook

crc = load_hook(
    "check_cancellable_required_check.py", "check_cancellable_required_check"
)

# A workflow body that declares a required check via the mandatory marker, with no
# decide gate — the shape check_static_concurrency's heuristic misses.
REQUIRED_CHECK_JOBS = (
    "jobs:\n"
    "  work:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps: []\n"
    "  report: # required-check: true\n"
    "    if: always()\n"
    "    needs: [work]\n"
    "    runs-on: ubuntu-latest\n"
    "    steps: []\n"
)
# Same shape but the reporter is deliberately advisory — declares no required check.
ADVISORY_JOBS = REQUIRED_CHECK_JOBS.replace(
    "# required-check: true", "# required-check: false  # advisory only"
)


def _wf(concurrency: str, jobs: str = REQUIRED_CHECK_JOBS, header: str = "") -> str:
    return f"{header}name: x\non:\n  pull_request:\nconcurrency:\n{concurrency}" + jobs


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


STATIC_CANCELLABLE = "  group: my-static-lock\n  cancel-in-progress: true\n"
STATIC_NONCANCELLABLE = "  group: my-static-lock\n  cancel-in-progress: false\n"
PER_REF_CANCELLABLE = (
    "  group: x-${{ github.event.pull_request.number || github.ref }}\n"
    "  cancel-in-progress: true\n"
)
STATIC_EXPR = (
    "  group: my-static-lock\n"
    "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}\n"
)


# ── check_file: the violating combination ─────────────────────────────────────


def test_static_cancellable_on_required_check_is_an_error(tmp_path):
    result = crc.check_file(_write(tmp_path, _wf(STATIC_CANCELLABLE)))
    assert result is not None
    _line, message = result
    assert "static" in message
    assert "Expected — Waiting" in message


def test_static_cancellable_expression_is_flagged_conservatively(tmp_path):
    """An expression cancel-in-progress could evaluate true, so it is treated as
    cancellable rather than assumed safe."""
    result = crc.check_file(_write(tmp_path, _wf(STATIC_EXPR)))
    assert result is not None
    assert "static" in result[1]


# ── check_file: the safe combinations (false-positive guards) ─────────────────


def test_per_ref_cancellable_is_clean(tmp_path):
    """The repo's blessed pattern: a per-ref/per-PR group is only superseded by its
    own ref's newer run, which re-reports."""
    assert crc.check_file(_write(tmp_path, _wf(PER_REF_CANCELLABLE))) is None


def test_static_but_not_cancellable_is_clean(tmp_path):
    """cancel-in-progress: false queues instead of cancelling, so the reporter is
    never torn down — check_static_concurrency owns the static-group hazard here."""
    assert crc.check_file(_write(tmp_path, _wf(STATIC_NONCANCELLABLE))) is None


def test_static_cancellable_on_non_required_workflow_is_clean(tmp_path):
    """No `# required-check: true` marker → nothing to strand → not our concern."""
    assert (
        crc.check_file(_write(tmp_path, _wf(STATIC_CANCELLABLE, ADVISORY_JOBS))) is None
    )


def test_absent_cancel_in_progress_is_clean(tmp_path):
    """Omitted cancel-in-progress defaults to false (not cancel-on-supersede)."""
    body = _wf("  group: my-static-lock\n")
    assert crc.check_file(_write(tmp_path, body)) is None


# ── opt-out ───────────────────────────────────────────────────────────────────


def test_opt_out_comment_suppresses_the_error(tmp_path):
    body = _wf(STATIC_CANCELLABLE, header=f"# {crc.OPT_OUT}\n")
    assert crc.check_file(_write(tmp_path, body)) is None


def test_opt_out_token_in_string_value_does_not_suppress(tmp_path):
    """The opt-out counts only inside a real `#` comment, never a string value —
    matching it anywhere in the byte stream would be a fail-open."""
    body = _wf(f'  group: "{crc.OPT_OUT}"\n  cancel-in-progress: true\n')
    result = crc.check_file(_write(tmp_path, body))
    assert result is not None
    assert "static" in result[1]


# ── structural edge cases ─────────────────────────────────────────────────────


def test_malformed_yaml_is_reported_not_raised(tmp_path):
    result = crc.check_file(_write(tmp_path, "on: [pull_request\nconcurrency: {\n"))
    assert result is not None
    line, message = result
    assert line is None
    assert "could not parse as YAML" in message


def test_no_concurrency_block_is_clean(tmp_path):
    body = "name: x\non:\n  pull_request:\n" + REQUIRED_CHECK_JOBS
    assert crc.check_file(_write(tmp_path, body)) is None


def test_groupless_concurrency_is_clean(tmp_path):
    body = _wf("  cancel-in-progress: true\n")
    assert crc.check_file(_write(tmp_path, body)) is None


def test_non_dict_concurrency_is_ignored(tmp_path):
    body = (
        "name: x\non:\n  pull_request:\nconcurrency: my-group\n" + REQUIRED_CHECK_JOBS
    )
    assert crc.check_file(_write(tmp_path, body)) is None


def test_non_dict_yaml_top_level_is_ignored(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n")
    assert crc.check_file(path) is None


# ── _is_cancellable unit coverage ─────────────────────────────────────────────


def test_is_cancellable_truth_table():
    assert crc._is_cancellable(True) is True
    assert crc._is_cancellable(False) is False
    assert crc._is_cancellable(None) is False
    assert crc._is_cancellable("${{ github.event_name == 'pull_request' }}") is True
    assert crc._is_cancellable("false") is False
    assert crc._is_cancellable("FALSE") is False
    assert crc._is_cancellable("true") is True


def test_concurrency_line_returns_1_when_no_match():
    assert crc._concurrency_line("name: x\njobs: {}\n") == 1


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_reports_violation_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    _write(tmp_path, _wf(STATIC_CANCELLABLE), name="bad.yaml")
    monkeypatch.setattr(crc, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(crc, "REPO_ROOT", tmp_path)
    rc = crc.main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "static" in out
    assert "violation" in out


def test_all_shipped_workflows_pass(monkeypatch, capsys):
    """The repo dogfoods this lint: every required-check workflow uses a per-ref
    cancellable group (the blessed pattern), so none are flagged."""
    workflows = REPO_ROOT / ".github" / "workflows"
    monkeypatch.setattr(crc, "REPO_ROOT", REPO_ROOT)
    monkeypatch.setattr(crc, "WORKFLOWS_DIR", workflows)
    assert crc.main() == 0, capsys.readouterr().out
