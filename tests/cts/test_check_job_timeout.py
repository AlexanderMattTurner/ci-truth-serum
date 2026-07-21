"""Tests for hooks/check_job_timeout.py — the (opinionated) pre-commit lint that
requires every GitHub Actions job to declare timeout-minutes, so no job silently
inherits GitHub's 360-minute default.

Drives check_file(path) directly so each rule is asserted in isolation."""

from pathlib import Path

from tests._helpers import REPO_ROOT, load_hook

jt = load_hook("check_job_timeout.py", "check_job_timeout")


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# ── clean ────────────────────────────────────────────────────────────────────


def test_job_with_timeout_is_clean(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    timeout-minutes: 10\n    steps: []\n",
    )
    assert jt.check_file(path) == []


def test_reusable_workflow_call_job_is_exempt(tmp_path):
    """A `uses:` job cannot carry timeout-minutes, so it is not required to."""
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n"
        "  gate:\n    uses: ./.github/workflows/decide-reusable.yaml\n",
    )
    assert jt.check_file(path) == []


def test_no_jobs_is_clean(tmp_path):
    path = _write(tmp_path, "name: x\non:\n  push:\n")
    assert jt.check_file(path) == []


# ── violation ────────────────────────────────────────────────────────────────


def test_job_without_timeout_is_flagged(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    result = jt.check_file(path)
    assert len(result) == 1
    line, message = result[0]
    assert line == 5  # the `build:` job key line
    assert "timeout-minutes" in message
    assert "build" in message


def test_multiple_jobs_each_flagged(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n"
        "  a:\n    runs-on: ubuntu-latest\n    steps: []\n"
        "  b:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps: []\n"
        "  c:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    result = jt.check_file(path)
    names = sorted(msg.split("'")[1] for _, msg in result)
    assert names == ["a", "c"]  # b has a timeout


# ── opt-out (reason required) ────────────────────────────────────────────────


def test_optout_with_reason_on_key_line_suppresses(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n"
        "  watcher:  # allow-no-timeout: long-lived poll loop, must not be killed\n"
        "    runs-on: ubuntu-latest\n    steps: []\n",
    )
    assert jt.check_file(path) == []


def test_optout_with_reason_on_body_line_suppresses(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n"
        "  watcher:\n    runs-on: ubuntu-latest\n"
        "    # allow-no-timeout: deliberately unbounded watcher\n    steps: []\n",
    )
    assert jt.check_file(path) == []


def test_reasonless_optout_does_not_suppress(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n"
        "  build:  # allow-no-timeout:\n    runs-on: ubuntu-latest\n    steps: []\n",
    )
    result = jt.check_file(path)
    assert len(result) == 1


def test_optout_token_in_string_value_does_not_suppress(tmp_path):
    """The token must be inside a real `#` comment, not a string value."""
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n"
        "  build:\n    runs-on: ubuntu-latest\n"
        '    env:\n      NOTE: "allow-no-timeout: not a comment"\n    steps: []\n',
    )
    result = jt.check_file(path)
    assert len(result) == 1


# ── malformed / non-dict ─────────────────────────────────────────────────────


def test_malformed_yaml_is_reported_not_raised(tmp_path):
    path = _write(tmp_path, "jobs: {\n  build:\n")
    result = jt.check_file(path)
    assert len(result) == 1
    line, message = result[0]
    assert line is None
    assert "could not parse as YAML" in message


def test_non_dict_yaml_is_ignored(tmp_path):
    path = _write(tmp_path, "- a\n- b\n", name="list.yaml")
    assert jt.check_file(path) == []


# ── main wiring ──────────────────────────────────────────────────────────────


def test_main_reports_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: x\non:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n"
    )
    monkeypatch.setattr(jt, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(jt, "REPO_ROOT", tmp_path)
    assert jt.main() == 1
    out = capsys.readouterr().out
    assert "timeout-minutes" in out
    assert "violation" in out


def test_main_clean_dir_returns_zero(tmp_path, monkeypatch):
    (tmp_path / "ok.yaml").write_text(
        "name: x\non:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    timeout-minutes: 5\n    steps: []\n"
    )
    monkeypatch.setattr(jt, "WORKFLOWS_DIR", tmp_path)
    monkeypatch.setattr(jt, "REPO_ROOT", tmp_path)
    assert jt.main() == 0


# ── dogfood: the repo's own workflows all set timeout-minutes ────────────────


def test_own_workflows_all_declare_timeouts():
    wf_dir = REPO_ROOT / ".github" / "workflows"
    offenders = []
    for path in sorted(wf_dir.glob("*.yaml")):
        offenders += [f"{path.name}: {msg}" for _, msg in jt.check_file(path)]
    assert offenders == [], offenders
