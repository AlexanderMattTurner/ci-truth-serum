"""Tests for ci_truth_serum/check_moving_runner_alias.py — the lint that refuses a
GitHub-hosted runner alias (`ubuntu-latest` and its siblings) in a workflow or a
composite action.

Each test drives `violations()` over real workflow text, so each rule is checked
alone. Each test that asserts a span is skipped also holds one real alias, so a
check that found nothing at all cannot pass it. The `main()` tests run discovery
over a scratch repository.
"""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

mra = load_hook("check_moving_runner_alias.py", "check_moving_runner_alias")


def _workflow(job_body: str) -> str:
    """A one-job workflow whose job body starts on line 5."""
    return f"name: w\non: push\njobs:\n  build:\n{job_body}"


def _lines(text: str) -> list[int]:
    return [line for line, _message in mra.violations(text)]


_STEPS = "    steps:\n      - run: make\n"
_FIRING = _workflow("    runs-on: ubuntu-latest\n" + _STEPS)


# ── every place that picks a runner ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("runs_on", "line"),
    [
        ("    runs-on: ubuntu-latest\n", 5),
        ("    runs-on: [self-hosted, ubuntu-latest]\n", 5),
        ("    runs-on:\n      - ubuntu-latest\n", 6),
        ("    runs-on:\n      labels: ubuntu-latest\n", 6),
        ("    runs-on: ${{ vars.RUNNER || 'ubuntu-latest' }}\n", 5),
        (
            '    runs-on: ${{ fromJSON(\'["ubuntu-latest","blacksmith"]\')[0] }}\n',
            5,
        ),
    ],
)
def test_each_runs_on_form_fires(runs_on: str, line: int) -> None:
    assert _lines(_workflow(runs_on + _STEPS)) == [line]


def test_a_matrix_entry_fires_on_its_own_line() -> None:
    text = _workflow(
        "    strategy:\n      matrix:\n        os:\n"
        "          - ubuntu-24.04\n          - macos-latest\n"
        "    runs-on: ${{ matrix.os }}\n" + _STEPS
    )
    assert _lines(text) == [9]


def test_an_input_default_and_an_env_value_fire() -> None:
    text = (
        "name: w\non:\n  workflow_call:\n    inputs:\n      runner:\n"
        "        type: string\n        default: windows-latest\n"
        "env:\n  HOSTED: ubuntu-latest\n"
        "jobs:\n  build:\n    runs-on: ${{ inputs.runner }}\n" + _STEPS
    )
    assert _lines(text) == [7, 9]


@pytest.mark.parametrize(
    "label",
    [
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
        "ubuntu-latest-4-cores",
        "windows-latest-8-cores",
        "macos-latest-xlarge",
        "macos-latest-large",
        "Ubuntu-Latest",
    ],
)
def test_every_hosted_alias_fires(label: str) -> None:
    text = _workflow(f"    runs-on: {label}\n" + _STEPS)
    [(line, message)] = mra.violations(text)
    assert (line, f"`{label}`" in message) == (5, True)


@pytest.mark.parametrize(
    "label",
    [
        "ubuntu-24.04",
        "ubuntu-24.04-arm",
        "macos-15",
        "windows-2025",
        "blacksmith-2vcpu-ubuntu-2404",
        "acme-ubuntu-latest",
        "ubuntu-latest.example",
        "ubuntu-latestx",
        "self-hosted",
    ],
)
def test_a_named_release_or_a_vendor_label_passes(label: str) -> None:
    text = _workflow(f"    runs-on: {label}\n" + _STEPS)
    assert _lines(text) == []


def test_two_aliases_on_one_line_report_once() -> None:
    text = _workflow(
        "    runs-on: ${{ github.event_name == 'push' && 'ubuntu-latest' "
        "|| 'macos-latest' }}\n" + _STEPS
    )
    assert _lines(text) == [5]


def test_a_value_reached_through_a_yaml_alias_reports_once() -> None:
    text = (
        "name: w\non: push\njobs:\n"
        "  a:\n    runs-on: &hosted ubuntu-latest\n" + _STEPS + ""
        "  b:\n    runs-on: *hosted\n" + _STEPS
    )
    assert _lines(text) == [5]


def test_a_line_after_a_non_ascii_character_is_counted_in_characters() -> None:
    text = _workflow(
        "    name: build \u2014 caf\u00e9 \U0001f680\n    runs-on: ubuntu-latest\n"
        + _STEPS
    )
    assert _lines(text) == [6]


