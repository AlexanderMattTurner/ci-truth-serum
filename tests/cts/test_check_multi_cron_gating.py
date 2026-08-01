"""Tests for ci_truth_serum/check_multi_cron_gating.py — the guard that a
workflow declaring two or more crons routes each one by name, since GitHub
starts every schedule-eligible job on every cron fire.

Everything is driven through the real ``violations()`` code on synthetic
workflow YAML, and through ``main()`` against fixture trees in tmp dirs
(discovery redirected at the module's dir constants). The non-vacuity pairs
assert the same file flips from flagged to clean when only the job's gate (or
its marker) changes, and the single-cron exemption is pinned against the
byte-identical two-cron failure.
"""

import textwrap
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

cmg = load_hook("check_multi_cron_gating.py", "check_multi_cron_gating")

CRON_A = "0 6 * * 1"
CRON_B = "13 5 * * 3"


def _workflow(crons: list[str], jobs: list[tuple[str, str | None, str | None]]) -> str:
    """A workflow with CRONS and JOBS as (name, if-expression, marker-comment);
    an if of None leaves the job ungated, a marker of None omits the comment."""
    out = ["name: Evals", "on:", "  schedule:"]
    out += [f'    - cron: "{cron}"' for cron in crons]
    out.append("jobs:")
    for name, if_expr, marker in jobs:
        out.append(f"  {name}:{'  ' + marker if marker else ''}")
        if if_expr is not None:
            out.append(f"    if: {if_expr}")
        out += ["    runs-on: ubuntu-latest", "    steps:", "      - run: make"]
    return "\n".join(out) + "\n"


def _named(cron: str) -> str:
    return f"github.event.schedule == '{cron}'"


# ── each cron named by its own job: the intended shape passes ────────────
def test_two_crons_each_named_by_its_own_job():
    text = _workflow(
        [CRON_A, CRON_B],
        [("weekly", _named(CRON_A), None), ("eval", _named(CRON_B), None)],
    )
    assert cmg.violations(text) == []


def test_one_job_naming_both_crons_passes():
    both = f"{_named(CRON_A)} || {_named(CRON_B)}"
    text = _workflow([CRON_A, CRON_B], [("sweep", both, None)])
    assert cmg.violations(text) == []


def test_two_jobs_naming_the_same_cron_pass_when_both_crons_are_named():
    text = _workflow(
        [CRON_A, CRON_B],
        [
            ("eval", _named(CRON_A), None),
            ("report", _named(CRON_A), None),
            ("weekly", _named(CRON_B), None),
        ],
    )
    assert cmg.violations(text) == []


# ── rule 1: the bare event-name gate cannot tell crons apart ─────────────
def test_bare_event_name_gate_fails_and_names_the_job():
    text = _workflow(
        [CRON_A, CRON_B],
        [
            ("eval", "github.event_name == 'schedule'", None),
            ("weekly", _named(CRON_B), None),
        ],
    )
    found = cmg.violations(text)
    assert len(found) == 1
    assert "'eval'" in found[0][1]
    assert "EVERY cron fire" in found[0][1]


@pytest.mark.parametrize(
    "expr",
    [
        "github.event_name == 'schedule'",
        'github.event_name == "schedule"',
        "'schedule' == github.event_name",
        "github.event_name=='schedule'",
        "${{ github.event_name == 'schedule' }}",
        "github.event_name == 'schedule' && runner.os == 'Linux'",
    ],
)
def test_every_spelling_of_the_bare_gate_is_recognized(expr):
    text = _workflow([CRON_A, CRON_B], [("eval", expr, None)])
    assert len(cmg.violations(text)) == 1


def test_event_name_gate_beside_a_named_cron_passes():
    # Redundant but harmless: the job DOES name its cron.
    expr = f"github.event_name == 'schedule' && {_named(CRON_A)}"
    text = _workflow(
        [CRON_A, CRON_B],
        [("eval", expr, None), ("weekly", _named(CRON_B), None)],
    )
    assert cmg.violations(text) == []


def test_single_cron_file_is_exempt_from_the_bare_gate_rule():
    # Non-vacuity pair with the failing case: same job, one cron fewer.
    text = _workflow([CRON_A], [("eval", "github.event_name == 'schedule'", None)])
    assert cmg.violations(text) == []


