"""Tests for ci_truth_serum/check_failure_notifier_coverage.py — the freshness check
keeping a failure notifier's `on.workflow_run.workflows` list (a derived copy,
since workflow_run has no wildcard) in sync with the tree's set of
push/schedule-triggered workflow names.

The notifier is DISCOVERED (a workflow_run trigger plus a named notification
sink), never matched by filename, so the cases below name notifier files several
different things on purpose — a lint that recognized only one basename passed
vacuously everywhere else.

Fixture workflow trees in tmp dirs drive the real hook code through ``main()``;
discovery is redirected at the module's dir constants so the real repo's
workflows never leak into a case.
"""

import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

cfnc = load_hook(
    "check_failure_notifier_coverage.py", "check_failure_notifier_coverage"
)


PUSH_WF = "name: {name}\non:\n  push:\n    branches: [main]\njobs: {{}}\n"
SCHEDULE_WF = "name: {name}\non:\n  schedule:\n    - cron: '0 0 * * 0'\njobs: {{}}\n"
PR_ONLY_WF = "name: {name}\non:\n  pull_request:\njobs: {{}}\n"

# ── one fixture per route to a human the residual predicate accepts ──────
# Route 3: a failure lands as a check run on the PR that caused it.
PUSH_AND_PR_WF = (
    "name: {name}\non:\n  push:\n    branches: [main]\n  pull_request:\njobs: {{}}\n"
)
# Route 4: the sanctioned opt-out, on the schedule key and on the push key.
MARKED_SCHEDULE_WF = (
    "name: {name}\non:\n  schedule:\n"
    "    # cron-alert: false  # {reason}\n    - cron: '0 0 * * 0'\njobs: {{}}\n"
)
MARKED_PUSH_WF = (
    "name: {name}\non:\n  push:  # cron-alert: false  # {reason}\n"
    "    branches: [main]\njobs: {{}}\n"
)


def _self_notifying(name: str, gate: str = "failure()") -> str:
    """Route 1: a scheduled workflow carrying its own notification step, GATE
    deciding whether a failure can actually reach it."""
    return (
        f"name: {name}\non:\n  schedule:\n    - cron: '0 0 * * 0'\n"
        "jobs:\n  work:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: make check\n      - name: Notify\n"
        f"        if: {gate}\n        uses: ./.github/actions/notify-ntfy\n"
    )


# ── job bodies, one per place discovery is allowed to find a sink ─────────
NO_SINK_JOBS = textwrap.dedent(
    """\
    jobs:
      collect:
        runs-on: ubuntu-latest
        steps:
          - uses: codecov/codecov-action@v4
    """
)
STEP_USES_SINK_JOBS = textwrap.dedent(
    """\
    jobs:
      tell:
        runs-on: ubuntu-latest
        steps:
          - uses: ./.github/actions/notify-ntfy
    """
)
STEP_NAME_SINK_JOBS = textwrap.dedent(
    """\
    jobs:
      tell:
        runs-on: ubuntu-latest
        steps:
          - name: Post to Slack
            run: ./bin/announce
    """
)
STEP_RUN_SINK_JOBS = textwrap.dedent(
    """\
    jobs:
      tell:
        runs-on: ubuntu-latest
        steps:
          - name: Announce
            run: bash scripts/manage-release-failure-issue.sh open
    """
)
JOB_USES_SINK_JOBS = textwrap.dedent(
    """\
    jobs:
      tell:
        uses: ./.github/workflows/reusable-slack-alert.yaml
    """
)
HOUSE_SINK_JOBS = textwrap.dedent(
    """\
    jobs:
      tell:
        runs-on: ubuntu-latest
        steps:
          - uses: ./.github/actions/tell-the-humans
    """
)


def _wf_run(
    name: str,
    workflows: list[str] | None = None,
    jobs: str = "jobs: {}\n",
    also_push: bool = False,
) -> str:
    """A `workflow_run`-triggered workflow document.

    WORKFLOWS is the `on.workflow_run.workflows` list (None omits the key
    entirely, which the lint treats as a malformed notifier); ALSO_PUSH adds a
    push trigger so the same file is both a notifier and monitorable.
    """
    lines = [f"name: {name}", "on:"]
    if also_push:
        lines += ["  push:", "    branches: [main]"]
    lines += ["  workflow_run:"]
    if workflows == []:
        lines += ["    workflows: []"]
    elif workflows is not None:
        lines += ["    workflows:"] + [f'      - "{w}"' for w in workflows]
    lines += ["    types: [completed]"]
    return "\n".join(lines) + "\n" + jobs


def _notifier(names: list[str]) -> str:
    """The default notifier fixture: a workflow_run workflow whose own `name:`
    carries the sink word, so discovery finds it via the top-level name."""
    return _wf_run("CI failure notify", names)


