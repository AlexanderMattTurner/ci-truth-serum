"""Tests for hooks/check_failure_notifier_coverage.py — the freshness check
keeping ci-failure-notify.yaml's `on.workflow_run.workflows` list (a derived
copy, since workflow_run has no wildcard) in sync with the tree's set of
push/schedule-triggered workflow names.

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


def _notifier(names: list[str]) -> str:
    listed = "\n".join(f'      - "{n}"' for n in names)
    return textwrap.dedent(
        """\
        name: CI failure notify
        on:
          workflow_run:
            workflows:
        {listed}
            types: [completed]
        jobs: {{}}
        """
    ).format(listed=listed)


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


# ── missing notifier: silent without the flag, loud with it ──────────────
def test_missing_notifier_passes_without_flag(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"a.yaml": PUSH_WF.format(name="Alpha")})
    assert _main(monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_missing_notifier_fails_with_require_flag(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, monkeypatch, {"a.yaml": PUSH_WF.format(name="Alpha")})
    assert _main(monkeypatch, "--require-notifier") == 1
    assert "notifier workflow missing" in capsys.readouterr().out


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
            "ci-failure-notify.yaml": "name: n\non:\n  workflow_run:\njobs: {}\n",
        },
    )
    assert _main(monkeypatch) == 1
    assert "no `on.workflow_run.workflows` list" in capsys.readouterr().out


# ── crash resistance (property fuzz over the check_repo surface) ─────────
_FRAGMENTS = [
    "name: x\n",
    "on: push\n",
    "on: [push, pull_request]\n",
    "on:\n  schedule:\n    - cron: '0 0 * * 0'\n",
    "on:\n  workflow_run:\n    workflows: ['A']\n",
    "on:\n  workflow_run:\n    workflows: [1, 'A']\n",
    "on:\n  workflow_run: scalar\n",
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
@given(notifier_text=_workflow_text(), other_text=_workflow_text(), flag=st.booleans())
def test_check_repo_never_crashes(
    notifier_text, other_text, flag, tmp_path_factory, monkeypatch
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
    result = cfnc.check_repo(flag)
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