def test_non_schedule_gates_are_out_of_scope():
    # A job gated off schedule entirely never runs on a cron fire.
    text = _workflow(
        [CRON_A, CRON_B],
        [
            ("pr", "github.event_name == 'pull_request'", None),
            ("eval", _named(CRON_A), None),
            ("weekly", _named(CRON_B), None),
        ],
    )
    assert cmg.violations(text) == []


# ── rule 2: every declared cron must be answered ─────────────────────────
def test_a_cron_named_by_no_job_fails_and_names_the_cron():
    text = _workflow([CRON_A, CRON_B], [("eval", _named(CRON_A), None)])
    found = cmg.violations(text)
    assert len(found) == 1
    assert f"cron '{CRON_B}'" in found[0][1]
    # Anchored on the unnamed cron's own line.
    assert found[0][0] == 5


# ── rule 3: an ungated job runs on every cron ────────────────────────────
UNGATED = _workflow(
    [CRON_A, CRON_B],
    [("eval", None, None), ("weekly", _named(CRON_B), None)],
)
MARKED = _workflow(
    [CRON_A, CRON_B],
    [
        ("eval", None, "# multi-cron-ok: refreshes a cache every schedule wants warm"),
        ("weekly", _named(CRON_B), None),
    ],
)


def test_ungated_job_without_a_marker_fails():
    found = cmg.violations(UNGATED)
    assert len(found) == 1
    assert "'eval'" in found[0][1]
    assert "multi-cron-ok" in found[0][1]


def test_the_same_file_with_a_reasoned_marker_passes():
    # Non-vacuity pair: only the marker comment differs from UNGATED.
    assert cmg.violations(MARKED) == []


@pytest.mark.parametrize(
    "marker",
    [
        "# multi-cron-ok:",
        "# multi-cron-ok: n/a",
        "# multi-cron-ok: N/A.",
        "# multi-cron-ok: none",
        "# multi-cron-ok: not needed",
        "# multi-cron-ok: TBD",
    ],
)
def test_placeholder_marker_reasons_fail(marker):
    text = _workflow(
        [CRON_A, CRON_B],
        [("eval", None, marker), ("weekly", _named(CRON_B), None)],
    )
    found = cmg.violations(text)
    assert len(found) == 1
    assert "The reason IS the marker" in found[0][1]


def test_marker_on_a_direct_child_line_counts():
    text = textwrap.dedent(
        f"""\
        on:
          schedule:
            - cron: "{CRON_A}"
            - cron: "{CRON_B}"
        jobs:
          eval:
            # multi-cron-ok: a metrics roll-up every schedule should refresh
            runs-on: ubuntu-latest
            steps:
              - run: make
          weekly:
            if: {_named(CRON_B)}
            runs-on: ubuntu-latest
            steps:
              - run: make
        """
    )
    assert cmg.violations(text) == []


def test_marker_buried_in_a_step_does_not_count():
    text = textwrap.dedent(
        f"""\
        on:
          schedule:
            - cron: "{CRON_A}"
            - cron: "{CRON_B}"
        jobs:
          eval:
            runs-on: ubuntu-latest
            steps:
              - run: make  # multi-cron-ok: smuggled in from a step
        """
    )
    assert len(cmg.violations(text)) == 1


def test_a_marked_ungated_job_answers_every_cron():
    # The marker acknowledges a run-on-everything job, so a cron named by no
    # gated job is still answered — one violation shape does not cascade into
    # the other.
    text = _workflow(
        [CRON_A, CRON_B],
        [("eval", None, "# multi-cron-ok: a roll-up every schedule refreshes")],
    )
    assert cmg.violations(text) == []


def test_a_flagged_job_does_not_also_cascade_per_cron_findings():
    # One defect, one finding: the bare-gated job runs on both crons, so
    # neither cron is additionally reported as unanswered.
    text = _workflow(
        [CRON_A, CRON_B], [("eval", "github.event_name == 'schedule'", None)]
    )
    assert len(cmg.violations(text)) == 1