def _tree(tmp_path: Path, monkeypatch, files: dict[str, str]) -> Path:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    for name, body in files.items():
        (wf_dir / name).write_text(body, encoding="utf-8")
    monkeypatch.setattr(cfnc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cfnc, "WORKFLOWS_DIR", wf_dir)
    return wf_dir


def _actions(tmp_path: Path, monkeypatch, actions: dict[str, str]) -> Path:
    """Write `.github/actions/<name>/action.yaml` for each entry and redirect the
    module's ACTIONS_DIR at the fixture tree.

    Separate from `_tree` (which only builds `.github/workflows/`) so the
    workflow-only cases keep resolving local `uses:` against a directory that
    isn't there — the "action not on disk" path the priority check must survive.
    """
    actions_dir = tmp_path / ".github" / "actions"
    actions_dir.mkdir(parents=True, exist_ok=True)
    for name, body in actions.items():
        (actions_dir / name).mkdir(parents=True)
        (actions_dir / name / "action.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(cfnc, "ACTIONS_DIR", actions_dir)
    return actions_dir


# ── composite actions, one per shape the priority rule keys on ───────────
OPTIONAL_PRIORITY_ACTION = textwrap.dedent(
    """\
    name: Notify via ntfy
    inputs:
      priority:
        required: false
        default: "5"
      tags:
        required: false
        default: rotating_light
    runs:
      using: composite
      steps:
        - run: true
          shell: bash
    """
)
REQUIRED_PRIORITY_ACTION = textwrap.dedent(
    """\
    name: Notify via ntfy
    inputs:
      priority:
        required: true
        description: urgency
    runs:
      using: composite
      steps:
        - run: true
          shell: bash
    """
)
NO_PRIORITY_ACTION = textwrap.dedent(
    """\
    name: Upload something
    inputs:
      path:
        required: false
        default: dist
    runs:
      using: composite
      steps:
        - run: true
          shell: bash
    """
)

ALERT_STEP_NAME = "Alert on failure"


def _alert_wf(
    uses: str = "./.github/actions/notify-ntfy",
    with_lines: list[str] | None = None,
    name: str = "Alpha",
) -> str:
    """A push workflow whose second step invokes USES, optionally with a `with:`
    block (None omits the key entirely). The first step is an unrelated published
    action so the flagged line is never the first line of the file."""
    lines = [
        f"name: {name}",
        "on:",
        "  push:",
        "    branches: [main]",
        "jobs:",
        "  build:",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - name: Checkout",
        "        uses: actions/checkout@v4",
        f"      - name: {ALERT_STEP_NAME}",
        f"        uses: {uses}",
    ]
    if with_lines is not None:
        lines += ["        with:"] + [f"          {entry}" for entry in with_lines]
    return "\n".join(lines) + "\n"


def _line_of(text: str, needle: str) -> int:
    """The 1-based line of the first line in TEXT containing NEEDLE — the fixture's
    own ground truth for what a line-anchored annotation must point at."""
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return index
    raise AssertionError(f"{needle!r} not in fixture")


def _main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["check_failure_notifier_coverage", *argv])
    return cfnc.main()


# ── exact match passes ───────────────────────────────────────────────────
def test_exact_list_passes(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "b.yaml": SCHEDULE_WF.format(name="Beta"),
            "pr.yaml": PR_ONLY_WF.format(name="PR only"),
            "ci-failure-notify.yaml": _notifier(["Alpha", "Beta"]),
        },
    )
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_watching_more_than_the_residual_is_not_stale(tmp_path, monkeypatch, capsys):
    # Staleness means "names no workflow in the tree", never "sits outside the
    # required set". A repo is allowed to watch a workflow it does not have to:
    # a PR-only one, and one that already alerts on its own.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "pr.yaml": PR_ONLY_WF.format(name="PR only"),
            "self.yaml": _self_notifying("Self notifier"),
            "ci-failure-notify.yaml": _notifier(["Alpha", "PR only", "Self notifier"]),
        },
    )
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


# ── the residual: only workflows nothing else routes must be listed ──────
def _residual_tree(tmp_path, monkeypatch, body: str) -> None:
    """A tree holding one candidate workflow and an empty notifier, so the only
    thing `main` can report is whether the candidate is in the residual."""
    _tree(
        tmp_path,
        monkeypatch,
        {"cand.yaml": body, "ci-failure-notify.yaml": _notifier([])},
    )


