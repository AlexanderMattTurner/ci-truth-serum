"""Tests for ci_truth_serum/check_cron_alert_coverage.py — the guard that a
scheduled workflow's failures reach a human, since a cron has no PR surface.

Everything is driven through the real ``violations()`` / ``gate_direction()``
code on synthetic workflow YAML, and through ``main()`` against fixture trees in
tmp dirs (discovery redirected at the module's dir constants, so the real repo's
workflows never leak into a case). The load-bearing property is REACHABILITY:
the parametrized gate tables below walk every accepted and every rejected gate
shape member by member, and the non-vacuity pair asserts the same workflow
flips from clean to flagged when only its gate regresses to `== 'success'`.
"""

import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

cac = load_hook("check_cron_alert_coverage.py", "check_cron_alert_coverage")

MATCHER = cac.notifier_matcher([])


def _workflow(
    step_gate: str = "", job_gate: str = "", marker: str = "", step_name: str = "Notify"
) -> str:
    """One scheduled workflow with a single notification step, gates injected."""
    marker_line = f"    {marker}\n" if marker else ""
    job_if = f"    if: {job_gate}\n" if job_gate else ""
    step_if = f"        if: {step_gate}\n" if step_gate else ""
    return (
        "name: Nightly\n"
        "on:\n"
        "  schedule:\n"
        f"{marker_line}"
        "    - cron: '0 3 * * *'\n"
        "jobs:\n"
        "  work:\n"
        f"{job_if}"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: make check\n"
        f"      - name: {step_name}\n"
        f"{step_if}"
        "        uses: ./.github/actions/notify-ntfy\n"
    )


def _find(text: str, require_alert: bool = False) -> list[tuple[int, str]]:
    return cac.violations(text, MATCHER, require_alert)


# ── gate_direction: every accepted shape ─────────────────────────────────
@pytest.mark.parametrize(
    "gate",
    [
        "failure()",
        "${{ failure() }}",
        "always()",
        "${{ always() }}",
        "cancelled()",
        "failure() && github.event_name == 'schedule'",
        "always() && github.event_name == 'schedule'",
        "needs.build.result != 'success'",
        "needs.build.result == 'failure'",
        "needs.build.result == 'cancelled'",
        "needs.build.result == 'timed_out'",
        'needs.build.result == "failure"',
        "steps.probe.outcome != 'success'",
        "needs.build.conclusion == 'failure'",
        "always() && needs.analyze.result == 'failure'",
        "${{ needs.build.result != 'success' }}",
    ],
)
def test_gate_direction_reachable(gate):
    assert cac.gate_direction(gate) == cac.REACHABLE


# ── gate_direction: every rejected shape ─────────────────────────────────
@pytest.mark.parametrize(
    "gate",
    [
        "success()",
        "${{ success() }}",
        "needs.build.result == 'success'",
        'needs.build.result == "success"',
        "needs.build.result != 'failure'",
        "steps.probe.outcome == 'success'",
        "needs.build.conclusion == 'success'",
        "needs.a.result == 'success' && needs.b.result == 'success'",
        "success() && github.event_name == 'schedule'",
    ],
)
def test_gate_direction_blocked(gate):
    assert cac.gate_direction(gate) == cac.BLOCKED


# ── gate_direction: says nothing about status ────────────────────────────
@pytest.mark.parametrize(
    "gate",
    [
        "",
        None,
        "github.event_name == 'schedule'",
        "steps.check.outputs.drift == 'true'",
        "needs.decide.outputs.run == 'true'",
        "inputs.suite == 'nightly'",
    ],
)
def test_gate_direction_neutral(gate):
    assert cac.gate_direction(gate) == cac.NEUTRAL


def test_direction_is_read_from_the_comparison_not_the_mere_mention():
    # Both gates name `needs.build.result`; only the direction differs, and the
    # direction is the whole finding.
    assert cac.gate_direction("needs.build.result != 'success'") == cac.REACHABLE
    assert cac.gate_direction("needs.build.result == 'success'") == cac.BLOCKED


# ── non-vacuity: the same workflow, clean then regressed ─────────────────
def test_failure_gated_notifier_passes_in_both_modes():
    text = _workflow(step_gate="failure()")
    assert _find(text) == []
    assert _find(text, require_alert=True) == []


def test_same_workflow_fails_when_the_gate_regresses_to_success():
    # Only the step gate changed; the notifier, the schedule, and the job are
    # byte-identical to the passing case above.
    found = _find(_workflow(step_gate="success()"))
    assert len(found) == 1
    assert "success-only gate" in found[0][1]


