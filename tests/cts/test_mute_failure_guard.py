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


def _sites(rows):
    """(file name, line, status, command) of each ledger row."""
    return [
        (
            Path(r[2].rsplit(":", 1)[0]).name,
            int(r[2].rsplit(":", 1)[1]),
            int(r[3]),
            r[5],
        )
        for r in rows
    ]


def test_a_failure_an_outer_command_drops_is_recorded_where_it_happened(tmp_path):
    proc, rows, _ = _run(tmp_path, _MASKED)
    assert (proc.returncode, proc.stdout) == (0, "[]\nreached-end\n")
    assert {r[0] for r in rows} == {"build"}
    assert _sites(rows) == [
        ("step.sh", 1, 1, "false"),  # `false` inside scrub, under "$(scrub)"
        ("step.sh", 3, 1, "false"),  # scrub's status inside the $(…) on line 3
        ("step.sh", 3, 1, "subshell exit, first command: scrub"),
        ("step.sh", 1, 1, "false"),  # `false` inside scrub, under <(scrub)
        ("step.sh", 4, 1, "subshell exit, first command: scrub"),
    ]


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('if [[ -n "$(false)" ]]; then :; fi', id="if-condition"),
        pytest.param('[[ -n "$(false)" ]] || echo no', id="left-of-or"),
        pytest.param(
            "f() { local v; v=$(false); }\nif f; then :; fi",
            id="function-called-from-a-condition",
        ),
    ],
)
def test_a_substitution_that_fails_inside_a_condition_is_recorded(tmp_path, line):
    # Bash runs no ERR trap in a condition, so the subshell-exit row is the only record.
    proc, rows, _ = _run(tmp_path, line + "\n")
    assert proc.returncode == 0
    assert [s[2:] for s in _sites(rows)] == [(1, "subshell exit, first command: false")]


def test_a_failure_that_or_true_discards_is_recorded(tmp_path):
    proc, rows, _ = _run(
        tmp_path, "f() { return 4; }\nf || :\ng() { return 0; }\ng || true\n"
    )
    assert proc.returncode == 0
    assert _sites(rows) == [("step.sh", 2, 4, "suppressed by || true: return")]


def test_a_substitution_that_succeeds_leaves_no_row(tmp_path):
    proc, rows, _ = _run(
        tmp_path,
        'x=$(true)\ny="$(printf ok)"\nwhile read -r l; do :; done < <(echo a)\n( : )\n',
    )
    assert (proc.returncode, rows) == (0, [])


@pytest.mark.parametrize(
    "signals",
    [
        pytest.param("RETURN", id="return-only"),
        pytest.param("RETURN EXIT", id="return-then-exit"),
        pytest.param("RETURN ERR", id="return-then-err"),
        pytest.param("EXIT RETURN", id="exit-then-return"),
        pytest.param("'RETURN'", id="single-quoted"),
        pytest.param('"RETURN" EXIT', id="double-quoted-then-exit"),
    ],
)
def test_a_return_trap_cleanup_waits_for_its_own_function(tmp_path, signals):
    # -T would also run f's RETURN trap when g returns; the guard turns -T off before such a trap.
    script = (
        "g() { :; }\n"
        f'f() {{ local t="$PWD/kept"; : >"$t"; trap "rm -f -- $t" {signals}; g; [[ -e $t ]] && echo kept-after-g; }}\n'
        "f\n"
        '[[ -e "$PWD/kept" ]] || echo removed-after-f\n'
    )
    proc, _, _ = _run(tmp_path, script)
    assert (proc.returncode, proc.stdout) == (0, "kept-after-g\nremoved-after-f\n")


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
    assert [s[3] for s in _sites(rows)] == [
        "false",
        "subshell exit, first command: false",
        "x=",
    ]


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


def test_a_bash_c_child_records_no_command_text(tmp_path):
    # The -c string was expanded by the parent, so it can hold a secret value.
    proc, rows, _ = _run(tmp_path, 'token=s3cr3t-value\nbash -c "false $token; true"\n')
    assert proc.returncode == 0
    assert [r[5] for r in rows] == ["<bash -c>", "suppressed by || true: <bash -c>"]
    assert "s3cr3t" not in "\n".join("\t".join(r) for r in rows)


def test_a_launcher_that_clears_the_environment_but_keeps_bash_env_stays_guarded(
    tmp_path,
):
    # agent-glovebox's bin/glovebox keeps BASH_ENV and drops every variable it does not list.
    proc, rows, _ = _run(
        tmp_path,
        'env -i BASH_ENV="$BASH_ENV" bash -c \'f() { false; }; x="$(f)"; echo "[$x]"\'\n',
    )
    assert proc.stdout == "[]\n"
    # `false` in f, f's status inside $(…), the subshell's exit, then the assignment, since
    # that child has no -e.
    assert [r[5] for r in rows] == [
        "<bash -c>",
        "<bash -c>",
        "subshell exit, first command: <bash -c>",
        "<bash -c>",
    ]