@pytest.mark.parametrize(
    "body",
    [
        _self_notifying("Cand"),
        PUSH_AND_PR_WF.format(name="Cand"),
        MARKED_SCHEDULE_WF.format(name="Cand", reason="opens a PR only; retries daily"),
        MARKED_PUSH_WF.format(name="Cand", reason="advisory badge refresh only"),
    ],
    ids=["self-notify", "pr-surface", "marker-on-schedule", "marker-on-push"],
)
def test_a_workflow_routed_to_a_human_is_not_demanded(
    tmp_path, monkeypatch, capsys, body
):
    # One case per route in _failure_routing. Each already reaches a human, so
    # the notifier is not required to watch it as well.
    _residual_tree(tmp_path, monkeypatch, body)
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "body",
    [
        SCHEDULE_WF.format(name="Cand"),
        PUSH_WF.format(name="Cand"),
        _self_notifying("Cand", gate="success()"),
        MARKED_SCHEDULE_WF.format(name="Cand", reason=""),
        MARKED_SCHEDULE_WF.format(name="Cand", reason="n/a"),
    ],
    ids=[
        "bare-schedule",
        "bare-push",
        "notify-behind-a-success-gate",
        "reasonless-marker",
        "placeholder-marker",
    ],
)
def test_a_workflow_routing_its_failure_nowhere_is_demanded(
    tmp_path, monkeypatch, capsys, body
):
    # Non-vacuity for the cases above: strip the route (or make it unreachable,
    # or state no reason for the opt-out) and the same predicate goes red naming
    # the workflow. A notify step behind `if: success()` reads as coverage in
    # review and fires never on the path it exists for.
    _residual_tree(tmp_path, monkeypatch, body)
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "missing (fails silently): ['Cand']" in out


def test_an_opted_out_workflow_the_notifier_also_watches_is_clean(
    tmp_path, monkeypatch, capsys
):
    # The redundant-but-not-contradictory case: a workflow both listed and
    # marked. The marker keeps it out of the residual, and the listing is not
    # stale — it names a workflow that really exists.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "sync.yaml": MARKED_SCHEDULE_WF.format(
                name="Sync from Template", reason="opens a PR; tomorrow's fire retries"
            ),
            "ci-failure-notify.yaml": _notifier(["Sync from Template"]),
        },
    )
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_the_suggested_block_keeps_the_extras_the_repo_already_watches(
    tmp_path, monkeypatch, capsys
):
    # The corrected block is not the residual: it is what the list should hold —
    # every real name already watched, plus the unrouted one that is missing.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "pr.yaml": PUSH_AND_PR_WF.format(name="Lint checks"),
            "cron.yaml": SCHEDULE_WF.format(name="Nightly"),
            "ci-failure-notify.yaml": _notifier(["Lint checks", "Ghost"]),
        },
    )
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "missing (fails silently): ['Nightly']" in out
    assert "stale (matches nothing): ['Ghost']" in out
    assert '    workflows:\n      - "Lint checks"\n      - "Nightly"' in out


# ── stale list fails with a copy-paste corrected block ───────────────────
def test_stale_list_fails_with_suggested_block(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "b.yaml": PUSH_WF.format(name="Beta"),
            "ci-failure-notify.yaml": _notifier(["Alpha", "Gone"]),
        },
    )
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "missing (fails silently): ['Beta']" in out
    assert "stale (matches nothing): ['Gone']" in out
    assert '    workflows:\n      - "Alpha"\n      - "Beta"' in out
    assert "::error file=.github/workflows/ci-failure-notify.yaml::" in out


def test_duplicate_entries_fail(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "ci-failure-notify.yaml": _notifier(["Alpha", "Alpha"]),
        },
    )
    assert _main(monkeypatch) == 1
    assert "duplicates present" in capsys.readouterr().out


# ── the vacuity regression: the notifier is discovered, not named ────────
def test_stale_notifier_under_a_nonstandard_filename_fails(
    tmp_path, monkeypatch, capsys
):
    # The headline case. A repo whose notifier is not called
    # `ci-failure-notify.yaml` must still be checked: a lint matching one
    # hardcoded basename found nothing here and reported success.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "b.yaml": PUSH_WF.format(name="Beta"),
            "build-publish-notify.yaml": _wf_run(
                "Build, publish and notify", ["Alpha", "Gone"]
            ),
        },
    )
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/build-publish-notify.yaml::" in out
    assert "missing (fails silently): ['Beta']" in out
    assert "stale (matches nothing): ['Gone']" in out


def test_correct_notifier_under_a_nonstandard_filename_passes(
    tmp_path, monkeypatch, capsys
):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "b.yaml": PUSH_WF.format(name="Beta"),
            "build-publish-notify.yaml": _wf_run(
                "Build, publish and notify", ["Alpha", "Beta"]
            ),
        },
    )
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


