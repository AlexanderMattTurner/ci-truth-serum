"""Tests for .github/scripts/claude-conflict-resolve.sh's credential refusal.

The script walks a LADDER of OAuth credentials, so the question it must ask
before installing anything is whether that ladder is empty — never whether one
named rung is set. It used to ask for CLAUDE_CODE_OAUTH_TOKEN by name, which
refused ci-truth-serum itself: the repository holds five working _FALLBACK
credentials and nothing under the primary name, so every auto-resolve run died
at the guard with the ladder full.

Both directions are driven against the real script with a stub `npm` on PATH, so
the assertions read the actual refusal rather than source text. Reaching the
ladder walk is the positive marker: it is the first thing past the guard.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "claude-conflict-resolve.sh"
REFUSAL = "no Claude credential is configured"
LADDER_VARS = [
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK",
    "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_2",
    "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_3",
    "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_4",
    "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_5",
    "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_6",
]


def _run(tmp_path: Path, rungs: dict[str, str]) -> subprocess.CompletedProcess:
    """Drive the script with exactly `rungs` set. A stub npm makes the pinned
    CLI install a no-op, so the run reaches the ladder walk in milliseconds and
    dies there on the conflict env it was not given."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    npm.chmod(npm.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    env = {k: v for k, v in os.environ.items() if k not in LADDER_VARS}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["RUNNER_TEMP"] = str(tmp_path / "runner-temp")
    Path(env["RUNNER_TEMP"]).mkdir()
    env.update(rungs)
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize(
    "var",
    ["CLAUDE_CODE_OAUTH_TOKEN_FALLBACK", "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_3",
     "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_6", "CLAUDE_CODE_OAUTH_TOKEN"],
)
def test_one_configured_rung_is_enough(tmp_path: Path, var: str) -> None:
    """ANY single rung clears the guard — the whole point of a ladder. The
    positive marker keeps this from passing vacuously: absence of the refusal
    would also be true of a script that died before the guard ran."""
    result = _run(tmp_path, {var: "sk-test"})
    assert REFUSAL not in result.stderr, f"{var} alone was refused: {result.stderr}"
    assert "credential 1 of 1" in result.stdout, (
        f"never reached the ladder walk past the guard: {result.stdout}{result.stderr}"
    )


def test_an_empty_ladder_is_refused(tmp_path: Path) -> None:
    """No rung at all still refuses, before any credential is walked."""
    result = _run(tmp_path, {})
    assert result.returncode != 0
    assert REFUSAL in result.stderr, result.stderr
    assert "credential" not in result.stdout, (
        f"walked a ladder it had already refused: {result.stdout}"
    )
