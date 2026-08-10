"""Tests for ci_truth_serum/check_workflow_run_branch_filter.py — the guard that a
`workflow_run` listener names the branches whose completions it answers, since
GitHub creates the run before the listener's own `if:` can discard it.

Everything is driven through the real ``violations()`` on synthetic workflow
YAML, and through ``main()`` against fixture trees in tmp dirs (discovery
redirected at the module's dir constants). The non-vacuity pairs assert the same
file flips from flagged to clean when only the filter (or the marker) changes.
"""

import textwrap
from pathlib import Path

import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

wrf = load_hook(
    "check_workflow_run_branch_filter.py", "check_workflow_run_branch_filter"
)


def _workflow(trigger: str) -> str:
    """A listener workflow whose `on:` block is TRIGGER, indented under `on:`."""
    block = textwrap.indent(textwrap.dedent(trigger).strip("\n"), "  ")
    return (
        "name: Notify\n"
        "on:\n"
        f"{block}\n"
        "\n"
        "jobs:\n"
        "  notify:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: make notify\n"
    )


FILTERED = _workflow(
    """
    workflow_run:
      workflows: [CI]
      types: [completed]
      branches: [main]
    """
)
UNFILTERED = _workflow(
    """
    workflow_run:
      workflows: [CI]
      types: [completed]
    """
)


# ── the filter that makes the listener legitimate ────────────────────────
def test_a_branch_filter_passes():
    assert wrf.violations(FILTERED) == []


def test_the_same_file_without_the_filter_is_flagged():
    """Non-vacuity: FILTERED and UNFILTERED differ by the `branches:` line
    alone, so the finding cannot come from anything else in the file."""
    found = wrf.violations(UNFILTERED)
    assert len(found) == 1
    assert "no `branches:`/`branches-ignore:` filter" in found[0][1]


def test_branches_ignore_counts_as_a_filter():
    assert (
        wrf.violations(
            _workflow(
                """
                workflow_run:
                  workflows: [CI]
                  branches-ignore: [dependabot/**]
                """
            )
        )
        == []
    )


def test_a_workflow_with_no_workflow_run_trigger_is_untouched():
    for trigger in ("push:\n  branches: [main]", "schedule:\n  - cron: '0 6 * * 1'"):
        assert wrf.violations(_workflow(trigger)) == []
    assert wrf.violations("on: push\njobs: {}\n") == []
    assert wrf.violations("on: [push, pull_request]\njobs: {}\n") == []


def test_types_alone_is_not_a_branch_filter():
    """`types: [completed]` narrows which completion states fire, not which
    branches — the run is still created for every branch."""
    found = wrf.violations(UNFILTERED)
    assert len(found) == 1


# ── a filter that narrows nothing ────────────────────────────────────────
def test_an_all_wildcard_branches_list_narrows_nothing():
    for pattern in ("'*'", "'**'"):
        found = wrf.violations(
            _workflow(
                f"""
                workflow_run:
                  workflows: [CI]
                  branches: [{pattern}]
                """
            )
        )
        assert len(found) == 1, pattern
        assert "narrows nothing" in found[0][1]


def test_a_wildcard_beside_a_real_branch_is_still_vacuous_but_a_real_one_passes():
    vacuous = _workflow(
        """
        workflow_run:
          workflows: [CI]
          branches: ['**', '*']
        """
    )
    real = _workflow(
        """
        workflow_run:
          workflows: [CI]
          branches: ['**', release/*]
        """
    )
    assert len(wrf.violations(vacuous)) == 1
    # A list holding one non-wildcard pattern is not vacuous — `**` beside it
    # is a review question, not a run-creation one, and this lint reports the
    # runs, not the style.
    assert wrf.violations(real) == []


def test_an_empty_filter_list_under_either_key_is_flagged():
    for key in ("branches", "branches-ignore"):
        found = wrf.violations(
            _workflow(
                f"""
                workflow_run:
                  workflows: [CI]
                  {key}: []
                """
            )
        )
        assert len(found) == 1, key
        assert f"`{key}:` narrows nothing" in found[0][1]


def test_a_scalar_branches_value_is_read_as_one_pattern():
    assert (
        wrf.violations(
            _workflow(
                """
                workflow_run:
                  workflows: [CI]
                  branches: main
                """
            )
        )
        == []
    )
    assert len(wrf.violations(_workflow("workflow_run:\n  branches: '**'"))) == 1


# ── the bare-name spelling, which can carry no filter ────────────────────
def test_the_list_spelling_is_flagged_as_unfilterable():
    found = wrf.violations("on: [push, workflow_run]\njobs: {}\n")
    assert len(found) == 1
    assert "written as a bare name" in found[0][1]


def test_the_scalar_spelling_is_flagged_as_unfilterable():
    found = wrf.violations("on: workflow_run\njobs: {}\n")
    assert len(found) == 1
    assert "written as a bare name" in found[0][1]


def test_the_bare_name_spelling_can_be_annotated_on_the_on_block():
    """It has no `workflow_run:` key line, so the marker window falls back to
    the `on:` block — otherwise the finding would be unsuppressable."""
    text = "on: # unfiltered-listener-ok: an external repo dispatches this\n  [workflow_run]\njobs: {}\n"
    assert wrf.violations(text) == []