# not-a-drift-guard: it checks the files the guard writes, not two copies of one value
def test_the_step_shell_records_its_status_and_a_copy_of_its_output(tmp_path):
    proc, _, mute = _run(tmp_path, "echo one\necho two >&2\nexit 7\n")
    assert proc.returncode == 7
    assert (mute / "steps").read_text(encoding="utf-8") == "build\t7\n"
    out = (mute / "step-build.out").read_text(encoding="utf-8")
    assert sorted(out.splitlines()) == ["one", "two"]


def test_a_child_bash_is_not_mistaken_for_the_step_shell(tmp_path):
    _, _, mute = _run(tmp_path, "bash -c 'exit 0'\nexit 3\n")
    assert (mute / "steps").read_text(encoding="utf-8") == "build\t3\n"


def test_a_child_bash_that_lost_the_environment_does_not_claim_the_step(tmp_path):
    # The child keeps GITHUB_ACTION and BASH_ENV, as agent-glovebox's launcher does, and fails.
    script = 'env -i GITHUB_ACTION="$GITHUB_ACTION" BASH_ENV="$BASH_ENV" PATH="$PATH" bash -c "exit 5" || :\nexit 0\n'
    proc, _, mute = _run(tmp_path, script)
    assert proc.returncode == 0
    assert (mute / "steps").read_text(encoding="utf-8") == "build\t0\n"


def test_a_bare_wait_in_the_step_returns(tmp_path):
    # Bash 5.1+ makes a bare `wait` wait for the last process substitution too, so an output
    # copy built from `> >(tee …)` hung any step that waits for its background jobs.
    proc, _, mute = _run(tmp_path, "sleep 0.1 &\nwait\necho waited\n")
    assert (proc.returncode, proc.stdout) == (0, "waited\n")
    assert (mute / "steps").read_text(encoding="utf-8") == "build\t0\n"


def test_an_evaluated_command_records_only_its_name(tmp_path):
    # Inside eval, BASH_COMMAND is already expanded, so its text can hold a secret.
    script = 'secret=dynamic-secret-value\neval "false $secret" || :\neval "tok=$secret; false" || :\n'
    proc, rows, mute = _run(tmp_path, script)
    assert proc.returncode == 0
    assert "dynamic-secret-value" not in (mute / "ledger").read_text(encoding="utf-8")
    assert [r[5] for r in rows] == [
        "suppressed by || true: false",
        "suppressed by || true: false",
    ]


@pytest.mark.parametrize(
    "script",
    [
        pytest.param('mkdir -p "$HOME/made"; cd "$_"; pwd\n', id="last-argument"),
        pytest.param(
            '[[ release-123 =~ ^release-([0-9]+)$ ]]; printf "%s\\n" "${BASH_REMATCH[1]}"\n',
            id="regex-captures",
        ),
    ],
)
def test_the_guard_leaves_the_callers_shell_state_alone(tmp_path, script):
    plain, _, _ = _run(tmp_path, script, guarded=False)
    guarded, _, _ = _run(tmp_path, script)
    assert (guarded.returncode, guarded.stdout) == (plain.returncode, plain.stdout)
    assert plain.stdout.strip() not in ("", str(tmp_path))


def test_a_shell_that_unsets_the_guards_variables_keeps_running(tmp_path):
    # A launcher that replays a variable snapshot unsets each name first; under `set -u` a
    # trap that then reads one would end the user's script.
    script = (
        "set -u\n"
        "unset _cts_sub_level _cts_prev _cts_mute_dir _cts_us\n"
        "echo ok\n"
        "x=$(echo sub)\n"
        "false || :\n"
        "echo done\n"
    )
    proc, rows, mute = _run(tmp_path, script)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "ok\ndone\n", "")
    assert (mute / "steps").read_text(encoding="utf-8") == "build\t0\n"
    # The rows still reach the ledger with its path variable gone.
    assert _sites(rows) == [("step.sh", 5, 1, "suppressed by || true: false")]


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("git fetch origin", "git"),
        ("tok=s3cr3t", "tok="),
        ("_cts_rc=$? _cts_us=$_", "?"),
    ],
)
def test_a_row_names_the_command_and_never_the_guards_own(tmp_path, text, name):
    # Bash 3.2 hands the ERR trap its own command text, which names no user command.
    proc, _, _ = _run(
        tmp_path, f"_cts_mute_cmd '{text}'\nprintf '%s\\n' \"$_cts_cmd\"\n"
    )
    assert (proc.returncode, proc.stdout) == (0, f"{name}\n")
