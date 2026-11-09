"""Tests for actions/mute-failure-guard/guard.bash, the file the action points BASH_ENV at.

Each test runs a real bash over a real script with the guard loaded the way the runner loads it,
then reads the ledger the guard wrote."""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

GUARD = REPO_ROOT / "actions" / "mute-failure-guard" / "guard.bash"


def _run(tmp_path: Path, script: str, *, guarded: bool = True, step: str = "build"):
    """Run SCRIPT as a step's shell runs it, and return (process, ledger rows, mute dir)."""
    mute = tmp_path / "mute"
    mute.mkdir(exist_ok=True)
    (mute / "guard.bash").write_text(
        GUARD.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (mute / "active").write_text("", encoding="utf-8")
    body = tmp_path / "step.sh"
    body.write_text(script, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GITHUB_ACTION": step}
    if guarded:
        env["BASH_ENV"] = str(mute / "guard.bash")
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(body)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=30,
    )
    ledger = mute / "ledger"
    rows = (
        [r.split("\t") for r in ledger.read_text(encoding="utf-8").splitlines()]
        if ledger.exists()
        else []
    )
    return proc, rows, mute


_MASKED = (
    "scrub() { false; }\n"  # line 1
    "show() { printf '[%s]\\n' \"$1\"; }\n"  # line 2
    'show "$(scrub)"\n'  # line 3
    "while read -r x; do :; done < <(scrub)\n"  # line 4
    "echo reached-end\n"
)


def test_a_failure_an_outer_command_drops_is_recorded_where_it_happened(tmp_path):
    proc, rows, _ = _run(tmp_path, _MASKED)
    assert (proc.returncode, proc.stdout) == (0, "[]\nreached-end\n")
    sites = [
        (r[0], Path(r[2].rsplit(":", 1)[0]).name, r[2].rsplit(":", 1)[1], r[3])
        for r in rows
    ]
    # `false` inside scrub under "$(scrub)", scrub's status inside $(…), then `false` under <(scrub).
    assert sites == [
        ("build", "step.sh", "1", "1"),
        ("build", "step.sh", "3", "1"),
        ("build", "step.sh", "1", "1"),
    ]


def test_the_guard_changes_no_output_and_no_status(tmp_path):
    script = 'echo out; echo err >&2; x="$(false)"\necho never\n'
    plain, _, _ = _run(tmp_path, script, guarded=False)
    guarded, rows, _ = _run(tmp_path, script)
    assert (guarded.returncode, guarded.stdout, guarded.stderr) == (
        plain.returncode,
        plain.stdout,
        plain.stderr,
    )
    assert plain.returncode == 1
    assert [r[5] for r in rows] == ["false", 'x="$(false)"']


def test_a_guard_with_no_active_marker_does_nothing(tmp_path):
    # The copy in the action's source tree has no marker beside it.
    script = tmp_path / "step.sh"
    script.write_text('show() { :; }\nshow "$(false)"\n', encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "GITHUB_ACTION": "build", "BASH_ENV": str(GUARD)}
    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=30,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert not (GUARD.parent / "ledger").exists()
    assert not (GUARD.parent / "steps").exists()


def test_a_launcher_that_clears_the_environment_but_keeps_bash_env_stays_guarded(
    tmp_path,
):
    # agent-glovebox's bin/glovebox keeps BASH_ENV and drops every variable it does not list.
    proc, rows, _ = _run(
        tmp_path,
        'env -i BASH_ENV="$BASH_ENV" bash -c \'f() { false; }; x="$(f)"; echo "[$x]"\'\n',
    )
    assert proc.stdout == "[]\n"
    # `false` in f, f's status inside $(…), then the assignment, since that child has no -e.
    assert [r[5] for r in rows] == ["<bash -c>", "<bash -c>", "<bash -c>"]


def test_a_bash_c_child_records_no_command_text(tmp_path):
    # The -c string was expanded by the parent, so it can hold a secret value.
    proc, rows, _ = _run(tmp_path, 'token=s3cr3t-value\nbash -c "false $token; true"\n')
    assert proc.returncode == 0
    assert [r[5] for r in rows] == ["<bash -c>"]
    assert "s3cr3t" not in "\n".join("\t".join(r) for r in rows)


# not-a-drift-guard: it checks the files the guard writes, not two copies of one value
def test_the_step_shell_records_its_status_and_a_copy_of_its_output(tmp_path):
    proc, _, mute = _run(tmp_path, "echo one\necho two >&2\nexit 7\n")
    assert proc.returncode == 7
    assert (mute / "steps").read_text(encoding="utf-8") == "build\t7\n"
    assert sorted(
        (mute / "step-build.out").read_text(encoding="utf-8").splitlines()
    ) == ["one", "two"]


def test_a_child_bash_is_not_mistaken_for_the_step_shell(tmp_path):
    _, _, mute = _run(tmp_path, "bash -c 'exit 0'\nexit 3\n")
    assert (mute / "steps").read_text(encoding="utf-8") == "build\t3\n"


@pytest.mark.xfail(
    strict=True,
    reason="bash runs no ERR trap inside a condition, nor in anything a condition calls; "
    "the action's post report names this gap",
)
def test_a_failure_inside_a_condition_is_recorded(tmp_path):
    _, rows, _ = _run(tmp_path, 'if [[ -n "$(false)" ]]; then :; fi\n')
    assert rows


def test_a_bare_wait_in_the_step_returns(tmp_path):
    # Bash 5.1+ makes a bare `wait` wait for the last process substitution too, so an output
    # copy built from `> >(tee …)` hung any step that waits for its background jobs.
    proc, _, mute = _run(tmp_path, "sleep 0.1 &\nwait\necho waited\n")
    assert (proc.returncode, proc.stdout) == (0, "waited\n")
    assert (mute / "steps").read_text(encoding="utf-8") == "build\t0\n"


def test_a_child_bash_that_lost_the_environment_does_not_claim_the_step(tmp_path):
    # The child keeps GITHUB_ACTION and BASH_ENV, as agent-glovebox's launcher does, and fails.
    script = 'env -i GITHUB_ACTION="$GITHUB_ACTION" BASH_ENV="$BASH_ENV" PATH="$PATH" bash -c "exit 5" || :\nexit 0\n'
    proc, _, mute = _run(tmp_path, script)
    assert proc.returncode == 0
    assert (mute / "steps").read_text(encoding="utf-8") == "build\t0\n"
