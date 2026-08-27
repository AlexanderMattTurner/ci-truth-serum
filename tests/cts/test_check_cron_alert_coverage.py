"""Tests for ci_truth_serum/check_cron_alert_coverage.py — the guard that a
scheduled workflow's failures reach a human, since a cron has no PR surface.

Everything is driven through the real ``violations()`` code on synthetic
workflow YAML, and through ``main()`` against fixture trees in tmp dirs
(discovery redirected at the module's dir constants, so the real repo's
workflows never leak into a case). The non-vacuity pairs assert the same
workflow flips from clean to flagged when only its gate regresses to
`== 'success'`, and from flagged to clean when only the tree's notifier starts
listing it. The gate grammar and the four routes to a human belong to
``_cts_failure_routing`` and are pinned in ``test_cts_failure_routing.py``.
"""

import sys
import textwrap
import time
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


def test_marker_on_the_push_key_counts_for_a_schedule_workflow():
    # One marker means one thing to both coverage lints: this workflow's
    # failures are deliberately routed nowhere. Which monitored trigger key
    # carries it is not a second doctrine.
    text = (
        "name: N\non:\n  push:  # cron-alert: false  # cosmetic badge refresh only\n"
        "    branches: [main]\n  schedule:\n    - cron: '0 3 * * *'\n"
        "jobs:\n  work:\n    steps:\n      - run: make\n"
    )
    assert _find(text, require_alert=True) == []


# ── route 2: the tree's notifier already pages for this workflow ─────────
BARE_CRON = "name: Nightly\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs: {}\n"


def test_a_workflow_the_notifier_watches_needs_no_notifier_of_its_own():
    # Non-vacuity: the only difference between the two calls is whether the
    # tree's notifier lists this workflow's display name.
    assert len(cac.violations(BARE_CRON, MATCHER, True, frozenset())) == 1
    assert cac.violations(BARE_CRON, MATCHER, True, frozenset({"Nightly"})) == []


def test_watched_membership_is_judged_on_the_display_name():
    # A name the notifier does not carry is not coverage, however close it looks.
    assert len(cac.violations(BARE_CRON, MATCHER, True, frozenset({"nightly"}))) == 1


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


# ── issue-management sinks: the verb prefix is the whole boundary ────────
def _issue_sink_workflow(step_name: str, run: str = "") -> str:
    """A scheduled workflow whose ONLY candidate sink is one `if: failure()`
    step — so `--require-alert` is clean exactly when that step is recognized."""
    run_line = f"        run: {run}\n" if run else ""
    return (
        "name: Nightly\n"
        "on:\n"
        "  schedule:\n"
        "    - cron: '0 3 * * *'\n"
        "jobs:\n"
        "  work:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: make check\n"
        f"      - name: {step_name}\n"
        "        if: failure()\n"
        f"{run_line}"
    )


@pytest.mark.parametrize(
    ("step_name", "run"),
    [
        # The real repo shape: a house script wrapping the tracker call, whose
        # name never carries the literal `gh issue create`.
        (
            "Open a tracking issue",
            "bash .github/scripts/manage-release-failure-issue.sh open",
        ),
        ("Open a tracking issue if the release run failed", ""),
        # The member the narrower `create[-_]issue` already matched: broadening
        # the verb set must not have dropped it.
        ("alarm", "./bin/create-issue --title broke"),
        ("alarm", "./bin/report_issue --failure"),
        ("File an issue for the failed nightly", ""),
    ],
)
def test_issue_management_sinks_are_recognized(step_name, run):
    text = _issue_sink_workflow(step_name, run)
    assert _find(text, require_alert=True) == [], "issue sink not recognized"
    assert _find(text) == []


@pytest.mark.parametrize(
    ("step_name", "run"),
    [
        ("Close the milestone", ""),
        # `open` trails the noun here, so no verb precedes `issue`.
        ("triage", "gh issue list --state open"),
        ("Known issues summary", ""),
    ],
)
def test_mentioning_issues_without_a_verb_is_not_a_sink(step_name, run):
    # The verb prefix is what keeps the broadened pattern from crediting every
    # step that merely says "issue"; without it the workflow is still uncovered.
    found = _find(_issue_sink_workflow(step_name, run), require_alert=True)
    assert len(found) == 1
    assert "routes its failures nowhere" in found[0][1]


