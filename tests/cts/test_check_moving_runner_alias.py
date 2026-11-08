"""Tests for ci_truth_serum/check_moving_runner_alias.py — the lint that refuses a
GitHub-hosted runner alias (`ubuntu-latest` and its siblings) in a workflow or a
composite action.

Each test drives `violations()` over real workflow text. A test that asserts a
value is skipped also holds one real alias, so a check that found nothing at
all cannot pass it.
"""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

mra = load_hook("check_moving_runner_alias.py", "check_moving_runner_alias")

_STEPS = "    steps:\n      - run: make\n"


def _workflow(job_body: str) -> str:
    """A one-job workflow whose job body starts on line 5."""
    return f"name: w\non: push\njobs:\n  build:\n{job_body}"


def _lines(text: str) -> list[int]:
    return [line for line, _message in mra.violations(text)]


_FIRING = _workflow("    runs-on: ubuntu-latest\n" + _STEPS)


@pytest.mark.parametrize(
    ("body", "line"),
    [
        ("    runs-on: ubuntu-latest\n", 5),
        ("    runs-on: [self-hosted, macos-latest]\n", 5),
        ("    runs-on:\n      labels: windows-latest\n", 6),
        ("    runs-on: ${{ vars.RUNNER || 'ubuntu-latest' }}\n", 5),
        ("    strategy:\n      matrix:\n        os: [ubuntu-24.04, macos-latest]\n", 7),
        ("    env:\n      HOSTED: ubuntu-latest-4-cores\n", 6),
        # A YAML escape decodes to the alias GitHub receives.
        ('    runs-on: "ubuntu\\u002dlatest"\n', 5),
        # A block scalar reports on the line where it starts.
        (
            "    steps:\n      - run: |\n          make\n          echo macos-latest\n",
            6,
        ),
    ],
)
def test_each_place_that_names_an_alias_fires(body: str, line: int) -> None:
    assert _lines(_workflow(body + _STEPS)) == [line]


@pytest.mark.parametrize(
    "label",
    [
        "ubuntu-24.04",
        "macos-15",
        "windows-2025",
        "blacksmith-2vcpu-ubuntu-2404",
        "acme-ubuntu-latest",
        "ubuntu-latest.example",
    ],
)
def test_a_named_release_or_a_vendor_label_passes(label: str) -> None:
    assert _lines(_workflow(f"    runs-on: {label}\n" + _STEPS)) == []


def test_keys_comments_and_description_text_are_skipped() -> None:
    text = (
        "name: w\non:\n  workflow_call:\n    inputs:\n      os:\n"
        "        description: e.g. ubuntu-latest\n"
        "        default: macos-latest\n"
        "jobs:\n  b:\n    # ubuntu-latest moved to 26.04\n"
        "    env:\n      ubuntu-latest: x\n"
        "    runs-on: ${{ inputs.os }}\n" + _STEPS
    )
    assert _lines(text) == [7]


@pytest.mark.parametrize(
    ("text", "line"),
    [
        (
            _workflow(
                "    strategy:\n      matrix:\n        description: [windows-latest]\n"
            ),
            7,
        ),
        (_workflow("    description: windows-latest\n"), 5),
        # An action input's description is display text, and its default is data.
        (
            "name: a\ninputs:\n  os:\n    description: ubuntu-latest\n    default: macos-latest\n",
            5,
        ),
    ],
)
def test_only_display_text_named_description_is_skipped(text: str, line: int) -> None:
    assert _lines(text + _STEPS) == [line]


def test_a_recursive_anchor_is_read_once() -> None:
    text = _workflow("    env:\n      L: &x [macos-latest, *x]\n")
    assert _lines(text + _STEPS) == [6]


@pytest.mark.parametrize(
    ("marker_at", "line"),
    [
        ("ubuntu-latest  # allow-moving-runner-alias: tests the alias\n", []),
        ("ubuntu-latest  # allow-moving-runner-alias:\n", [5]),
    ],
)
def test_the_opt_out_needs_a_reason(marker_at: str, line: list[int]) -> None:
    assert _lines(_FIRING.replace("ubuntu-latest\n", marker_at)) == line


def test_a_marker_on_one_matrix_entry_does_not_cover_the_next() -> None:
    text = _workflow(
        "    strategy:\n      matrix:\n        os:\n"
        "          - macos-latest  # allow-moving-runner-alias: tests the alias\n"
        "          - ubuntu-latest\n" + _STEPS
    )
    assert _lines(text) == [9]


def test_a_marker_inside_a_quoted_value_does_not_exempt() -> None:
    text = _FIRING.replace(
        "    runs-on:",
        '    name: "x  # allow-moving-runner-alias: not a comment"\n    runs-on:',
    )
    assert _lines(text) == [6]


def test_unparseable_yaml_is_a_finding() -> None:
    [(line, message)] = mra.violations("jobs: [unclosed\n")
    assert line == 1
    assert message.startswith("could not parse as YAML")


@pytest.mark.parametrize("text", ["", "# only a comment\n", "just a scalar\n"])
def test_a_document_with_no_alias_is_clean(text: str) -> None:
    assert _lines(text) == []


def _point_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    wf = tmp_path / ".github" / "workflows"
    actions = tmp_path / ".github" / "actions"
    wf.mkdir(parents=True)
    monkeypatch.setattr(mra, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mra, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(mra, "ACTIONS_DIR", actions)
    return wf, actions


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


def test_main_reports_a_script_the_grammar_refuses_and_reads_the_next_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wf, _actions = _point_at(tmp_path, monkeypatch)
    pipes = " | ".join(["echo ubuntu-latest"] + ["cat"] * 2001)
    (wf / "a.yaml").write_text(
        _workflow(f"    runs-on: ubuntu-24.04\n    steps:\n      - run: {pipes}\n"),
        encoding="utf-8",
    )
    (wf / "b.yaml").write_text(_FIRING, encoding="utf-8")
    with pytest.raises(SystemExit):
        mra.main()
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/a.yaml::input carries more than" in out
    assert "::error file=.github/workflows/b.yaml,line=5::" in out


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