# ── discovery predicate: every place a sink may be named ─────────────────
def _discovery_tree(tmp_path, monkeypatch, notifier_text: str) -> None:
    # `Alpha` is monitored but unlisted, so the tree is a violation IFF the
    # candidate was recognized as a notifier — discovery is what the exit code
    # reports on, with no reliance on the candidate's filename.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "candidate.yaml": notifier_text,
        },
    )


def test_sink_in_top_level_name_is_discovered(tmp_path, monkeypatch, capsys):
    _discovery_tree(tmp_path, monkeypatch, _wf_run("Nightly ntfy alert", []))
    assert _main(monkeypatch) == 1
    assert "missing (fails silently): ['Alpha']" in capsys.readouterr().out


def test_sink_in_step_uses_is_discovered(tmp_path, monkeypatch, capsys):
    _discovery_tree(
        tmp_path, monkeypatch, _wf_run("Post run", [], jobs=STEP_USES_SINK_JOBS)
    )
    assert _main(monkeypatch) == 1
    assert "missing (fails silently): ['Alpha']" in capsys.readouterr().out


def test_sink_in_step_name_is_discovered(tmp_path, monkeypatch, capsys):
    _discovery_tree(
        tmp_path, monkeypatch, _wf_run("Post run", [], jobs=STEP_NAME_SINK_JOBS)
    )
    assert _main(monkeypatch) == 1
    assert "missing (fails silently): ['Alpha']" in capsys.readouterr().out


def test_sink_in_step_run_is_discovered(tmp_path, monkeypatch, capsys):
    _discovery_tree(
        tmp_path, monkeypatch, _wf_run("Post run", [], jobs=STEP_RUN_SINK_JOBS)
    )
    assert _main(monkeypatch) == 1
    assert "missing (fails silently): ['Alpha']" in capsys.readouterr().out


def test_sink_in_job_uses_is_discovered(tmp_path, monkeypatch, capsys):
    # A reusable notifier workflow: the calling job has no steps at all.
    _discovery_tree(
        tmp_path, monkeypatch, _wf_run("Post run", [], jobs=JOB_USES_SINK_JOBS)
    )
    assert _main(monkeypatch) == 1
    assert "missing (fails silently): ['Alpha']" in capsys.readouterr().out


def test_workflow_run_without_a_sink_is_not_the_notifier(tmp_path, monkeypatch, capsys):
    # An unrelated workflow_run consumer (here a coverage uploader) must not be
    # held to the exhaustiveness invariant: `Alpha` goes unreported. The tree has
    # no notifier at all, so `--allow-no-notifier` isolates that invariant from
    # the fail-closed one the next test covers.
    _discovery_tree(
        tmp_path, monkeypatch, _wf_run("Coverage upload", [], jobs=NO_SINK_JOBS)
    )
    assert _main(monkeypatch, "--allow-no-notifier") == 0
    assert capsys.readouterr().out == ""


def test_workflow_run_without_a_sink_leaves_the_repo_notifierless(
    tmp_path, monkeypatch, capsys
):
    _discovery_tree(
        tmp_path, monkeypatch, _wf_run("Coverage upload", [], jobs=NO_SINK_JOBS)
    )
    assert _main(monkeypatch) == 1
    assert "no failure-notifier workflow found" in capsys.readouterr().out


def test_sink_without_workflow_run_is_monitored_not_the_notifier(
    tmp_path, monkeypatch, capsys
):
    # A push-triggered workflow that happens to Slack: not a notifier, so it
    # belongs in the EXPECTED set the real notifier must list.
    files = {
        "digest.yaml": PUSH_WF.format(name="Slack digest"),
        "ci-failure-notify.yaml": _notifier([]),
    }
    _tree(tmp_path, monkeypatch, files)
    assert _main(monkeypatch) == 1
    assert "missing (fails silently): ['Slack digest']" in capsys.readouterr().out

    files["ci-failure-notify.yaml"] = _notifier(["Slack digest"])
    _tree(tmp_path / "listed", monkeypatch, files)
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


# ── --notifier-pattern extends (never replaces) the built-in sinks ───────
def test_house_sink_needs_notifier_pattern_to_be_discovered(
    tmp_path, monkeypatch, capsys
):
    _discovery_tree(
        tmp_path, monkeypatch, _wf_run("Post run", [], jobs=HOUSE_SINK_JOBS)
    )
    assert _main(monkeypatch) == 1
    assert "no failure-notifier workflow found" in capsys.readouterr().out


def test_notifier_pattern_discovers_the_house_sink(tmp_path, monkeypatch, capsys):
    _discovery_tree(
        tmp_path, monkeypatch, _wf_run("Post run", [], jobs=HOUSE_SINK_JOBS)
    )
    assert _main(monkeypatch, "--notifier-pattern", "tell-the-humans") == 1
    assert "missing (fails silently): ['Alpha']" in capsys.readouterr().out


