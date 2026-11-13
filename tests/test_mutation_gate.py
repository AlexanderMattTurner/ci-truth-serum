"""Tests for .github/scripts/mutation-gate.sh — the job that skips a scheduled
mutation run when a passing run already judged HEAD at this scope or wider.

`gh` is a stub that prints the run names the test hands it, so each case pins
one verdict of the scope rule: a full run covers a daily one, and a daily run
does not cover a full one.
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

GATE = REPO_ROOT / ".github" / "scripts" / "mutation-gate.sh"


def _gate(tmp_path: Path, scope: str, titles: str, event: str = "schedule"):
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    gh = stubs / "gh"
    gh.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$TITLES"\n', encoding="utf-8")
    gh.chmod(0o755)
    out = tmp_path / "out"
    out.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "TITLES": titles,
        "EVENT": event,
        "HEAD_SHA": "abc",
        "SCOPE": scope,
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_OUTPUT": str(out),
    }
    proc = subprocess.run(
        ["bash", str(GATE)], env=env, capture_output=True, text=True, check=False
    )
    return proc, out.read_text(encoding="utf-8").split()


@pytest.mark.parametrize(
    ("scope", "titles", "run"),
    [
        ("daily", "Mutation testing (daily)", "false"),
        ("daily", "Mutation testing (full)", "false"),
        ("full", "Mutation testing (daily)", "true"),
        ("full", "Mutation testing (full)", "false"),
        ("daily", "", "true"),
    ],
)
def test_a_scheduled_run_skips_only_a_head_judged_at_its_scope_or_wider(
    tmp_path, scope, titles, run
) -> None:
    proc, out = _gate(tmp_path, scope, titles)
    assert proc.returncode == 0, proc.stderr
    assert out == [f"run={run}", f"scope={scope}"]


def test_a_dispatch_always_runs(tmp_path) -> None:
    proc, out = _gate(
        tmp_path, "full", "Mutation testing (full)", event="workflow_dispatch"
    )
    assert proc.returncode == 0, proc.stderr
    assert out == ["run=true", "scope=full"]


def test_an_unknown_scope_fails_loud(tmp_path) -> None:
    proc, _ = _gate(tmp_path, "weekly", "")
    assert proc.returncode == 1
    assert "SCOPE must be daily or full" in proc.stderr
