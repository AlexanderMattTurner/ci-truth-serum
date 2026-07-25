"""Tests for ci_truth_serum/_failure_routing.py — the one predicate answering
"is this workflow's failure routed to a human?", consumed by both
check_cron_alert_coverage and check_failure_notifier_coverage.

Everything is driven through the real functions on synthetic workflow YAML: the
parametrized gate tables walk every accepted and every rejected gate shape
member by member, and each of the four routes gets a pair — one workflow that
takes it, and the same workflow with that route removed — so no case can pass
because the predicate is simply lenient.

The two consumer predicates are deliberately asymmetric (`unrouted_scheduled`
does not credit the PR surface; `needs_tree_notifier` does not credit being
watched), and that asymmetry is pinned here, at its source.
"""

import pytest
import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

routing = load_hook("_failure_routing.py", "_failure_routing")
linecheck = load_hook("_linecheck.py", "_linecheck_for_routing")

MATCHER = linecheck.notifier_matcher([])


def _doc(text: str) -> dict:
    return yaml.load(text, Loader=linecheck.LineLoader)


def _route(text: str, watched: set[str] | None = None):
    return routing.routing(_doc(text), text, MATCHER, watched)


BARE_CRON = "name: Nightly\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs: {}\n"
BARE_PUSH = "name: Nightly\non:\n  push:\n    branches: [main]\njobs: {}\n"


def _with_notify(gate: str) -> str:
    """BARE_CRON plus a notification step behind GATE."""
    return (
        "name: Nightly\non:\n  schedule:\n    - cron: '0 3 * * *'\n"
        "jobs:\n  work:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: make check\n      - name: Notify\n"
        f"        if: {gate}\n        uses: ./.github/actions/notify-ntfy\n"
    )


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
    assert routing.gate_direction(gate) == routing.REACHABLE


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
    assert routing.gate_direction(gate) == routing.BLOCKED


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
    assert routing.gate_direction(gate) == routing.NEUTRAL


def test_direction_is_read_from_the_comparison_not_the_mere_mention():
    # Both gates name `needs.build.result`; only the direction differs, and the
    # direction is the whole finding.
    assert (
        routing.gate_direction("needs.build.result != 'success'") == routing.REACHABLE
    )
    assert routing.gate_direction("needs.build.result == 'success'") == routing.BLOCKED


# ── route 1: a self-notification a failure can reach ─────────────────────
def test_self_notify_is_credited_only_when_a_failure_reaches_it():
    assert _route(_with_notify("failure()")).self_notify is True
    assert _route(_with_notify("success()")).self_notify is False
    assert _route(BARE_CRON).self_notify is False


def test_an_ungated_notify_step_is_not_a_route():
    # GitHub abandons a job at its first failed step, so a trailing ungated
    # notify never runs on the failure it exists to report.
    text = _with_notify("failure()").replace("        if: failure()\n", "")
    assert _route(text).self_notify is False


# ── route 2: some notifier in the tree watches this workflow ─────────────
def test_watched_is_membership_in_the_notifiers_union():
    assert _route(BARE_CRON, {"Nightly"}).watched is True
    assert _route(BARE_CRON, {"nightly"}).watched is False
    assert _route(BARE_CRON, set()).watched is False


def test_an_unnamed_workflow_is_never_credited_as_watched():
    text = "on:\n  schedule:\n    - cron: '0 3 * * *'\njobs: {}\n"
    assert _route(text, {"Nightly"}).watched is False


NOTIFIER = (
    "name: CI failure notify\non:\n  workflow_run:\n    workflows:\n"
    '      - "Alpha"\n    types: [completed]\njobs: {}\n'
)


def test_watched_names_unions_every_notifier_and_skips_non_notifiers():
    second = (
        "name: Slack alerts\non:\n  workflow_run:\n    workflows:\n"
        '      - "Beta"\n    types: [completed]\njobs: {}\n'
    )
    # A workflow_run consumer with no sink observes the run but tells nobody.
    collector = (
        "name: Collect artifacts\non:\n  workflow_run:\n    workflows:\n"
        '      - "Gamma"\n    types: [completed]\njobs: {}\n'
    )
    docs = [_doc(NOTIFIER), _doc(second), _doc(collector)]
    assert routing.watched_names(docs, MATCHER) == {"Alpha", "Beta"}


def test_is_notifier_needs_both_the_trigger_and_the_sink():
    assert routing.is_notifier(_doc(NOTIFIER), MATCHER) is True
    # A push workflow that happens to Slack is monitored, not the notifier.
    sink_on_push = (
        "name: Slack digest\non:\n  push:\n    branches: [main]\n"
        "jobs:\n  tell:\n    steps:\n      - uses: ./.github/actions/notify-ntfy\n"
    )
    assert routing.is_notifier(_doc(sink_on_push), MATCHER) is False