def test_notifier_pattern_does_not_displace_default_sinks(
    tmp_path, monkeypatch, capsys
):
    # Both notifiers must be recognized in the same run: the house one (via the
    # extra pattern) and the default one (via `notify` in its name), each judged
    # for staleness against its own file.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "house.yaml": _wf_run("Post run", ["Alpha"], jobs=HOUSE_SINK_JOBS),
            "ci-failure-notify.yaml": _notifier(["Alpha", "Ghost"]),
        },
    )
    assert _main(monkeypatch, "--notifier-pattern", "tell-the-humans") == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/ci-failure-notify.yaml::" in out
    assert "stale (matches nothing): ['Ghost']" in out
    assert "house.yaml" not in out
    assert "missing (fails silently)" not in out


# ── a notifier is never required to list itself ──────────────────────────
def test_notifier_with_a_push_trigger_need_not_list_itself(
    tmp_path, monkeypatch, capsys
):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "ci-failure-notify.yaml": _wf_run(
                "CI failure notify", ["Alpha"], also_push=True
            ),
        },
    )
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


# ── more than one notifier: coverage is their union ──────────────────────
def _two_notifiers(first: list[str], second: list[str], extra: dict) -> dict:
    return {
        "a.yaml": PUSH_WF.format(name="Alpha"),
        "b.yaml": PUSH_WF.format(name="Beta"),
        "n1.yaml": _wf_run("CI failure notify", first),
        "n2.yaml": _wf_run("Nightly ntfy alert", second),
        **extra,
    }


def test_two_notifiers_splitting_the_tree_pass(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, _two_notifiers(["Alpha"], ["Beta"], {}))
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_workflow_covered_by_neither_notifier_is_missing(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        _two_notifiers(["Alpha"], ["Beta"], {"c.yaml": PUSH_WF.format(name="Gamma")}),
    )
    assert _main(monkeypatch) == 1
    assert "missing (fails silently): ['Gamma']" in capsys.readouterr().out


def test_staleness_is_judged_per_notifier_file(tmp_path, monkeypatch, capsys):
    # `Ghost` is dead wherever it sits, so it is reported against the file that
    # carries it — not against the sibling notifier whose list is clean.
    _tree(tmp_path, monkeypatch, _two_notifiers(["Alpha"], ["Beta", "Ghost"], {}))
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/n2.yaml::" in out
    assert "stale (matches nothing): ['Ghost']" in out
    assert "n1.yaml" not in out
    assert "missing (fails silently)" not in out


# ── missing notifier: FAILS CLOSED by default, opt out to pass ───────────
# A tree with no discoverable notifier exits 1 by DEFAULT: a green here would be
# one the check never earned, since the thing it verifies is absent — which is
# exactly how a notifier gets deleted, renamed, or never synced without anyone
# noticing. `--allow-no-notifier` is the stated decision that restores the pass.
def test_missing_notifier_fails_closed_by_default(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "b.yaml": PUSH_WF.format(name="Beta"),
        },
    )
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "no failure-notifier workflow found under .github/workflows/" in out
    # The message must name the filename to create, or it isn't actionable.
    assert "ci-failure-notify.yaml" in out
    assert "expected `ci-failure-notify.yaml`" in out


def test_allow_no_notifier_restores_the_pass(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "b.yaml": PUSH_WF.format(name="Beta"),
        },
    )
    assert _main(monkeypatch, "--allow-no-notifier") == 0
    assert capsys.readouterr().out == ""


def test_notifier_named_file_that_is_not_a_notifier_gets_the_sharper_message(
    tmp_path, monkeypatch, capsys
):
    # The file is there under the expected name but observes nothing (no
    # workflow_run trigger, no sink), so "expected `…`" would misdirect: the fix
    # is to make that file a notifier, not to create it.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "ci-failure-notify.yaml": PUSH_WF.format(name="Not really a notifier"),
        },
    )
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "no failure-notifier workflow found under .github/workflows/" in out
    assert "`ci-failure-notify.yaml` exists but is not a notifier" in out
    assert "expected `ci-failure-notify.yaml`" not in out


def test_notifier_flag_names_the_repo_s_own_expected_filename(
    tmp_path, monkeypatch, capsys
):
    _tree(tmp_path, monkeypatch, {"a.yaml": PUSH_WF.format(name="Alpha")})
    assert _main(monkeypatch, "--notifier", "my-alerts.yaml") == 1
    out = capsys.readouterr().out
    assert "expected `my-alerts.yaml`" in out
    assert "ci-failure-notify.yaml" not in out