def test_job_level_success_gate_blocks_even_a_failure_gated_step():
    found = _find(
        _workflow(step_gate="failure()", job_gate="needs.a.result == 'success'")
    )
    assert len(found) == 1
    assert "success-only gate" in found[0][1]


def test_job_level_gate_carries_reachability_for_an_ungated_step():
    # The codeql shape: the notify JOB is gated on failure, its step is bare.
    text = _workflow(job_gate="always() && needs.analyze.result == 'failure'")
    assert _find(text) == []
    assert _find(text, require_alert=True) == []


# ── the ungated trailing notifier: neutral, not coverage ─────────────────
def test_ungated_notifier_is_silent_by_default_but_fails_strict():
    text = _workflow()
    assert _find(text) == []
    found = _find(text, require_alert=True)
    assert len(found) == 1
    assert "gated on something other than status" in found[0][1]


def test_output_gated_notifier_is_silent_by_default_but_fails_strict():
    text = _workflow(step_gate="steps.check.outputs.drift == 'true'")
    assert _find(text) == []
    assert len(_find(text, require_alert=True)) == 1


# ── unadopted repos stay quiet by default ────────────────────────────────
def test_scheduled_workflow_with_no_notifier_at_all():
    text = textwrap.dedent(
        """\
        name: Nightly
        on:
          schedule:
            - cron: '0 3 * * *'
        jobs:
          work:
            runs-on: ubuntu-latest
            steps:
              - run: make check
        """
    )
    assert _find(text) == []
    found = _find(text, require_alert=True)
    assert len(found) == 1
    assert "routes its failures nowhere" in found[0][1]


@pytest.mark.parametrize(
    "on_block",
    [
        "on:\n  push:\n    branches: [main]\n",
        "on: [push, pull_request]\n",
        "on: push\n",
    ],
)
def test_unscheduled_workflows_are_out_of_scope(on_block):
    text = f"name: X\n{on_block}jobs:\n  a:\n    steps:\n      - run: make\n"
    assert _find(text, require_alert=True) == []


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
    assert len(_find(text, require_alert=True)) == 1


# ── the marker: reason mandatory, placeholders rejected ──────────────────
def test_marker_with_a_reason_excuses_a_workflow_with_no_notifier():
    text = _workflow(
        marker="# cron-alert: false  # PR-only proposal; a failed fire retries tomorrow.",
        step_name="build",
    )
    assert _find(text, require_alert=True) == []


@pytest.mark.parametrize(
    "marker",
    [
        "# cron-alert: false",
        "# cron-alert: false  #",
        "# cron-alert: false  # n/a",
        "# cron-alert: false  # N/A.",
        "# cron-alert: false  # none",
        "# cron-alert: false  # not needed",
        "# cron-alert: false  # not applicable",
        "# cron-alert: false  # TBD",
        "# cron-alert: false  # nothing to do",
    ],
)
def test_reasonless_and_placeholder_markers_fail(marker):
    found = _find(_workflow(marker=marker, step_name="build"))
    assert len(found) == 1
    assert "cron-alert" in found[0][1]


def test_marker_with_an_unrecognized_value_fails():
    found = _find(_workflow(marker="# cron-alert: true  # whatever", step_name="build"))
    assert len(found) == 1
    assert "unrecognized value" in found[0][1]


def test_a_malformed_marker_fails_even_in_default_mode():
    # The default mode is lenient about UNADOPTED repos, never about a marker
    # that is present and states nothing.
    assert len(_find(_workflow(marker="# cron-alert: false", step_name="build"))) == 1


def test_marker_outside_the_schedule_block_does_not_count():
    # A marker buried in a step is not a classification of the schedule.
    text = (
        "name: N\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n"
        "  work:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: make  # cron-alert: false  # smuggled in from a step\n"
    )
    assert len(_find(text, require_alert=True)) == 1


def test_marker_on_the_schedule_key_line_counts():
    text = (
        "name: N\non:\n  schedule:  # cron-alert: false  # cosmetic badge refresh only\n"
        "    - cron: '0 3 * * *'\njobs:\n  work:\n    steps:\n      - run: make\n"
    )
    assert _find(text, require_alert=True) == []


