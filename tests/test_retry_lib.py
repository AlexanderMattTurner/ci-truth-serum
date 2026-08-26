"""Behaviour of the one retry primitive, `.github/scripts/lib/retry.bash`.

Every release and CI script sources this file, so its argument validation is
the only thing between a caller's typo and a `set -e` abort deep in a job.
"""

import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)
RETRY_LIB = REPO_ROOT / ".github" / "scripts" / "lib" / "retry.bash"


def _run(body: str) -> subprocess.CompletedProcess:
    script = textwrap.dedent(
        f"""
        set -euo pipefail
        source {RETRY_LIB}
        {body}
        """
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def test_a_command_that_succeeds_runs_once():
    result = _run("retry_cmd 3 0 bash -c 'echo ran; exit 0'; echo \"rc=$?\"")
    assert result.stdout.count("ran") == 1
    assert "rc=0" in result.stdout


def test_a_command_that_fails_every_time_returns_one():
    # `|| rc=$?` because the caller runs under `set -e`, which the contract
    # requires and which aborts on the bare call.
    result = _run('rc=0; retry_cmd 3 0 false || rc=$?; echo "rc=$rc"')
    assert "rc=1" in result.stdout
    assert result.stderr.count("failed; retrying") == 2


def test_a_command_that_succeeds_on_the_second_attempt_returns_zero(tmp_path):
    marker = tmp_path / "seen"
    result = _run(
        f"retry_cmd 3 0 bash -c '[[ -e {marker} ]] || {{ touch {marker}; exit 1; }}'"
        '; echo "rc=$?"'
    )
    assert "rc=0" in result.stdout
    assert result.stderr.count("failed; retrying") == 1


def test_a_non_integer_max_is_rejected_before_the_command_runs():
    """`$((delay * 2))` on a non-integer is a syntax error that aborts the
    caller, so the guard must reject the argument instead."""
    result = _run("retry_cmd 2.5 0 echo ran")
    assert result.returncode != 0
    assert "MAX must be a non-negative integer" in result.stderr
    assert "ran" not in result.stdout


def test_a_zero_max_is_rejected_rather_than_reported_as_a_failure():
    """MAX=0 would skip the loop and return 1 without ever running COMMAND."""
    result = _run("retry_cmd 0 0 echo ran")
    assert result.returncode != 0
    assert "MAX must be at least 1" in result.stderr
    assert "ran" not in result.stdout


def test_a_non_integer_delay_is_rejected():
    result = _run("retry_cmd 2 0.5 echo ran")
    assert result.returncode != 0
    assert "INITIAL_DELAY must be a non-negative integer" in result.stderr
    assert "ran" not in result.stdout
