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
        (wf_dir / name).write_text(body)
    monkeypatch.setattr(cfnc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cfnc, "WORKFLOWS_DIR", wf_dir)
    return wf_dir


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


def test_pull_request_only_workflows_are_excluded(tmp_path, monkeypatch):
    # A PR-only workflow in the list is stale: it never fires on push/schedule.
    _tree(
        tmp_path,
        monkeypatch,
        {
            "a.yaml": PUSH_WF.format(name="Alpha"),
            "pr.yaml": PR_ONLY_WF.format(name="PR only"),
            "ci-failure-notify.yaml": _notifier(["Alpha", "PR only"]),
        },
    )
    assert _main(monkeypatch) == 1


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
    # held to the exhaustiveness invariant: it lists nothing and stays silent.
    _discovery_tree(
        tmp_path, monkeypatch, _wf_run("Coverage upload", [], jobs=NO_SINK_JOBS)
    )
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_workflow_run_without_a_sink_leaves_the_repo_notifierless(
    tmp_path, monkeypatch, capsys
):
    _discovery_tree(
        tmp_path, monkeypatch, _wf_run("Coverage upload", [], jobs=NO_SINK_JOBS)
    )
    assert _main(monkeypatch, "--require-notifier") == 1
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
    assert _main(monkeypatch, "--require-notifier") == 1
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


# ── missing notifier: silent without the flag, loud with it ──────────────
def test_missing_notifier_passes_without_flag(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"a.yaml": PUSH_WF.format(name="Alpha")})
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_missing_notifier_fails_with_require_flag(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"a.yaml": PUSH_WF.format(name="Alpha")})
    assert _main(monkeypatch, "--require-notifier") == 1
    assert (
        "no failure-notifier workflow found under .github/workflows/"
        in capsys.readouterr().out
    )


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
)
def test_check_repo_never_crashes(
    notifier_text, other_text, flag, extra, tmp_path_factory, monkeypatch
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
    result = cfnc.check_repo(flag, extra)
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