def test_marker_after_other_comment_text_counts():
    text = _workflow(
        [CRON_A, CRON_B],
        [
            ("eval", None, "# nightly refresh — multi-cron-ok: warms a shared cache"),
            ("weekly", _named(CRON_B), None),
        ],
    )
    assert cmg.violations(text) == []


def test_a_longer_slug_containing_the_token_is_not_the_marker():
    text = _workflow(
        [CRON_A, CRON_B],
        [
            ("eval", None, "# not-multi-cron-ok: this is a different annotation"),
            ("weekly", _named(CRON_B), None),
        ],
    )
    assert len(cmg.violations(text)) == 1


def test_a_reasoned_marker_wins_over_a_placeholder_on_another_line():
    text = textwrap.dedent(
        f"""\
        on:
          schedule:
            - cron: "{CRON_A}"
            - cron: "{CRON_B}"
        jobs:
          eval:  # multi-cron-ok: n/a
            # multi-cron-ok: a metrics roll-up every schedule should refresh
            runs-on: ubuntu-latest
            steps:
              - run: make
          weekly:
            if: {_named(CRON_B)}
            runs-on: ubuntu-latest
            steps:
              - run: make
        """
    )
    assert cmg.violations(text) == []


# ── needs: gating is inherited, not run-on-every ─────────────────────────
def _needs_workflow(needs_if: str = "") -> str:
    """Two gated jobs and a fan-in `report` job gated only by `needs:`."""
    if_line = f"    if: {needs_if}\n" if needs_if else ""
    return (
        "on:\n"
        "  schedule:\n"
        f'    - cron: "{CRON_A}"\n'
        f'    - cron: "{CRON_B}"\n'
        "jobs:\n"
        "  weekly:\n"
        f"    if: {_named(CRON_A)}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: make\n"
        "  monthly:\n"
        f"    if: {_named(CRON_B)}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: make\n"
        "  report:\n"
        "    needs: [weekly, monthly]\n"
        f"{if_line}"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: make\n"
    )


def test_a_needs_gated_fan_in_job_needs_no_marker():
    # GitHub skips a job whose needed job was skipped, so `report` inherits
    # its dependencies' gating and is not a run-on-every-cron job.
    assert cmg.violations(_needs_workflow()) == []


def test_a_needs_gated_job_answers_no_cron():
    # Non-vacuity for the inheritance rule's other half: `b` runs only when
    # `a` ran, so it must not silently mark the second cron as answered.
    text = textwrap.dedent(
        f"""\
        on:
          schedule:
            - cron: "{CRON_A}"
            - cron: "{CRON_B}"
        jobs:
          a:
            if: {_named(CRON_A)}
            runs-on: ubuntu-latest
            steps:
              - run: make
          b:
            needs: a
            runs-on: ubuntu-latest
            steps:
              - run: make
        """
    )
    found = cmg.violations(text)
    assert len(found) == 1
    assert f"cron '{CRON_B}'" in found[0][1]


def test_always_defeats_needs_inheritance():
    # `if: always()` runs the job even when its needs were skipped, so the
    # fan-in exemption does not apply and the marker obligation returns.
    found = cmg.violations(_needs_workflow(needs_if="always()"))
    assert len(found) == 1
    assert "'report'" in found[0][1]


# ── vacuous gates count as ungated ───────────────────────────────────────
@pytest.mark.parametrize("expr", ["always()", "${{ always() }}", "true"])
def test_a_vacuous_gate_is_judged_as_ungated(expr):
    text = _workflow(
        [CRON_A, CRON_B],
        [("eval", expr, None), ("weekly", _named(CRON_B), None)],
    )
    found = cmg.violations(text)
    assert len(found) == 1
    assert "multi-cron-ok" in found[0][1]


def test_a_vacuous_gate_passes_with_a_reasoned_marker():
    text = _workflow(
        [CRON_A, CRON_B],
        [
            ("eval", "always()", "# multi-cron-ok: a roll-up every schedule wants"),
            ("weekly", _named(CRON_B), None),
        ],
    )
    assert cmg.violations(text) == []