# ── route 3: the failure lands on a pull request ─────────────────────────
@pytest.mark.parametrize("trigger", ["pull_request", "pull_request_target"])
def test_pr_surface_is_credited_for_either_pr_trigger(trigger):
    text = f"name: Lint\non:\n  push:\n    branches: [main]\n  {trigger}:\njobs: {{}}\n"
    assert _route(text).pr_surface is True
    assert _route(BARE_PUSH).pr_surface is False


# ── route 4: the reasoned opt-out marker ─────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "name: N\non:\n  schedule:\n    # cron-alert: false  # opens a PR only\n"
        "    - cron: '0 3 * * *'\njobs: {}\n",
        "name: N\non:\n  schedule:  # cron-alert: false  # opens a PR only\n"
        "    - cron: '0 3 * * *'\njobs: {}\n",
        "name: N\non:\n  push:  # cron-alert: false  # cosmetic badges only\n"
        "    branches: [main]\njobs: {}\n",
    ],
    ids=["schedule-child", "schedule-key", "push-key"],
)
def test_a_reasoned_marker_on_a_monitored_trigger_key_opts_out(text):
    route = _route(text)
    assert route.opted_out is True
    assert route.marker_error is None


@pytest.mark.parametrize("reason", ["", "n/a", "none"])
def test_a_marker_without_a_reason_is_not_an_opt_out(reason):
    text = (
        f"name: N\non:\n  schedule:\n    # cron-alert: false  {reason}\n"
        "    - cron: '0 3 * * *'\njobs: {}\n"
    )
    route = _route(text)
    assert route.opted_out is False
    assert route.marker_error is not None


def test_a_marker_inside_a_step_is_not_a_classification_of_the_trigger():
    text = (
        "name: N\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n"
        "  work:\n    steps:\n      - run: make  # cron-alert: false  # smuggled\n"
    )
    route = _route(text)
    assert (route.opted_out, route.marker_error) == (False, None)


# ── the asymmetry between the two consumer predicates ────────────────────
def test_the_pr_surface_excuses_the_notifier_list_but_never_a_cron():
    text = "name: Both\non:\n  schedule:\n    - cron: '0 3 * * *'\n  pull_request:\njobs: {}\n"
    route = _route(text)
    # A cron fire has no pull request, so the PR check says nothing about it.
    assert routing.unrouted_scheduled(route) is True
    assert routing.needs_tree_notifier(route) is False


def test_being_watched_excuses_a_cron_but_is_not_a_route_for_the_list():
    route = _route(BARE_CRON, {"Nightly"})
    assert routing.unrouted_scheduled(route) is False
    # The list must not be allowed to justify its own contents.
    assert routing.needs_tree_notifier(route) is True


@pytest.mark.parametrize(
    "text",
    [
        _with_notify("failure()"),
        "name: N\non:\n  schedule:\n    # cron-alert: false  # opens a PR only\n"
        "    - cron: '0 3 * * *'\njobs: {}\n",
    ],
    ids=["self-notify", "marker"],
)
def test_the_routes_both_predicates_share(text):
    route = _route(text)
    assert routing.unrouted_scheduled(route) is False
    assert routing.needs_tree_notifier(route) is False


def test_a_workflow_with_no_route_at_all_is_unrouted_for_both():
    route = _route(BARE_CRON)
    assert routing.unrouted_scheduled(route) is True
    assert routing.needs_tree_notifier(route) is True


# ── crash resistance ─────────────────────────────────────────────────────
_FRAGMENTS = [
    "name: x\n",
    "on: schedule\n",
    "on:\n  schedule:\n    - cron: '0 0 * * 0'\n",
    "on:\n  schedule:\n    # cron-alert: false\n    - cron: '0 0 * * 0'\n",
    "on:\n  push:  # cron-alert: false  # because\n    branches: [main]\n",
    "on: [push, pull_request]\n",
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
@given(text=_workflow_text(), watched=st.booleans())
def test_routing_never_crashes(text, watched):
    # The predicate reads a parsed document AND the raw source (the opt-out
    # marker is a comment the parser drops), so it is fuzzed on both at once.
    try:
        doc = yaml.load(text, Loader=linecheck.LineLoader)
    except yaml.YAMLError:
        assume(False)
    assume(isinstance(doc, dict))
    route = routing.routing(doc, text, MATCHER, {"x"} if watched else set())
    assert isinstance(route.self_notify, bool)
    assert isinstance(routing.needs_tree_notifier(route), bool)
    assert isinstance(routing.unrouted_scheduled(route), bool)
