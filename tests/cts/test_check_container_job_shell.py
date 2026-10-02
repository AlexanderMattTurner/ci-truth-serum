"""Tests for ci_truth_serum/check_container_job_shell.py — the lint that makes every
`run:` step in a `container:` job name the shell it runs under.

Each test drives `violations()` over real workflow text, so each rule is checked
alone. The `main()` tests run discovery over a scratch repository.
"""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

cjs = load_hook("check_container_job_shell.py", "check_container_job_shell")

_CONTAINER = "    runs-on: ubuntu-latest\n    container: ubuntu:26.04\n"


def _workflow(job_body: str, *, top: str = "") -> str:
    """A one-job workflow whose `build:` key sits on line 5 when TOP is empty."""
    return f"name: w\non: push\n{top}jobs:\n  build:\n{job_body}"


def _lines(text: str) -> list[int]:
    return [line for line, _message in cjs.violations(text)]


# ── the rule ─────────────────────────────────────────────────────────────────


def test_undeclared_run_step_in_a_container_job_fires() -> None:
    text = _workflow(_CONTAINER + "    steps:\n      - run: echo hi\n")
    assert _lines(text) == [8]


def test_two_undeclared_steps_report_separately() -> None:
    text = _workflow(
        _CONTAINER + "    steps:\n      - run: echo a\n      - run: echo b\n"
    )
    assert _lines(text) == [8, 9]


def test_the_finding_points_at_the_run_key_not_the_step_start() -> None:
    text = _workflow(
        _CONTAINER + "    steps:\n      - name: build\n        run: make\n"
    )
    assert _lines(text) == [9]


def test_a_mapping_container_is_in_scope() -> None:
    text = _workflow(
        "    runs-on: ubuntu-latest\n    container:\n      image: alpine:3\n"
        "    steps:\n      - run: echo hi\n"
    )
    assert _lines(text) == [9]


@pytest.mark.parametrize(
    "scope",
    [
        "    defaults:\n      run:\n        shell: sh\n",
        "    defaults:\n      run:\n        shell: bash\n",
    ],
)
def test_job_level_defaults_satisfy_every_step(scope: str) -> None:
    text = _workflow(
        _CONTAINER + scope + "    steps:\n      - run: echo a\n      - run: echo b\n"
    )
    assert _lines(text) == []


def test_workflow_level_defaults_satisfy_every_job() -> None:
    text = _workflow(
        _CONTAINER + "    steps:\n      - run: echo a\n",
        top="defaults:\n  run:\n    shell: bash\n",
    )
    assert _lines(text) == []


def test_step_level_shell_satisfies_only_that_step() -> None:
    text = _workflow(
        _CONTAINER
        + "    steps:\n      - run: echo a\n        shell: bash\n      - run: echo b\n"
    )
    assert _lines(text) == [10]


def test_a_null_shell_declares_nothing() -> None:
    # `shell:` with no value is YAML null, and GitHub then picks its default.
    text = _workflow(
        _CONTAINER
        + "    defaults:\n      run:\n        shell:\n"
        + "    steps:\n      - run: echo a\n        shell:\n"
    )
    assert _lines(text) == [11]


def test_job_without_a_container_is_out_of_scope() -> None:
    text = _workflow("    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n")
    assert _lines(text) == []


def test_a_null_container_is_out_of_scope() -> None:
    text = _workflow(
        "    runs-on: ubuntu-latest\n    container:\n    steps:\n      - run: echo hi\n"
    )
    assert _lines(text) == []


@pytest.mark.parametrize(
    "body",
    [
        _CONTAINER,
        _CONTAINER + "    steps:\n      - uses: actions/checkout@v5\n",
    ],
    ids=["no-steps", "uses-only"],
)
def test_a_container_job_without_run_steps_reports_nothing(body: str) -> None:
    assert _lines(_workflow(body)) == []


def test_file_without_jobs_is_ignored() -> None:
    assert _lines("name: w\non: push\n") == []


def test_unparseable_yaml_is_itself_a_finding() -> None:
    found = cjs.violations("jobs:\n  build:\n    steps: [\n")
    assert len(found) == 1
    assert "could not parse as YAML" in found[0][1]


# ── the opt-out ──────────────────────────────────────────────────────────────

_FIRING = _workflow(_CONTAINER + "    steps:\n      - run: echo hi\n")


def test_annotation_on_the_job_key_line_exempts_it() -> None:
    text = _FIRING.replace(
        "  build:\n", "  build:  # shell-default-ok: wants whatever GitHub picks\n"
    )
    assert _lines(text) == []