def test_a_composite_action_fires() -> None:
    text = (
        "name: a\ndescription: d\ninputs:\n  runner:\n    default: ubuntu-latest\n"
        "runs:\n  using: composite\n  steps:\n"
        "    - run: gh workflow run t.yaml -f os=macos-latest\n      shell: bash\n"
    )
    assert _lines(text) == [5, 9]


# ── text that picks no runner ───────────────────────────────────────────────


def test_a_yaml_comment_is_not_a_value() -> None:
    text = _workflow(
        "    # ubuntu-latest moved to 26.04, so this job names the release\n"
        "    runs-on: ubuntu-24.04  # was ubuntu-latest\n"
        "    container: macos-latest\n" + _STEPS
    )
    assert _lines(text) == [7]


def test_a_description_value_is_prose() -> None:
    text = (
        "name: w\non:\n  workflow_call:\n    inputs:\n      runner:\n"
        "        description: A label such as ubuntu-latest or ubuntu-24.04\n"
        "        type: string\n        default: ubuntu-latest\n"
        "jobs:\n  build:\n    runs-on: ${{ inputs.runner }}\n" + _STEPS
    )
    assert _lines(text) == [8]


@pytest.mark.parametrize(
    ("body", "line"),
    [
        # An input whose NAME is `description` holds a mapping, not prose.
        (
            "on:\n  workflow_dispatch:\n    inputs:\n      description:\n"
            "        description: the runner\n        default: ubuntu-latest\n"
            "jobs:\n  b:\n    runs-on: ${{ inputs.description }}\n",
            7,
        ),
        # A matrix axis named `description` holds a list.
        (
            "on: push\njobs:\n  b:\n    strategy:\n      matrix:\n"
            "        description: [macos-latest]\n"
            "    runs-on: ${{ matrix.description }}\n",
            7,
        ),
        # A matrix `include` entry is not an input, output or secret.
        (
            "on: push\njobs:\n  b:\n    strategy:\n      matrix:\n"
            "        include:\n          - description: windows-latest\n"
            "    runs-on: ${{ matrix.description }}\n",
            8,
        ),
    ],
)
def test_a_field_named_description_that_picks_a_runner_fires(
    body: str, line: int
) -> None:
    assert _lines("name: w\n" + body + _STEPS) == [line]


def test_an_action_description_is_prose() -> None:
    text = (
        "name: a\ndescription: Sets up ubuntu-latest tools\n"
        "outputs:\n  os:\n    description: e.g. macos-latest\n"
        "    value: windows-latest\n"
        "runs:\n  using: composite\n  steps:\n    - run: make\n      shell: bash\n"
    )
    assert _lines(text) == [6]


@pytest.mark.parametrize(
    ("value", "alias"),
    [
        ('"ubuntu\\u002dlatest"', "ubuntu-latest"),
        ('"macos-\\\n      latest"', "macos-latest"),
    ],
)
def test_an_alias_spelled_with_yaml_escapes_fires_on_the_scalar_line(
    value: str, alias: str
) -> None:
    [(line, message)] = mra.violations(_workflow(f"    runs-on: {value}\n" + _STEPS))
    assert (line, f"`{alias}`" in message) == (5, True)


def test_an_escaped_alias_inside_a_bash_comment_is_skipped() -> None:
    text = _workflow(
        "    runs-on: ubuntu-24.04\n    steps:\n"
        '      - run: "make  # ubuntu\\u002dlatest"\n'
        '      - run: "make os=windows\\u002dlatest"\n'
    )
    assert _lines(text) == [8]


def test_a_mapping_key_is_not_a_value() -> None:
    text = _workflow(
        "    env:\n      ubuntu-latest: x\n    runs-on: windows-latest\n" + _STEPS
    )
    assert _lines(text) == [7]


def test_a_shell_comment_in_a_bash_run_is_skipped_and_its_code_is_not() -> None:
    text = _workflow(
        "    runs-on: ubuntu-24.04\n    steps:\n      - run: |\n"
        "          # ubuntu-latest ships jq\n"
        '          echo "# not a comment: ubuntu-latest"\n'
        "          gh workflow run t.yaml -f os=windows-latest  # macos-latest\n"
    )
    assert _lines(text) == [9, 10]


def test_a_quoted_run_value_keeps_its_comment_offsets() -> None:
    text = _workflow(
        "    runs-on: ubuntu-24.04\n    steps:\n"
        "      - run: 'make  # ubuntu-latest'\n"
        '      - run: "make os=ubuntu-latest"\n'
    )
    assert _lines(text) == [8]


