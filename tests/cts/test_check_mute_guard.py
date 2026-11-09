"""Tests for ci_truth_serum/check_mute_guard.py — the lint that requires every job with a bash
`run:` step to start with the mute-failure-guard action, so a failed step leaves a ledger.

Drives check_file(path) for each rule, and main() for the workflow selection."""

from pathlib import Path

import pytest

from tests._helpers import load_hook

mg = load_hook("check_mute_guard.py", "check_mute_guard")

GUARD = "AlexanderMattTurner/ci-truth-serum/actions/mute-failure-guard@0123456789abcdef0123456789abcdef01234567"


def _write(tmp_path: Path, jobs: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(f"name: x\non:\n  push:\njobs:\n{jobs}", encoding="utf-8")
    return path


def _lines(found):
    return [line for line, _ in found]


@pytest.mark.parametrize(
    "jobs",
    [
        pytest.param(
            f"  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {GUARD}\n"
            "      - uses: actions/checkout@v4\n      - run: make\n",
            id="guard-first",
        ),
        pytest.param(
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n",
            id="no-run-step",
        ),
        pytest.param(
            "  gate:\n    uses: ./.github/workflows/reusable.yaml\n",
            id="reusable-call",
        ),
        pytest.param(
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: Get-Date\n        shell: pwsh\n",
            id="pwsh-only",
        ),
        pytest.param(
            "  build:\n    runs-on: windows-latest\n    steps:\n      - run: Get-Date\n",
            id="windows-default-shell",
        ),
        pytest.param(
            "  build:  # allow-unguarded-job: kcov owns BASH_ENV in this job\n"
            "    runs-on: ubuntu-latest\n    steps:\n      - run: make\n",
            id="opt-out-with-reason",
        ),
        pytest.param(
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n"
            "          # allow-unguarded-job: the coverage tool replaces BASH_ENV\n          make\n",
            id="opt-out-in-run-comment",
        ),
    ],
)
def test_a_guarded_or_exempt_job_is_clean(tmp_path, jobs):
    assert mg.check_file(_write(tmp_path, jobs)) == []


@pytest.mark.parametrize(
    ("jobs", "line"),
    [
        pytest.param(
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n",
            5,
            id="no-guard",
        ),
        pytest.param(
            f"  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n      - uses: {GUARD}\n",
            5,
            id="guard-after-the-first-run",
        ),
        pytest.param(
            f"  build:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {GUARD}\n"
            "        if: github.event_name == 'push'\n      - run: make\n",
            5,
            id="guard-that-can-be-skipped",
        ),
        pytest.param(
            "  build:\n    runs-on: ubuntu-latest\n    defaults:\n      run:\n        shell: bash\n"
            "    steps:\n      - run: make\n",
            5,
            id="explicit-bash-default",
        ),
        pytest.param(
            "  build:  # allow-unguarded-job:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n",
            5,
            id="opt-out-without-reason",
        ),
        pytest.param(
            '  build:\n    name: "x  # allow-unguarded-job: not a comment"\n    runs-on: ubuntu-latest\n'
            "    steps:\n      - run: make\n",
            5,
            id="marker-inside-a-string-value",
        ),
        pytest.param(
            "  build:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: someone/mute-failure-guard-lookalike@v1\n      - run: make\n",
            5,
            id="lookalike-action",
        ),
    ],
)
def test_an_unguarded_job_is_flagged_on_its_key_line(tmp_path, jobs, line):
    assert _lines(mg.check_file(_write(tmp_path, jobs))) == [line]


def test_a_vendored_copy_counts_when_the_consumer_names_it(tmp_path):
    jobs = (
        "  build:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: ./.github/actions/mute-failure-guard\n      - run: make\n"
    )
    path = _write(tmp_path, jobs)
    assert _lines(mg.check_file(path)) == [5]
    assert mg.check_file(path, actions=("./.github/actions/mute-failure-guard",)) == []


def test_a_file_that_is_not_yaml_is_a_finding(tmp_path):
    path = tmp_path / "wf.yaml"
    path.write_text("jobs: [\n", encoding="utf-8")
    assert _lines(mg.check_file(path)) == [None]


def test_main_checks_only_the_workflows_the_consumer_requires(
    tmp_path, monkeypatch, capsys
):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    unguarded = "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n"
    _write(workflows, unguarded, "vm-live.yaml")
    _write(workflows, unguarded, "lint.yaml")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as stopped:
        mg.main(["--only", "vm-live*.yaml"])
    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert "file=.github/workflows/vm-live.yaml,line=5" in out
    assert "lint.yaml" not in out

    mg.main(["--only", "nothing-matches-*.yaml", "--only", "lint-*.yaml"])
    assert "::error" not in capsys.readouterr().out


def test_main_checks_every_workflow_when_the_consumer_names_none(
    tmp_path, monkeypatch, capsys
):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    _write(
        workflows,
        "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n",
        "a.yaml",
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        mg.main([])
    assert "file=.github/workflows/a.yaml,line=5" in capsys.readouterr().out
