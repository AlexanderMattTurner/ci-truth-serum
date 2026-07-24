"""Tests for ci_truth_serum/check_cron_staleness_optout.py — the marker
discipline for the question a failure notifier structurally cannot answer:
did this cron RUN at all?

Driven through the real ``violations()`` / ``check_repo()`` on synthetic
workflow YAML and fixture trees. The load-bearing case is the NON-SUBSTITUTION:
`# cron-alert: false` answers "a failed fire is harmless", which presumes a next
cycle, so it must never be read as an answer to "is there still a next cycle".
"""

import textwrap
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

cso = load_hook("check_cron_staleness_optout.py", "check_cron_staleness_optout")

SCHEDULED = textwrap.dedent(
    """\
    name: Nightly
    on:
      schedule:
    {marker}    - cron: '0 3 * * *'
    jobs:
      work:
        runs-on: ubuntu-latest
        steps:
          - run: make check
    """
)


def _workflow(marker: str = "") -> str:
    return SCHEDULED.format(marker=f"    {marker}\n" if marker else "")


def _find(text: str, require: bool = False, watched: bool = False):
    return cso.violations(text, require, watched)


# ── default mode is silent in an unadopted repo ──────────────────────────
def test_no_marker_is_silent_by_default():
    assert _find(_workflow()) == []


def test_no_marker_fails_under_require():
    found = _find(_workflow(), require=True)
    assert len(found) == 1
    assert "cron-stale" in found[0][1]


def test_unscheduled_workflow_is_out_of_scope():
    text = "name: X\non:\n  pull_request:\njobs:\n  a:\n    steps:\n      - run: make\n"
    assert _find(text, require=True) == []


@pytest.mark.parametrize(
    "on_block",
    [
        "on:\n  schedule:\n    - cron: '0 3 * * *'\n",
        "on: schedule\n",
        "on: [push, schedule]\n",
    ],
)
def test_every_schedule_trigger_shape_is_in_scope(on_block):
    text = f"name: X\n{on_block}jobs:\n  a:\n    steps:\n      - run: make\n"
    assert len(_find(text, require=True)) == 1


# ── the marker: same shape, same mandatory reason ────────────────────────
def test_marker_with_a_reason_satisfies_require():
    marker = "# cron-stale: false  # The daily CI run covers the same assertions."
    assert _find(_workflow(marker), require=True) == []


@pytest.mark.parametrize(
    "marker",
    [
        "# cron-stale: false",
        "# cron-stale: false  #",
        "# cron-stale: false  # n/a",
        "# cron-stale: false  # None",
        "# cron-stale: false  # not needed",
        "# cron-stale: false  # not applicable",
        "# cron-stale: false  # TODO",
        "# cron-stale: false  # -",
    ],
)
def test_reasonless_and_placeholder_markers_fail_even_by_default(marker):
    # Default mode is lenient about an unadopted repo, never about a marker that
    # is present and states nothing.
    found = _find(_workflow(marker))
    assert len(found) == 1
    assert "reason IS the marker" in found[0][1]


def test_marker_with_an_unrecognized_value_fails():
    found = _find(_workflow("# cron-stale: maybe  # hmm"))
    assert len(found) == 1
    assert "unrecognized value" in found[0][1]


def test_marker_outside_the_schedule_block_does_not_count():
    text = (
        "name: N\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n"
        "  work:\n    steps:\n"
        "      - run: make  # cron-stale: false  # smuggled in from a step\n"
    )
    assert len(_find(text, require=True)) == 1


# ── NON-SUBSTITUTION: cron-alert is not cron-stale ───────────────────────
def test_cron_alert_marker_does_not_satisfy_cron_stale():
    marker = (
        "# cron-alert: false  # A failed fire self-heals on the next cycle, and "
        "the run opens a PR only."
    )
    found = _find(_workflow(marker), require=True)
    assert len(found) == 1, "a failure-alert opt-out was read as a staleness opt-out"
    # The finding explains WHY, since that reasoning is the whole rule.
    assert "presumes there IS a next cycle" in found[0][1]


def test_both_markers_together_satisfy_the_staleness_question():
    text = SCHEDULED.format(
        marker=(
            "    # cron-alert: false  # PR-only proposal; retries tomorrow.\n"
            "    # cron-stale: false  # The nightly CI job asserts the same pins.\n"
        )
    )
    assert _find(text, require=True) == []


def test_cron_stale_alone_is_enough():
    marker = "# cron-stale: false  # A dormant repo has nothing left to watch."
    assert _find(_workflow(marker), require=True) == []