def test_issue_pattern_does_not_backtrack_on_a_long_near_match():
    # The pattern's gap is a bounded lazy run of one token class; a long run of
    # exactly those characters with no trailing `issue` is its worst input.
    adversarial = "open" + "a_b." * 300
    matcher = cac.notifier_matcher([])
    start = time.perf_counter()
    result = matcher.search(adversarial)
    elapsed = time.perf_counter() - start
    assert result is None
    # allow-wall-clock: catastrophic backtracking runs for seconds to minutes,
    # so the duration IS the subject here, and 1.0s is far above any
    # scheduling noise a linear match can pick up.
    assert elapsed < 1.0, f"notifier matcher took {elapsed:.3f}s on a near-match"


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
        (wf_dir / name).write_text(body, encoding="utf-8")
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
    assert cac.main([]) == 0
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
    assert cac.main(["--allow-unrouted"]) == 0
    assert (
        cac.main(["--notifier-pattern", "siren", "--notifier-pattern", "klaxon"]) == 1
    )
    assert "success-only gate" in capsys.readouterr().out


def test_main_argv_defaults_to_sys_argv(tmp_path, monkeypatch):
    _tree(tmp_path, monkeypatch, {"a.yaml": _workflow(step_gate="success()")})
    monkeypatch.setattr(sys, "argv", ["check_cron_alert_coverage"])
    assert cac.main() == 1


# ── fail-closed default and its one escape ───────────────────────────────
def test_main_fails_closed_on_an_unrouted_cron_with_no_flags(
    tmp_path, monkeypatch, capsys
):
    # The whole point of the default: a tree where nothing is routed must not
    # read as a tree where everything is covered.
    _tree(tmp_path, monkeypatch, {"a.yaml": BARE_CRON})
    assert cac.main([]) == 1
    out = capsys.readouterr().out
    assert "routes its failures nowhere" in out
    assert "--allow-unrouted" in out, "the escape must be named in the failure"


def test_allow_unrouted_silences_the_same_tree(tmp_path, monkeypatch, capsys):
    # Non-vacuity pair with the case above: byte-identical tree, one flag.
    _tree(tmp_path, monkeypatch, {"a.yaml": BARE_CRON})
    assert cac.main(["--allow-unrouted"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("case", "body", "expected"),
    [
        ("success-gated notifier", _workflow(step_gate="success()"), "success-only"),
        (
            "malformed marker",
            _workflow(marker="# cron-alert: false", step_name="build"),
            "cron-alert",
        ),
    ],
)
def test_allow_unrouted_still_fails_on_a_false_claim_of_coverage(
    tmp_path, monkeypatch, capsys, case, body, expected
):
    # --allow-unrouted excuses only the un-adopted case. Claiming coverage you
    # do not have stays a finding, or the flag would be a mute switch.
    _tree(tmp_path, monkeypatch, {"a.yaml": body})
    assert cac.main(["--allow-unrouted"]) == 1, case
    assert expected in capsys.readouterr().out, case


def test_allow_unrouted_is_the_only_flag_that_relaxes_the_default(capsys):
    # A repo cannot reach the lenient mode by any other spelling: the removed
    # --require-alert must not survive as an accepted no-op that reads as
    # enforcement while changing nothing.
    with pytest.raises(SystemExit):
        cac.main(["--require-alert"])


def _notifier_over(*names: str) -> str:
    listed = "".join(f'      - "{name}"\n' for name in names)
    return (
        "name: CI failure notify\non:\n  workflow_run:\n    workflows:\n"
        f"{listed}    types: [completed]\njobs: {{}}\n"
    )


def test_main_credits_a_workflow_the_discovered_notifier_lists(
    tmp_path, monkeypatch, capsys
):
    # Through the real tree walk: main must discover the notifier and read its
    # list, or a repo whose crons are covered centrally is told to add a step
    # that would page it twice.
    _tree(
        tmp_path,
        monkeypatch,
        {"a.yaml": BARE_CRON, "notify.yaml": _notifier_over("Nightly")},
    )
    assert cac.main([]) == 0
    assert capsys.readouterr().out == ""


def test_main_still_flags_a_workflow_the_notifier_does_not_list(
    tmp_path, monkeypatch, capsys
):
    _tree(
        tmp_path,
        monkeypatch,
        {"a.yaml": BARE_CRON, "notify.yaml": _notifier_over("Something else")},
    )
    assert cac.main([]) == 1
    assert "routes its failures nowhere" in capsys.readouterr().out


def test_main_does_not_credit_a_list_on_a_workflow_that_is_not_a_notifier(
    tmp_path, monkeypatch, capsys
):
    # A workflow_run consumer with no notification sink (an artifact collector)
    # observes the run but tells nobody, so its list is not coverage.
    collector = (
        "name: Collect artifacts\non:\n  workflow_run:\n    workflows:\n"
        '      - "Nightly"\n    types: [completed]\njobs: {}\n'
    )
    _tree(tmp_path, monkeypatch, {"a.yaml": BARE_CRON, "collect.yaml": collector})
    assert cac.main([]) == 1
    assert "routes its failures nowhere" in capsys.readouterr().out


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