# ── notifier recognition is configurable, additive ───────────────────────
@pytest.mark.parametrize(
    "step",
    [
        "        uses: ./.github/actions/notify-ntfy\n",
        "        uses: rtCamp/action-slack-notify@v2\n",
        "        run: curl -d hi https://ntfy.sh/topic\n",
        "        run: gh issue create --title broke\n",
        "        run: curl -X POST $PAGERDUTY_URL\n",
    ],
)
def test_default_patterns_recognize_common_sinks(step):
    text = (
        "name: N\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  work:\n"
        "    steps:\n      - name: alarm\n        if: success()\n" + step
    )
    found = cac.violations(text, MATCHER, False)
    assert len(found) == 1, "sink not recognized as a notifier"


def test_custom_pattern_extends_rather_than_replaces_the_defaults():
    house = cac.notifier_matcher([r"tell-the-humans"])
    custom = (
        "name: N\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  work:\n"
        "    steps:\n      - name: tell-the-humans\n        if: success()\n"
        "        run: ./bin/page\n"
    )
    assert len(cac.violations(custom, house, False)) == 1
    assert cac.violations(custom, MATCHER, False) == []
    # The built-ins still fire under the extended matcher.
    assert len(cac.violations(_workflow(step_gate="success()"), house, False)) == 1


def test_unrelated_step_is_not_mistaken_for_a_notifier():
    text = _workflow(step_gate="success()", step_name="Upload artifact")
    text = text.replace("uses: ./.github/actions/notify-ntfy", "run: ./bin/upload")
    assert cac.violations(text, MATCHER, False) == []


# ── job-level `uses:` notifier (a reusable notifier workflow) ────────────
def test_job_level_reusable_notifier_is_credited_by_its_job_gate():
    text = (
        "name: N\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n"
        "  work:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n"
        "  tell:\n    if: failure()\n    uses: ./.github/workflows/notify.yaml\n"
    )
    assert cac.violations(text, MATCHER, True) == []


# ── malformed YAML is reported, never a silent pass ──────────────────────
def test_unparseable_workflow_is_a_finding():
    found = _find("on: [schedule\njobs: {\n")
    assert len(found) == 1
    assert "could not parse as YAML" in found[0][1]


# ── main() over a fixture tree ───────────────────────────────────────────
def _tree(tmp_path: Path, monkeypatch, files: dict[str, str]) -> Path:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    for name, body in files.items():
        (wf_dir / name).write_text(body)
    monkeypatch.setattr(cac, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cac, "WORKFLOWS_DIR", wf_dir)
    return wf_dir


def test_main_is_clean_on_a_compliant_tree(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": _workflow(step_gate="failure()"),
            "b.yaml": _workflow(
                marker="# cron-alert: false  # opens a PR only; retries tomorrow.",
                step_name="build",
            ),
            "pr.yaml": "name: p\non:\n  pull_request:\njobs: {}\n",
        },
    )
    assert cac.main(["--require-alert"]) == 0
    assert capsys.readouterr().out == ""


def test_main_annotates_file_and_line(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"a.yaml": _workflow(step_gate="success()")})
    assert cac.main([]) == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/a.yaml,line=10::" in out
    assert "1 cron-alert-coverage violation(s) found." in out


def test_main_accepts_repeated_notifier_patterns(tmp_path, monkeypatch, capsys):
    text = (
        "name: N\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  work:\n"
        "    steps:\n      - name: klaxon\n        if: success()\n"
        "        run: ./bin/page\n"
    )
    _tree(tmp_path, monkeypatch, {"a.yaml": text})
    assert cac.main([]) == 0
    assert (
        cac.main(["--notifier-pattern", "siren", "--notifier-pattern", "klaxon"]) == 1
    )
    assert "success-only gate" in capsys.readouterr().out


def test_main_argv_defaults_to_sys_argv(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch, {"a.yaml": _workflow(step_gate="success()")})
    monkeypatch.setattr(sys, "argv", ["check_cron_alert_coverage"])
    assert cac.main() == 1


# ── crash resistance ─────────────────────────────────────────────────────
_FRAGMENTS = [
    "name: x\n",
    "on: schedule\n",
    "on:\n  schedule:\n    - cron: '0 0 * * 0'\n",
    "on:\n  schedule:\n    # cron-alert: false\n    - cron: '0 0 * * 0'\n",
    "on: [push, schedule]\n",
    "on: null\n",
    "jobs: {}\n",
    "jobs:\n  a:\n    if: success()\n    steps:\n      - uses: notify/act@v1\n",
    "jobs:\n  a:\n    steps: notascalar\n",
    "jobs: []\n",
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
@given(text=_workflow_text(), strict=st.booleans())
def test_violations_never_crashes(text, strict):
    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        assume(False)
    result = cac.violations(text, MATCHER, strict)
    assert all(isinstance(line, int) and isinstance(msg, str) for line, msg in result)
