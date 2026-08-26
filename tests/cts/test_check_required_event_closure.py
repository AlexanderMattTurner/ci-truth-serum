"""Tests for ci_truth_serum/check_required_event_closure.py — the (opinionated)
lint that walks each `# required-check: true` job's `needs` closure and flags
any job whose `if:` provably skips it on an event the check gates.

The three violation fixtures reproduce, minimized, the three real defects that
motivated the lint (agent-glovebox PR #3176): a required job whose own `if:`
excludes declared pull_request activity types; a `decide` job that skips in the
merge queue while its reporter still reports there; and a decide job admitting
only `pull_request` in a workflow that also fires on `merge_group`. The
pass fixtures pin the unknown-passes contract that keeps the false-positive
rate at zero: fork guards, `needs.decide.outputs.*` gates, and status
functions must never fire it.
"""

from pathlib import Path

import pytest

from tests._helpers import load_hook

crec = load_hook("check_required_event_closure.py", "check_required_event_closure")


def _check(tmp_path: Path, body: str) -> list[tuple[int | None, str]]:
    path = tmp_path / "wf.yaml"
    path.write_text(body, encoding="utf-8")
    return crec.check_file(path)


# ── the three real defect shapes fire ─────────────────────────────────────

REQUIRED_JOB_NARROWER_THAN_TYPES = """\
name: x
on:
  pull_request:
    types: [opened, synchronize, reopened, edited, closed]
jobs:
  changelog: # required-check: true
    if: >-
      github.event_name == 'pull_request' &&
      contains(fromJSON('["opened", "reopened", "synchronize"]'),
      github.event.action)
    runs-on: ubuntu-latest
"""

DECIDE_SKIPS_MERGE_QUEUE = """\
name: x
on:
  pull_request:
  merge_group:
jobs:
  decide:
    if: github.event_name != 'merge_group'
    runs-on: ubuntu-latest
  work:
    needs: [decide]
    if: needs.decide.outputs.run == 'true'
    runs-on: ubuntu-latest
  report: # required-check: true
    needs: [work]
    if: always()
    runs-on: ubuntu-latest
"""

DECIDE_ADMITS_ONLY_PULL_REQUEST = """\
name: x
on:
  pull_request:
  merge_group:
jobs:
  decide:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
  report: # required-check: true
    needs: decide
    if: always()
    runs-on: ubuntu-latest
"""


@pytest.mark.parametrize(
    ("body", "job", "excluded"),
    [
        (
            REQUIRED_JOB_NARROWER_THAN_TYPES,
            "changelog",
            "pull_request:edited, pull_request:closed",
        ),
        (DECIDE_SKIPS_MERGE_QUEUE, "decide", "merge_group"),
        (DECIDE_ADMITS_ONLY_PULL_REQUEST, "decide", "merge_group"),
    ],
)
def test_the_defect_shapes_fire(tmp_path, body, job, excluded):
    findings = _check(tmp_path, body)
    assert len(findings) == 1
    _, message = findings[0]
    assert f"job '{job}'" in message
    assert excluded in message


def test_the_finding_anchors_on_the_offending_job(tmp_path):
    (line, _) = _check(tmp_path, DECIDE_SKIPS_MERGE_QUEUE)[0]
    assert line == 6  # the `decide:` key line


# ── unknown passes: the shapes that must never fire ───────────────────────

FORK_GUARD = "github.event.pull_request.head.repo.full_name == github.repository"
DECIDE_OUTPUT_GATE = "needs.decide.outputs.run == 'true'"
STATUS_FUNCTION = "always() && needs.work.result != 'failure'"
UNBOUND_CONTEXT = "vars.DOCKER_USER != ''"


@pytest.mark.parametrize(
    "cond", [FORK_GUARD, DECIDE_OUTPUT_GATE, STATUS_FUNCTION, UNBOUND_CONTEXT]
)
def test_conditions_not_about_events_pass(tmp_path, cond):
    body = f"""\
name: x
on:
  pull_request:
  merge_group:
jobs:
  gated:
    if: {cond}
    runs-on: ubuntu-latest
  report: # required-check: true
    needs: [gated]
    if: always()
    runs-on: ubuntu-latest
"""
    assert _check(tmp_path, body) == []


def test_exclusion_of_a_non_gating_event_passes(tmp_path):
    # push/schedule runs satisfy no required check, so skipping there is honest.
    body = """\
name: x
on:
  pull_request:
  push:
    branches: [main]
jobs:
  work: # required-check: true
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
"""
    assert _check(tmp_path, body) == []


def test_a_job_outside_every_required_closure_passes(tmp_path):
    body = """\
name: x
on:
  pull_request:
  merge_group:
jobs:
  advisory:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
  report: # required-check: true
    if: always()
    runs-on: ubuntu-latest
"""
    assert _check(tmp_path, body) == []


def test_an_event_scoped_job_admitting_every_gating_pair_passes(tmp_path):
    body = """\
name: x
on:
  pull_request:
  merge_group:
jobs:
  work: # required-check: true
    if: github.event_name == 'pull_request' || github.event_name == 'merge_group'
    runs-on: ubuntu-latest
"""
    assert _check(tmp_path, body) == []


def test_a_wrapped_expression_is_unwrapped(tmp_path):
    body = """\
name: x
on:
  pull_request:
  merge_group:
jobs:
  work: # required-check: true
    if: ${{ github.event_name == 'pull_request' }}
    runs-on: ubuntu-latest
"""
    findings = _check(tmp_path, body)
    assert len(findings) == 1
    assert "merge_group" in findings[0][1]