def test_notifier_flag_also_drives_the_exists_but_is_not_a_notifier_diagnosis(
    tmp_path, monkeypatch, capsys
):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "my-alerts.yaml": PUSH_WF.format(name="Not really a notifier"),
        },
    )
    assert _main(monkeypatch, "--notifier", "my-alerts.yaml") == 1
    assert "`my-alerts.yaml` exists but is not a notifier" in capsys.readouterr().out


def test_notifier_flag_is_not_a_filter_on_discovery(tmp_path, monkeypatch, capsys):
    # `--notifier` states an expectation for the error message only: a notifier
    # under a different filename is still DISCOVERED by shape, so nothing fires.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "build-publish-notify.yaml": _wf_run(
                "Build, publish and notify", ["Alpha"]
            ),
        },
    )
    assert _main(monkeypatch, "--notifier", "my-alerts.yaml") == 0
    assert capsys.readouterr().out == ""


def test_fail_closed_is_silent_when_a_notifier_is_present(
    tmp_path, monkeypatch, capsys
):
    # Non-vacuity guard for the four cases above: with a discoverable, correct
    # notifier in the tree NONE of the fail-closed wording fires, so those
    # assertions are reporting on the missing notifier and not on any tree.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "b.yaml": PUSH_WF.format(name="Beta"),
            "ci-failure-notify.yaml": _notifier(["Alpha", "Beta"]),
        },
    )
    assert _main(monkeypatch) == 0
    out = capsys.readouterr().out
    assert out == ""
    assert "no failure-notifier workflow found" not in out


# ── positional file arguments are ignored; discovery globs the tree ──────
def test_passed_filenames_do_not_narrow_discovery(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "b.yaml": PUSH_WF.format(name="Beta"),
            "ci-failure-notify.yaml": _notifier(["Alpha"]),
        },
    )
    # Only a.yaml is handed over, yet the unlisted Beta is still caught.
    assert _main(monkeypatch, ".github/workflows/a.yaml") == 1
    assert "missing (fails silently): ['Beta']" in capsys.readouterr().out


# ── unnamed monitored workflow: fallback name + flag ─────────────────────
def test_unnamed_workflow_uses_path_fallback_and_is_flagged(
    tmp_path, monkeypatch, capsys
):
    unnamed = "on:\n  push:\n    branches: [main]\njobs: {}\n"
    _tree(
        tmp_path,
        monkeypatch,
        {
            "unnamed.yaml": unnamed,
            "ci-failure-notify.yaml": _notifier([".github/workflows/unnamed.yaml"]),
        },
    )
    # The list carries the exact fallback (the file path), so coverage itself
    # is satisfied — but the missing `name:` is still flagged.
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "no `name:`" in out
    assert "is out of sync" not in out


# ── notifier without a workflows list is a finding ───────────────────────
def test_notifier_without_workflow_run_list_fails(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "ci-failure-notify.yaml": _wf_run("CI failure notify", None),
        },
    )
    assert _main(monkeypatch) == 1
    assert "no `on.workflow_run.workflows` list" in capsys.readouterr().out


# ── malformed YAML is reported, not raised ───────────────────────────────
def test_malformed_monitored_workflow_is_reported(tmp_path, monkeypatch, capsys):
    # A monitored workflow that PyYAML can't parse is reported as a violation
    # (not a traceback that aborts the whole scan), so coverage stays verifiable.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "broken.yaml": "on: [push\njobs: {\n",
            "ci-failure-notify.yaml": _notifier(["Alpha"]),
        },
    )
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "could not parse as YAML" in out
    assert "broken.yaml" in out


def test_malformed_notifier_is_reported(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "ci-failure-notify.yaml": "on: [workflow_run\njobs: {\n",
        },
    )
    assert _main(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "could not parse as YAML" in out
    assert "ci-failure-notify.yaml" in out


# ── --require-alert-priority: a defaulted priority must be stated ────────
# The trigger is the INVOKED ACTION's declared inputs, never its name, so every
# case below builds the composite it calls. `--allow-no-notifier` isolates these
# trees from the fail-closed invariant: the only violations that can appear are
# the priority ones being asserted on.
PRIORITY_ARGS = ("--allow-no-notifier", "--require-alert-priority")


def test_omitted_priority_on_a_defaulting_action_is_flagged_with_its_line(
    tmp_path, monkeypatch, capsys
):
    wf = _alert_wf(with_lines=["tags: rotating_light"])
    _tree(tmp_path, monkeypatch, {"alert.yaml": wf})
    _actions(tmp_path, monkeypatch, {"notify-ntfy": OPTIONAL_PRIORITY_ACTION})
    assert _main(monkeypatch, *PRIORITY_ARGS) == 1
    out = capsys.readouterr().out
    assert ALERT_STEP_NAME in out
    assert "without an explicit `priority:`" in out
    # Line-anchored at the step itself — the fixture's own ground truth, and
    # emphatically not the file's first line.
    line = _line_of(wf, f"name: {ALERT_STEP_NAME}")
    assert line > 1
    assert f"::error file=.github/workflows/alert.yaml,line={line}::" in out


def test_stated_priority_is_clean(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {"alert.yaml": _alert_wf(with_lines=["priority: 3", "tags: warning"])},
    )
    _actions(tmp_path, monkeypatch, {"notify-ntfy": OPTIONAL_PRIORITY_ACTION})
    assert _main(monkeypatch, *PRIORITY_ARGS) == 0
    assert capsys.readouterr().out == ""


def test_step_with_no_with_block_at_all_is_flagged(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"alert.yaml": _alert_wf(with_lines=None)})
    _actions(tmp_path, monkeypatch, {"notify-ntfy": OPTIONAL_PRIORITY_ACTION})
    assert _main(monkeypatch, *PRIORITY_ARGS) == 1
    assert "without an explicit `priority:`" in capsys.readouterr().out