def test_a_run_in_a_shell_bash_cannot_read_is_scanned_whole() -> None:
    # The bash grammar cannot say where a PowerShell comment starts, so the
    # check reads the whole script. A finding there is the safe direction.
    text = _workflow(
        "    runs-on: windows-2025\n    steps:\n      - shell: pwsh\n"
        "        run: |\n          # windows-latest ships pwsh 7\n"
    )
    assert _lines(text) == [9]


# ── the opt-out ─────────────────────────────────────────────────────────────


def test_a_reasoned_marker_on_the_line_exempts() -> None:
    text = _FIRING.replace(
        "ubuntu-latest\n",
        "ubuntu-latest  # allow-moving-runner-alias: this job tests the alias\n",
    )
    assert _lines(text) == []


def test_a_reasoned_marker_in_the_comment_block_above_exempts() -> None:
    text = _FIRING.replace(
        "    runs-on:",
        "    # allow-moving-runner-alias: this job tests the alias\n    runs-on:",
    )
    assert _lines(text) == []


def test_a_marker_in_a_bash_run_comment_exempts_its_line() -> None:
    text = _workflow(
        "    runs-on: ubuntu-24.04\n    steps:\n      - run: |\n"
        "          gh workflow run t.yaml -f os=macos-latest\n"
        "          gh workflow run t.yaml -f os=ubuntu-latest"
        "  # allow-moving-runner-alias: probe the alias\n"
    )
    assert _lines(text) == [8]


def test_a_marker_without_a_reason_does_not_exempt() -> None:
    text = _FIRING.replace(
        "ubuntu-latest\n", "ubuntu-latest  # allow-moving-runner-alias:\n"
    )
    assert _lines(text) == [5]


def test_a_marker_inside_a_quoted_value_does_not_exempt() -> None:
    text = _FIRING.replace(
        "    runs-on:",
        '    name: "x  # allow-moving-runner-alias: not a comment"\n    runs-on:',
    )
    assert _lines(text) == [6]


def test_the_message_names_the_opt_out_and_a_release() -> None:
    [(_line, message)] = mra.violations(_FIRING)
    assert "# allow-moving-runner-alias: <reason>" in message
    assert "`ubuntu-24.04`" in message


# ── input the check cannot read ─────────────────────────────────────────────


def test_unparseable_yaml_is_a_finding() -> None:
    [(line, message)] = mra.violations("jobs: [unclosed\n")
    assert (line, message.startswith("could not parse as YAML")) == (1, True)


@pytest.mark.parametrize("text", ["", "# only a comment\n", "just a scalar\n"])
def test_a_document_with_no_values_to_flag_is_clean(text: str) -> None:
    assert _lines(text) == []


# ── main ────────────────────────────────────────────────────────────────────


def _point_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    wf = tmp_path / ".github" / "workflows"
    actions = tmp_path / ".github" / "actions"
    wf.mkdir(parents=True)
    monkeypatch.setattr(mra, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mra, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(mra, "ACTIONS_DIR", actions)
    return wf, actions


def test_repo_root_is_the_current_working_directory() -> None:
    assert mra.REPO_ROOT == Path.cwd()
    assert mra.WORKFLOWS_DIR == Path.cwd() / ".github" / "workflows"
    assert mra.ACTIONS_DIR == Path.cwd() / ".github" / "actions"


def test_main_reports_workflows_and_actions_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wf, actions = _point_at(tmp_path, monkeypatch)
    (wf / "bad.yml").write_text(_FIRING, encoding="utf-8")
    (actions / "setup").mkdir(parents=True)
    (actions / "setup" / "action.yaml").write_text(
        "name: s\nruns:\n  using: composite\n  steps:\n"
        "    - run: echo macos-latest\n      shell: bash\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as stop:
        mra.main()
    assert stop.value.code == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yml,line=5::" in out
    assert "::error file=.github/actions/setup/action.yaml,line=5::" in out
    assert "2 moving runner alias(es) found" in out


def test_main_passes_a_clean_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf, _actions = _point_at(tmp_path, monkeypatch)
    (wf / "ok.yaml").write_text(
        _workflow("    runs-on: ubuntu-24.04\n" + _STEPS), encoding="utf-8"
    )
    assert mra.main() is None


@given(st.text(max_size=300))
def test_fuzz_violations_returns_line_findings(text: str) -> None:
    """Fuzz: any text yields findings on real lines, and never raises."""
    found = mra.violations(text)
    assert all(1 <= line <= text.count("\n") + 1 for line, _message in found)