def test_annotation_in_the_comment_block_above_exempts_it() -> None:
    text = _FIRING.replace(
        "  build:\n",
        "  # takes GitHub's own default on purpose\n"
        "  # shell-default-ok: the image's own sh is the tested surface\n"
        "  build:\n",
    )
    assert _lines(text) == []


def test_annotation_without_a_reason_does_not_exempt() -> None:
    text = _FIRING.replace("  build:\n", "  build:  # shell-default-ok:\n")
    assert _lines(text) == [8]


def test_annotation_above_a_non_comment_line_does_not_reach() -> None:
    text = (
        "name: w\non: push\njobs:\n"
        "  # shell-default-ok: this reason belongs to first\n"
        "  first:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo a\n"
        "  build:\n" + _CONTAINER + "    steps:\n      - run: echo hi\n"
    )
    assert _lines(text) == [13]


def test_a_marker_inside_a_quoted_value_does_not_exempt() -> None:
    # A `#` inside a YAML string is content, not a comment.
    text = _FIRING.replace(
        "  build:\n", '  build:\n    name: "x  # shell-default-ok: not a comment"\n'
    )
    assert _lines(text) == [9]


def test_the_message_names_the_opt_out() -> None:
    (_line, message), *_ = cjs.violations(_FIRING)
    assert "# shell-default-ok: <reason>" in message
    assert "shell: sh" in message


# ── probes: text a step prints or writes is not a key ───────────────────────


def test_a_shell_named_in_a_logger_message_declares_nothing() -> None:
    text = _workflow(
        _CONTAINER + '    steps:\n      - run: |\n          echo "use shell: bash"\n'
    )
    assert _lines(text) == [8]


def test_keys_in_a_heredoc_body_are_data() -> None:
    # The first job writes `container:` into a file, so it stays on the runner.
    # The second writes `shell: sh` into a file, so it still declares nothing.
    heredoc = (
        "      - run: |\n"
        "          cat <<'EOF' > ci.yaml\n"
        "          container: ubuntu:26.04\n"
        "          defaults:\n            run:\n              shell: sh\n"
        "          EOF\n"
    )
    runner = _workflow("    runs-on: ubuntu-latest\n    steps:\n" + heredoc)
    assert _lines(runner) == []
    assert _lines(_workflow(_CONTAINER + "    steps:\n" + heredoc)) == [8]


# ── non-vacuity: each rule changes the verdict ───────────────────────────────

_JOB_DEFAULTS = "    defaults:\n      run:\n        shell: sh\n"


@pytest.mark.parametrize(
    ("kept", "dropped"),
    [
        (_FIRING, _FIRING.replace("    container: ubuntu:26.04\n", "")),
        (_FIRING, _FIRING.replace("    steps:", _JOB_DEFAULTS + "    steps:")),
        (_FIRING, _FIRING.replace("jobs:", "defaults:\n  run:\n    shell: sh\njobs:")),
        (_FIRING, _FIRING.replace("echo hi\n", "echo hi\n        shell: sh\n")),
        (_FIRING, _FIRING.replace("  build:", "  build:  # shell-default-ok: why")),
    ],
    ids=["container", "job-defaults", "workflow-defaults", "step-shell", "opt-out"],
)
def test_each_rule_changes_the_verdict(kept: str, dropped: str) -> None:
    assert kept != dropped
    assert _lines(kept) == [8]
    assert _lines(dropped) == []


# ── main ─────────────────────────────────────────────────────────────────────


def _point_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    monkeypatch.setattr(cjs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cjs, "WORKFLOWS_DIR", wf)
    return wf


def test_repo_root_is_the_current_working_directory() -> None:
    assert cjs.REPO_ROOT == Path.cwd()
    assert cjs.WORKFLOWS_DIR == Path.cwd() / ".github" / "workflows"


def test_main_reports_each_finding_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wf = _point_at(tmp_path, monkeypatch)
    (wf / "bad.yml").write_text(_FIRING, encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        cjs.main()
    assert stop.value.code == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yml,line=8::" in out
    assert "shell-default-ok" in out


def test_main_passes_a_clean_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf = _point_at(tmp_path, monkeypatch)
    (wf / "ok.yaml").write_text(
        _workflow(_CONTAINER + "    steps:\n      - run: echo hi\n        shell: sh\n"),
        encoding="utf-8",
    )
    assert cjs.main() is None


@given(st.text(max_size=300))
def test_fuzz_violations_returns_line_findings_or_a_known_error(text) -> None:
    """Fuzz: any text yields findings on real lines, or one of the declared errors."""
    try:
        found = cjs.violations(text)
    except Exception as err:
        raise AssertionError(f"unexpected {type(err).__name__}: {err}") from err
    lines = [item[0] if isinstance(item, tuple) else item for item in found]
    assert all(1 <= line <= text.count("\n") + 1 for line in lines)