def test_priority_is_lenient_without_the_flag(tmp_path, monkeypatch, capsys):
    # Module precedent: a new invariant ships opt-in so adopting the hook does
    # not red every consumer's tree on day one.
    _tree(tmp_path, monkeypatch, {"alert.yaml": _alert_wf(with_lines=None)})
    _actions(tmp_path, monkeypatch, {"notify-ntfy": OPTIONAL_PRIORITY_ACTION})
    assert _main(monkeypatch, "--allow-no-notifier") == 0
    assert capsys.readouterr().out == ""


def test_required_priority_input_is_not_flagged(tmp_path, monkeypatch, capsys):
    # A required input has no silent default to inherit — GitHub itself is the
    # forcing function, so there is nothing for this lint to add.
    _tree(tmp_path, monkeypatch, {"alert.yaml": _alert_wf(with_lines=None)})
    _actions(tmp_path, monkeypatch, {"notify-ntfy": REQUIRED_PRIORITY_ACTION})
    assert _main(monkeypatch, *PRIORITY_ARGS) == 0
    assert capsys.readouterr().out == ""


def test_action_without_a_priority_input_is_not_flagged(tmp_path, monkeypatch, capsys):
    # Discovery is by declared inputs: an ordinary local composite is untouched.
    _tree(
        tmp_path,
        monkeypatch,
        {"alert.yaml": _alert_wf(uses="./.github/actions/upload", with_lines=None)},
    )
    _actions(tmp_path, monkeypatch, {"upload": NO_PRIORITY_ACTION})
    assert _main(monkeypatch, *PRIORITY_ARGS) == 0
    assert capsys.readouterr().out == ""


def test_published_action_and_reusable_workflow_are_not_flagged(
    tmp_path, monkeypatch, capsys
):
    # Neither is resolvable from the tree, so neither can be shown to default an
    # input — flagging on the name alone is exactly the guesswork this avoids.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "published.yaml": _alert_wf(uses="actions/checkout@v4", with_lines=None),
            "reusable.yaml": _alert_wf(
                uses="./.github/workflows/reusable-notify.yaml",
                with_lines=None,
                name="Beta",
            ),
        },
    )
    _actions(tmp_path, monkeypatch, {"notify-ntfy": OPTIONAL_PRIORITY_ACTION})
    assert _main(monkeypatch, *PRIORITY_ARGS) == 0
    assert capsys.readouterr().out == ""


def test_local_action_missing_from_disk_is_not_flagged_and_does_not_crash(
    tmp_path, monkeypatch, capsys
):
    _tree(
        tmp_path,
        monkeypatch,
        {"alert.yaml": _alert_wf(uses="./.github/actions/ghost", with_lines=None)},
    )
    _actions(tmp_path, monkeypatch, {})  # empty .github/actions/, no `ghost`
    assert _main(monkeypatch, *PRIORITY_ARGS) == 0
    assert capsys.readouterr().out == ""


def test_tags_is_deliberately_not_required(tmp_path, monkeypatch, capsys):
    # Pinning the decision: `tags:` is cosmetic and also defaulted by the action,
    # but only `priority:` drives phone behaviour. A step stating priority and
    # omitting tags is clean — requiring every optional input would make this
    # noise rather than a severity discipline.
    _tree(tmp_path, monkeypatch, {"alert.yaml": _alert_wf(with_lines=["priority: 4"])})
    _actions(tmp_path, monkeypatch, {"notify-ntfy": OPTIONAL_PRIORITY_ACTION})
    assert _main(monkeypatch, *PRIORITY_ARGS) == 0
    assert capsys.readouterr().out == ""