# ── malformed YAML is a finding, never a silent pass ─────────────────────
def test_unparseable_workflow_is_a_finding():
    found = _find("on: [schedule\njobs: {\n", require=True)
    assert len(found) == 1
    assert "startup_failure" in found[0][1]


def test_unparseable_workflow_is_a_finding_by_default_too():
    assert len(_find("on: [schedule\njobs: {\n")) == 1


# ── the watchdog: declared means it must actually exist and fire ─────────
def _tree(tmp_path: Path, monkeypatch, files: dict[str, str]) -> Path:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    for name, body in files.items():
        (wf_dir / name).write_text(body)
    monkeypatch.setattr(cso, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cso, "WORKFLOWS_DIR", wf_dir)
    return wf_dir


def test_missing_watchdog_is_a_hard_failure(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch, {"a.yaml": _workflow()})
    found = cso.check_repo(False, "cron-watch.yaml")
    assert any("does not exist" in m for m in found)


def test_unscheduled_watchdog_is_a_hard_failure(tmp_path, monkeypatch):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": _workflow(),
            "watch.yaml": "name: w\non:\n  workflow_dispatch:\njobs: {}\n",
        },
    )
    found = cso.check_repo(False, "watch.yaml")
    assert any("never fires on its own" in m for m in found)


def test_unparseable_watchdog_is_a_hard_failure(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch, {"watch.yaml": "on: [schedule\njobs: {\n"})
    found = cso.check_repo(False, "watch.yaml")
    assert any("does not parse as YAML" in m for m in found)


def test_a_valid_watchdog_covers_every_cron_but_itself(tmp_path, monkeypatch):
    _tree(
        tmp_path,
        monkeypatch,
        {"a.yaml": _workflow(), "b.yaml": _workflow(), "watch.yaml": _workflow()},
    )
    found = cso.check_repo(True, "watch.yaml")
    assert len(found) == 1, found
    assert "watch.yaml" in found[0]


def test_the_watchdog_itself_is_excused_by_its_own_marker(tmp_path, monkeypatch):
    watchdog = _workflow(
        "# cron-stale: false  # Nothing watches the watcher; its own quiet is "
        "caught by the tracking issue going stale."
    )
    _tree(tmp_path, monkeypatch, {"a.yaml": _workflow(), "watch.yaml": watchdog})
    assert cso.check_repo(True, "watch.yaml") == []


def test_a_broken_watchdog_does_not_excuse_the_other_crons(tmp_path, monkeypatch):
    # Fail closed: a watchdog that cannot run must not silently confer coverage.
    _tree(tmp_path, monkeypatch, {"a.yaml": _workflow()})
    found = cso.check_repo(True, "watch.yaml")
    assert any("does not exist" in m for m in found)
    assert any("a.yaml" in m for m in found)


# ── main() wiring ────────────────────────────────────────────────────────
def test_main_clean_tree(tmp_path, monkeypatch, capsys):
    marker = "# cron-stale: false  # Advisory only; a stopped schedule loses nothing."
    _tree(tmp_path, monkeypatch, {"a.yaml": _workflow(marker)})
    assert cso.main(["--require-stale-marker"]) == 0
    assert capsys.readouterr().out == ""


def test_main_annotates_file_and_line(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"a.yaml": _workflow("# cron-stale: false")})
    assert cso.main([]) == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/a.yaml,line=3::" in out
    assert "1 cron-staleness-optout violation(s) found." in out


def test_main_is_silent_on_an_unadopted_tree(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"a.yaml": _workflow(), "b.yaml": _workflow()})
    assert cso.main([]) == 0
    assert capsys.readouterr().out == ""


# ── crash resistance ─────────────────────────────────────────────────────
_FRAGMENTS = [
    "name: x\n",
    "on: schedule\n",
    "on:\n  schedule:\n    - cron: '0 0 * * 0'\n",
    "on:\n  schedule:\n    # cron-stale: false\n    - cron: '0 0 * * 0'\n",
    "on:\n  schedule:\n    # cron-alert: false  # because\n    - cron: '0 0 * * 0'\n",
    "on: [push, schedule]\n",
    "on: null\n",
    "jobs: {}\n",
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
@given(text=_workflow_text(), require=st.booleans(), watched=st.booleans())
def test_violations_never_crashes(text, require, watched):
    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        assume(False)
    result = cso.violations(text, require, watched)
    assert all(isinstance(line, int) and isinstance(msg, str) for line, msg in result)
