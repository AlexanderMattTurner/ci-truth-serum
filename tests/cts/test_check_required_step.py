"""Tests for ci_truth_serum/check_required_step.py — the lint that refuses a job
that runs a script before the step that the script declares it needs.

Each test writes a scratch repository and drives `violations()` over real
workflow text, so the walk into scripts and composite actions runs for real.
"""

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

rs = load_hook("check_required_step.py", "check_required_step")

RESTORE = "./.github/actions/restore"
INSTALL = "scripts/install.sh"
DECLARING = f"#!/usr/bin/env bash\n# requires-step: {RESTORE}\npip install x\n"


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _workflow(*steps: str) -> str:
    """A one-job workflow whose first step sits on line 5."""
    body = "".join(f"      - {step}\n" for step in steps)
    return f"jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n{body}"


def _violations(root: Path, text: str) -> list[tuple[int, str]]:
    return rs.violations(text, root / ".github" / "workflows" / "ci.yaml")


def _lines(root: Path, text: str) -> list[int]:
    return [line for line, _message in _violations(root, text)]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(rs.uncached, "REPO_ROOT", tmp_path)
    _write(tmp_path, INSTALL, DECLARING)
    _write(tmp_path, ".github/actions/restore/action.yaml", "runs:\n  steps: []\n")
    return tmp_path


def test_a_step_with_no_earlier_required_step_fires_on_its_line(repo: Path) -> None:
    [(line, message)] = _violations(repo, _workflow(f"run: bash {INSTALL}"))
    assert line == 5
    assert f"runs {INSTALL}" in message
    assert f"- uses: {RESTORE}" in message


def test_an_earlier_required_step_passes(repo: Path) -> None:
    text = _workflow(f"uses: {RESTORE}", f"run: bash {INSTALL}")
    assert _lines(repo, text) == []


def test_a_required_step_after_the_script_does_not_count(repo: Path) -> None:
    text = _workflow(f"run: bash {INSTALL}", f"uses: {RESTORE}")
    assert _lines(repo, text) == [5]


def test_a_trailing_slash_on_the_uses_value_still_counts(repo: Path) -> None:
    text = _workflow(f"uses: {RESTORE}/", f"run: bash {INSTALL}")
    assert _lines(repo, text) == []


def test_a_trailing_slash_on_the_marker_still_counts(repo: Path) -> None:
    _write(repo, INSTALL, DECLARING.replace(RESTORE, RESTORE + "/"))
    assert _lines(repo, _workflow(f"uses: {RESTORE}", f"run: bash {INSTALL}")) == []
    assert _lines(repo, _workflow(f"run: bash {INSTALL}")) == [5]


def test_a_marker_in_the_run_body_declares_nothing(repo: Path) -> None:
    step = f"run: |\n          # requires-step: {RESTORE}\n          echo hi"
    assert _lines(repo, _workflow(step)) == []


def test_the_script_resolves_against_the_step_working_directory(repo: Path) -> None:
    step = "run: bash install.sh\n        working-directory: scripts"
    assert _lines(repo, _workflow(step)) == [5]
    assert _lines(repo, _workflow("run: bash install.sh")) == []


def test_the_script_resolves_against_the_job_default_directory(repo: Path) -> None:
    defaults = "    defaults:\n      run:\n        working-directory: scripts\n"
    text = _workflow("run: bash install.sh").replace(
        "    steps:\n", defaults + "    steps:\n"
    )
    assert _lines(repo, text) == [8]


def test_the_job_default_directory_does_not_reach_an_action_step(repo: Path) -> None:
    """`defaults.run` applies only to the workflow's own `run:` steps."""
    _write(
        repo,
        ".github/actions/tool/action.yaml",
        "runs:\n  steps:\n    - run: bash scripts/install.sh\n      shell: bash\n",
    )
    defaults = "    defaults:\n      run:\n        working-directory: elsewhere\n"
    text = _workflow("uses: ./.github/actions/tool").replace(
        "    steps:\n", defaults + "    steps:\n"
    )
    assert _lines(repo, text) == [8]


def test_a_required_step_in_another_job_does_not_count(repo: Path) -> None:
    other = "  restore:\n    runs-on: ubuntu-latest\n    steps:\n"
    text = (
        "jobs:\n"
        + other
        + f"      - uses: {RESTORE}\n"
        + _workflow(f"run: bash {INSTALL}").removeprefix("jobs:\n")
    )
    assert _lines(repo, text) == [9]


def test_a_script_that_runs_the_declaring_script_fires(repo: Path) -> None:
    _write(repo, "scripts/outer.sh", f'retry() {{ "$@"; }}\nretry bash {INSTALL}\n')
    assert _lines(repo, _workflow("run: bash scripts/outer.sh")) == [5]