def test_every_offending_step_across_workflows_is_reported(
    tmp_path, monkeypatch, capsys
):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "alert.yaml": _alert_wf(with_lines=None),
            "other.yaml": _alert_wf(with_lines=["tags: warning"], name="Beta"),
        },
    )
    _actions(tmp_path, monkeypatch, {"notify-ntfy": OPTIONAL_PRIORITY_ACTION})
    assert _main(monkeypatch, *PRIORITY_ARGS) == 1
    out = capsys.readouterr().out
    assert "file=.github/workflows/alert.yaml,line=" in out
    assert "file=.github/workflows/other.yaml,line=" in out
    assert "2 notifier-coverage violation(s) found" in out


# ── crash resistance (property fuzz over the check_repo surface) ─────────
_FRAGMENTS = [
    "name: x\n",
    "name: nightly ntfy\n",
    "on: push\n",
    "on: [push, pull_request]\n",
    "on: [workflow_run]\n",
    "on:\n  schedule:\n    - cron: '0 0 * * 0'\n",
    "on:\n  workflow_run:\n    workflows: ['A']\n",
    "on:\n  workflow_run:\n    workflows: [1, 'A']\n",
    "on:\n  workflow_run: scalar\n",
    "on: null\n",
    "jobs: {}\n",
    "jobs:\n  a:\n    steps: notalist\n",
    "jobs:\n  a:\n    uses: ./.github/workflows/slack.yaml\n",
    "jobs:\n  a:\n    steps:\n      - 'bare string step'\n",
    "jobs:\n  a:\n    steps:\n      - run: gh issue create\n",
    "jobs:\n  a:\n    steps:\n      - uses: ./.github/actions/notify-ntfy\n",
    "jobs:\n  a:\n    steps:\n      - uses: ./.github/actions/\n        with: 3\n",
    "jobs:\n  a:\n    steps:\n      - uses: ./.github/actions/ghost\n        with:\n"
    "          priority: 3\n",
    "jobs: notamapping\n",
    "[]\n",
    "just a scalar\n",
]

_EXTRA_PATTERNS = ["tell-the-humans", r"house\.sink", r"\bping\b", "carrier[-_]?pigeon"]


@st.composite
def _workflow_text(draw: st.DrawFn) -> str:
    parts = draw(st.lists(st.sampled_from(_FRAGMENTS), max_size=4))
    if draw(st.booleans()):
        parts.append(draw(st.text(max_size=60)))
    return "".join(parts)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    notifier_text=_workflow_text(),
    other_text=_workflow_text(),
    flag=st.booleans(),
    extra=st.lists(st.sampled_from(_EXTRA_PATTERNS), max_size=3),
    notifier_name=st.sampled_from(
        ["ci-failure-notify.yaml", "other.yaml", "", "nope.yaml", "../escape.yaml"]
    ),
    require_priority=st.booleans(),
    action_text=st.sampled_from(
        [
            OPTIONAL_PRIORITY_ACTION,
            REQUIRED_PRIORITY_ACTION,
            NO_PRIORITY_ACTION,
            "inputs: notamapping\n",
            "priority: 3\n",
            "[]\n",
            "on: [push\n",  # unparseable action.yaml
        ]
    ),
)
def test_check_repo_never_crashes(
    notifier_text,
    other_text,
    flag,
    extra,
    notifier_name,
    require_priority,
    action_text,
    tmp_path_factory,
    monkeypatch,
):
    # check_repo reads and safe_loads each file inline, so (mirroring main()'s
    # behavior and the sibling fuzz harness) a YAMLError is the parser's, not the
    # lint's -- only parseable docs are fed through.
    for text in (notifier_text, other_text):
        try:
            yaml.safe_load(text)
        except yaml.YAMLError:
            assume(False)
    root = tmp_path_factory.mktemp("repo")
    _tree(
        root,
        monkeypatch,
        {"ci-failure-notify.yaml": notifier_text, "other.yaml": other_text},
    )
    _actions(root, monkeypatch, {"notify-ntfy": action_text})
    result = cfnc.check_repo(flag, extra, notifier_name, require_priority)
    assert isinstance(result, list)
    assert all(isinstance(msg, str) for msg in result)


# ── trigger-shape handling ───────────────────────────────────────────────
def test_scalar_and_list_on_shapes_are_monitored(tmp_path, monkeypatch):
    _tree(
        tmp_path,
        monkeypatch,
        {
            "scalar.yaml": "name: Scalar\non: push\njobs: {}\n",
            "list.yaml": "name: Listed\non: [push, pull_request]\njobs: {}\n",
            "ci-failure-notify.yaml": _notifier(["Listed", "Scalar"]),
        },
    )
    assert _main(monkeypatch) == 0