# ── the opt-out marker ───────────────────────────────────────────────────
def test_a_reasoned_marker_on_the_key_line_suppresses():
    text = UNFILTERED.replace(
        "workflow_run:",
        "workflow_run:  # unfiltered-listener-ok: comments on PR branches",
    )
    assert wrf.violations(text) == []


def test_a_reasoned_marker_on_a_direct_child_line_suppresses():
    text = UNFILTERED.replace(
        "    workflows: [CI]",
        "    # unfiltered-listener-ok: comments on PR branches\n    workflows: [CI]",
    )
    assert wrf.violations(text) == []


def test_a_marker_with_no_reason_is_its_own_finding():
    for suffix in ("", ": ", ": n/a"):
        text = UNFILTERED.replace(
            "workflow_run:", f"workflow_run:  # unfiltered-listener-ok{suffix}"
        )
        found = wrf.violations(text)
        assert len(found) == 1, suffix
        if suffix:
            assert "The reason IS the marker" in found[0][1]


def test_a_stale_marker_on_a_filtered_trigger_is_not_a_finding():
    """The marker suppresses nothing here, so its reason is never judged — the
    listener already names its branches."""
    text = FILTERED.replace("workflow_run:", "workflow_run:  # unfiltered-listener-ok:")
    assert wrf.violations(text) == []


def test_a_longer_slug_does_not_satisfy_the_marker():
    text = UNFILTERED.replace(
        "workflow_run:", "workflow_run:  # not-unfiltered-listener-ok: nope"
    )
    assert len(wrf.violations(text)) == 1


def test_a_marker_outside_the_trigger_block_does_not_suppress():
    text = UNFILTERED.replace(
        "  notify:", "  notify:  # unfiltered-listener-ok: wrong block"
    )
    assert len(wrf.violations(text)) == 1


# ── unit surfaces ────────────────────────────────────────────────────────
def test_trigger_line_points_at_the_key_and_falls_back_to_on():
    assert wrf.trigger_line(UNFILTERED) == 3
    assert wrf.trigger_line("name: x\non: [workflow_run]\n") == 2
    assert wrf.trigger_line("name: x\n") == 1


def test_vacuous_filter_names_the_key_it_faults():
    assert wrf.vacuous_filter({"branches": ["main"]}) is None
    assert wrf.vacuous_filter({"branches": []}) == "branches"
    assert wrf.vacuous_filter({"branches": ["*"]}) == "branches"
    assert wrf.vacuous_filter({"branches-ignore": []}) == "branches-ignore"
    assert wrf.vacuous_filter({"branches-ignore": ["main"]}) is None
    assert wrf.vacuous_filter({"workflows": ["CI"]}) is None


def test_marker_state_reports_presence_and_the_placeholder_detail():
    assert wrf.marker_state([]) == (False, None)
    assert wrf.marker_state(["  # unfiltered-listener-ok: real"]) == (True, None)
    present, detail = wrf.marker_state(["  # unfiltered-listener-ok: none"])
    assert present and detail == "states only 'none'"


def test_unparseable_yaml_is_reported_not_passed():
    found = wrf.violations("on:\n  workflow_run:\n   - [\n")
    assert len(found) == 1
    assert found[0][0] == 1
    assert "could not parse as YAML" in found[0][1]


# ── main() over a tree ───────────────────────────────────────────────────
def _tree(tmp_path: Path, monkeypatch, files: dict[str, str]) -> None:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    for name, text in files.items():
        (wf_dir / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(wrf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(wrf, "WORKFLOWS_DIR", wf_dir)


def test_main_passes_a_filtered_tree(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"a.yaml": FILTERED})
    assert wrf.main() == 0
    assert capsys.readouterr().out == ""


def test_main_annotates_file_and_line(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"a.yaml": UNFILTERED})
    assert wrf.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/a.yaml,line=3::" in out
    assert "1 unfiltered workflow_run listener(s) found." in out


def test_main_says_so_over_a_tree_with_no_workflows(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(wrf, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(wrf, "WORKFLOWS_DIR", tmp_path / ".github" / "workflows")
    assert wrf.main() == 0
    assert "scanned nothing" in capsys.readouterr().err


# ── crash resistance ─────────────────────────────────────────────────────
_FRAGMENTS = [
    "name: x\n",
    "on: workflow_run\n",
    "on: [push, workflow_run]\n",
    "on:\n  workflow_run:\n    workflows: [CI]\n",
    "on:\n  workflow_run:\n    branches: []\n",
    "on:\n  workflow_run:\n    branches: '**'\n",
    "on:\n  workflow_run: notamapping\n",
    "on:\n  workflow_run:\n    branches: {a: b}\n",
    "on: null\n",
    "on: []\n",
    "jobs: {}\n",
    "# unfiltered-listener-ok: reason\n",
    "  # unfiltered-listener-ok:\n",
    "[]\n",
    "just a scalar\n",
]


@st.composite
def _workflow_text(draw: st.DrawFn) -> str:
    parts = draw(st.lists(st.sampled_from(_FRAGMENTS), max_size=4))
    if draw(st.booleans()):
        parts.append(draw(st.text(max_size=60)))
    return "".join(parts)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(text=_workflow_text())
def test_violations_never_crashes(text):
    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        assume(False)
    result = wrf.violations(text)
    assert all(isinstance(line, int) and isinstance(msg, str) for line, msg in result)
    assert all(1 <= line <= max(1, len(text.splitlines())) for line, _ in result)
    assert result == wrf.violations(text)  # deterministic