# ── the opt-out marker ────────────────────────────────────────────────────


def test_the_marker_with_a_reason_suppresses(tmp_path):
    body = DECIDE_SKIPS_MERGE_QUEUE.replace(
        "  decide:",
        "  # event-scoped-ok: the reporter fails the batch when decide skipped\n"
        "  decide:",
    )
    assert _check(tmp_path, body) == []


def test_a_bare_marker_does_not_suppress(tmp_path):
    body = DECIDE_SKIPS_MERGE_QUEUE.replace(
        "  decide:", "  # event-scoped-ok:\n  decide:"
    )
    assert len(_check(tmp_path, body)) == 1


# ── fail-closed inputs ────────────────────────────────────────────────────


def test_unparseable_yaml_is_itself_a_violation(tmp_path):
    findings = _check(tmp_path, "on: [pull_request\njobs: {")
    assert len(findings) == 1
    assert findings[0][0] is None
    assert "could not parse as YAML" in findings[0][1]


def test_a_function_call_evaluates_to_unknown_and_passes(tmp_path):
    body = """\
name: x
on:
  pull_request:
jobs:
  work: # required-check: true
    if: startsWith(format('{0}', github.event_name), 'pull')
    runs-on: ubuntu-latest
"""
    assert _check(tmp_path, body) == []


def test_an_unreadable_if_on_a_closure_job_is_a_violation(tmp_path):
    body = """\
name: x
on:
  pull_request:
jobs:
  work: # required-check: true
    if: (github.event_name == 'pull_request'
    runs-on: ubuntu-latest
"""
    findings = _check(tmp_path, body)
    assert len(findings) == 1
    assert "could not be parsed" in findings[0][1]


def test_an_unreadable_if_outside_every_closure_passes(tmp_path):
    body = """\
name: x
on:
  pull_request:
jobs:
  advisory:
    if: (github.event_name == 'x'
    runs-on: ubuntu-latest
  work: # required-check: true
    runs-on: ubuntu-latest
"""
    assert _check(tmp_path, body) == []


# ── the expression evaluator ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("cond", "event", "action", "verdict"),
    [
        ("github.event_name == 'merge_group'", "pull_request", "opened", False),
        ("github.event_name != 'merge_group'", "merge_group", None, False),
        ("github.event_name != 'merge_group'", "pull_request", "opened", True),
        ("!(github.event_name == 'pull_request')", "pull_request", "opened", False),
        (
            "contains(fromJSON('[\"opened\"]'), github.event.action)",
            "pull_request",
            "edited",
            False,
        ),
        (
            "contains(fromJSON('[\"opened\"]'), github.event.action)",
            "pull_request",
            "opened",
            True,
        ),
        # Unknown wins comparisons it takes part in, and survives &&/|| unless
        # logic decides without it.
        ("github.ref == 'refs/heads/main'", "pull_request", "opened", "unknown"),
        (
            "vars.X == 'y' && github.event_name == 'push'",
            "pull_request",
            "opened",
            False,
        ),
        (
            "vars.X == 'y' || github.event_name == 'pull_request'",
            "pull_request",
            "opened",
            True,
        ),
        (
            "vars.X == 'y' && github.event_name == 'pull_request'",
            "pull_request",
            "opened",
            "unknown",
        ),
        # Relational operators are out of evaluation scope: unknown, never a guess.
        ("github.event.pull_request.commits > 1", "pull_request", "opened", "unknown"),
    ],
)
def test_truth_of(cond, event, action, verdict):
    env = {"github.event_name": event}
    if action is not None:
        env["github.event.action"] = action
    tree = crec._Parser(cond).parse()
    assert crec.truth_of(tree, env) is (
        crec._UNKNOWN if verdict == "unknown" else verdict
    )


def test_default_activity_types_apply_when_none_declared(tmp_path):
    # `on: pull_request:` with no types fires only opened/synchronize/reopened,
    # so excluding `edited` there is not an exclusion at all.
    body = """\
name: x
on:
  pull_request:
jobs:
  work: # required-check: true
    if: github.event.action != 'edited'
    runs-on: ubuntu-latest
"""
    assert _check(tmp_path, body) == []


def test_gating_pairs_reads_scalar_list_and_mapping_forms():
    assert crec.gating_pairs("merge_group") == [("merge_group", None)]
    assert crec.gating_pairs(["merge_group", "push"]) == [("merge_group", None)]
    assert crec.gating_pairs(
        {"pull_request": {"types": ["edited"]}, "schedule": None}
    ) == [("pull_request", "edited")]


def test_needs_closure_walks_transitively_and_accepts_string_needs():
    jobs = {
        "a": {},
        "b": {"needs": "a"},
        "c": {"needs": ["b"]},
        "d": {},
    }
    assert crec.needs_closure(jobs, "c") == {"a", "b", "c"}


# ── main() ────────────────────────────────────────────────────────────────


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "bad.yaml").write_text(DECIDE_SKIPS_MERGE_QUEUE, encoding="utf-8")
    monkeypatch.setattr(crec, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(crec, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(crec, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    assert crec.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yaml,line=6::" in out

    (wf / "bad.yaml").write_text(
        DECIDE_ADMITS_ONLY_PULL_REQUEST.replace(
            "github.event_name == 'pull_request'",
            "github.event_name == 'pull_request' || github.event_name == 'merge_group'",
        ),
        encoding="utf-8",
    )
    assert crec.main() == 0