# ── negated and phantom cron literals ────────────────────────────────────
def test_a_negated_cron_comparison_does_not_name_that_cron():
    # `!= CRON_A` names the cron the job EXCLUDES; crediting it would mark
    # the one cron this job never runs on as covered.
    text = _workflow(
        [CRON_A, CRON_B],
        [("eval", f"github.event.schedule != '{CRON_A}'", None)],
    )
    messages = [msg for _, msg in cmg.violations(text)]
    assert len(messages) == 2
    assert any(f"cron '{CRON_A}'" in msg for msg in messages)
    assert any(f"cron '{CRON_B}'" in msg for msg in messages)


def test_an_undeclared_cron_literal_is_a_finding_on_the_job():
    # The typo case: the gate compares against a cron the file never
    # declares, so the job is permanently dead on schedule fires.
    text = _workflow(
        [CRON_A, CRON_B],
        [
            ("weekly", _named(CRON_A), None),
            ("eval", "github.event.schedule == '0 6 * * 2'", None),
        ],
    )
    messages = [msg for _, msg in cmg.violations(text)]
    assert len(messages) == 2
    assert any("'eval'" in msg and "'0 6 * * 2'" in msg for msg in messages)
    assert any(f"cron '{CRON_B}'" in msg for msg in messages)


def test_a_duplicated_cron_is_still_a_single_schedule():
    # Two listings of one expression give a job nothing to distinguish, so
    # the file stays exempt like any single-cron workflow.
    text = _workflow([CRON_A, CRON_A], [("eval", None, None)])
    assert cmg.violations(text) == []


# ── scope: only multi-cron workflow files ────────────────────────────────
@pytest.mark.parametrize(
    "on_block",
    [
        "on:\n  push:\n    branches: [main]\n",
        "on: [push, schedule]\n",
        "on: schedule\n",
        f'on:\n  schedule:\n    - cron: "{CRON_A}"\n',
    ],
)
def test_zero_or_one_cron_is_out_of_scope(on_block):
    text = f"name: X\n{on_block}jobs:\n  a:\n    steps:\n      - run: make\n"
    assert cmg.violations(text) == []


def test_unparseable_workflow_is_a_finding():
    found = cmg.violations("on: [schedule\njobs: {\n")
    assert len(found) == 1
    assert "could not parse as YAML" in found[0][1]


# ── main() over a fixture tree ───────────────────────────────────────────
def _tree(tmp_path: Path, monkeypatch, files: dict[str, str]) -> Path:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    for name, body in files.items():
        (wf_dir / name).write_text(body)
    monkeypatch.setattr(cmg, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cmg, "WORKFLOWS_DIR", wf_dir)
    return wf_dir


def test_main_is_clean_on_a_compliant_tree(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": _workflow(
                [CRON_A, CRON_B],
                [("eval", _named(CRON_A), None), ("weekly", _named(CRON_B), None)],
            ),
            "b.yaml": MARKED,
            "pr.yaml": "name: p\non:\n  pull_request:\njobs: {}\n",
        },
    )
    assert cmg.main() == 0
    assert capsys.readouterr().out == ""


def test_main_annotates_file_and_line(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"a.yaml": UNGATED})
    assert cmg.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/a.yaml,line=7::" in out
    assert "1 multi-cron-gating violation(s) found." in out


# ── crash resistance ─────────────────────────────────────────────────────
_FRAGMENTS = [
    "name: x\n",
    "on: schedule\n",
    f'on:\n  schedule:\n    - cron: "{CRON_A}"\n    - cron: "{CRON_B}"\n',
    "on:\n  schedule: notalist\n",
    "on:\n  schedule:\n    - notamapping\n",
    "on: [push, schedule]\n",
    "on: null\n",
    "jobs: {}\n",
    "jobs:\n  a:\n    if: github.event_name == 'schedule'\n",
    "jobs:\n  a:\n    needs: b\n",
    "jobs:\n  a:\n    if: always()\n    needs: [b, c]\n",
    "jobs:\n  a:\n    if: true\n",
    "jobs:\n  a: notamapping\n",
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
@given(text=_workflow_text())
def test_violations_never_crashes(text):
    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        assume(False)
    result = cmg.violations(text)
    assert all(isinstance(line, int) and isinstance(msg, str) for line, msg in result)