def test_a_script_named_only_as_text_does_not_fire(repo: Path) -> None:
    assert _lines(repo, _workflow(f"run: grep pip {INSTALL}")) == []


@pytest.mark.parametrize(
    "script",
    [
        f'#!/usr/bin/env bash\necho "# requires-step: {RESTORE}"\n',
        f"#!/usr/bin/env bash\ncat <<'EOF'\n# requires-step: {RESTORE}\nEOF\n",
        "#!/usr/bin/env bash\n# requires-step: <reason>\n",
        "#!/usr/bin/env bash\n# requires-steps: ./x\n",
    ],
    ids=["printed-text", "heredoc-data", "placeholder", "longer-marker"],
)
def test_only_a_real_marker_comment_declares(repo: Path, script: str) -> None:
    _write(repo, INSTALL, script)
    assert _lines(repo, _workflow(f"run: bash {INSTALL}")) == []


def test_a_composite_action_step_fires_on_the_workflow_line(repo: Path) -> None:
    _write(
        repo,
        ".github/actions/tool/action.yaml",
        f"runs:\n  using: composite\n  steps:\n    - run: bash {INSTALL}\n      shell: bash\n",
    )
    assert _lines(repo, _workflow("run: true", "uses: ./.github/actions/tool")) == [6]


def test_an_action_yml_file_is_read_and_a_missing_action_is_not(repo: Path) -> None:
    _write(
        repo,
        ".github/actions/tool/action.yml",
        f"runs:\n  using: composite\n  steps:\n    - run: bash {INSTALL}\n      shell: bash\n",
    )
    assert _lines(repo, _workflow("uses: ./.github/actions/tool")) == [5]
    assert _lines(repo, _workflow("uses: ./.github/actions/missing")) == []


def test_a_composite_action_that_uses_the_step_first_passes(repo: Path) -> None:
    _write(
        repo,
        ".github/actions/tool/action.yaml",
        "runs:\n  using: composite\n  steps:\n"
        f"    - uses: {RESTORE}\n    - run: bash {INSTALL}\n      shell: bash\n",
    )
    assert _lines(repo, _workflow("uses: ./.github/actions/tool")) == []


def test_two_actions_that_use_each_other_end(repo: Path) -> None:
    for name, other in (("a", "b"), ("b", "a")):
        _write(
            repo,
            f".github/actions/{name}/action.yaml",
            f"runs:\n  steps:\n    - uses: ./.github/actions/{other}\n",
        )
    assert _lines(repo, _workflow("uses: ./.github/actions/a")) == []


@pytest.mark.parametrize(
    "text",
    ["", "- a\n", "jobs: []\n", "jobs:\n  build: 3\n", "jobs:\n  b:\n    steps: 3\n"],
)
def test_a_workflow_with_no_job_steps_has_no_finding(repo: Path, text: str) -> None:
    assert _lines(repo, text) == []


def test_an_unparseable_workflow_is_a_finding(repo: Path) -> None:
    [(line, message)] = _violations(repo, "jobs: [\n")
    assert line == 1
    assert message.startswith("could not parse this workflow or an action")


def test_an_unparseable_composite_action_is_a_finding(repo: Path) -> None:
    _write(repo, ".github/actions/tool/action.yaml", "runs: [\n")
    [(line, message)] = _violations(repo, _workflow("uses: ./.github/actions/tool"))
    assert line == 1
    assert message.startswith("could not parse this workflow or an action")


def test_an_unparseable_script_is_a_finding(repo: Path) -> None:
    pipes = "|" * (sys.modules["_cts_bash_ast"]._MAX_PIPE_BYTES + 1)
    [(line, message)] = _violations(repo, _workflow(f"run: echo '{pipes}'"))
    assert line == 1
    assert "pipe bytes" in message


# ── main ─────────────────────────────────────────────────────────────────────


def _point_at(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    monkeypatch.setattr(rs.uncached, "REPO_ROOT", root)
    monkeypatch.setattr(rs, "WORKFLOWS_DIR", workflows)
    return workflows


def test_main_reports_each_finding_and_exits_nonzero(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflows = _point_at(repo, monkeypatch)
    ci = workflows / "ci.yaml"
    ci.write_text(_workflow(f"uses: {RESTORE}", f"run: bash {INSTALL}"), "utf-8")
    assert rs.main() is None
    ci.write_text(_workflow(f"run: bash {INSTALL}"), "utf-8")
    with pytest.raises(SystemExit) as stop:
        rs.main()
    assert stop.value.code == 1
    assert "::error file=.github/workflows/ci.yaml,line=5::" in capsys.readouterr().out


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(max_size=300))
def test_fuzz_violations_reports_only_real_lines(repo: Path, text: str) -> None:
    """Fuzz: any text yields findings on lines that exist in it."""
    found = _violations(repo, text)
    assert all(1 <= line <= text.count("\n") + 1 for line, _message in found)
