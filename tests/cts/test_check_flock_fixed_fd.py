"""Tests for ci_truth_serum/check_flock_fixed_fd.py — the lint that flags a lock
taken on a hardcoded file descriptor the file never opens.

Drives `violations()` for the parsing rules and `main()` for the argv/exit-code
contract. The two probes every shell lint in this pack must survive — the idiom
inside a logger's message string, and the idiom inside a heredoc body — have
their own cases below.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, load_hook

mod = load_hook("check_flock_fixed_fd.py", "check_flock_fixed_fd")


# ── flagged: the operand is a literal descriptor ─────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        ("exclusive", "flock -x 9\n"),
        ("shared", "flock -s 9\n"),
        ("no option at all", "flock 200\n"),
        ("non-blocking", "flock -n 9\n"),
        ("short cluster", "flock -xn 9\n"),
        ("absolute path to flock", "/usr/bin/flock -x 9\n"),
        ("a timeout before the operand", "flock -w 5 9\n"),
        ("an attached timeout value", "flock -w5 9\n"),
        ("a long timeout option", "flock --timeout 5 9\n"),
        ("a joined long option", "flock --timeout=5 9\n"),
        ("a conflict exit code", "flock -E 3 9\n"),
        ("after the option terminator", "flock -x -- 9\n"),
        ("a quoted descriptor", 'flock -x "9"\n'),
        ("inside a command substitution", "out=$(flock -x 9)\n"),
        ("with an assignment prefix", "LC_ALL=C flock -x 9\n"),
    ],
)
def test_a_literal_descriptor_is_flagged(name: str, src: str) -> None:
    assert mod.violations(src) == [1], name


def test_every_call_is_reported_at_its_own_line() -> None:
    src = "flock -x 9\nflock -x /var/lock/a cmd\nflock -x 200\n"
    assert mod.violations(src) == [1, 3]


def test_two_calls_on_one_line_report_that_line_once() -> None:
    assert mod.violations("flock -x 9 && flock -x 8\n") == [1]


def test_a_launcher_wrapping_flock_is_not_judged() -> None:
    """Only the command's own NAME is in scope.

    A `flock` word further along a command line is somebody else's argument, and
    no launcher usefully wraps this form: the descriptor the operand names
    belongs to the shell that opened it.
    """
    assert mod.violations("sudo flock -x 9\n") == []


# ── not flagged: the file opens the descriptor itself ────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        ("the documented pairing", "exec 9>/var/lock/x\nflock -x 9\n"),
        ("opened for reading", "exec 9</var/lock/x\nflock -s 9\n"),
        ("opened for both", "exec 9<>/var/lock/x\nflock -x 9\n"),
        ("opened after the lock", "flock -x 9\nexec 9>/var/lock/x\n"),
    ],
)
def test_a_descriptor_the_file_opens_passes(name: str, src: str) -> None:
    """`exec 9>FILE` makes fd 9 this file's own, which is the documented
    util-linux pairing. Two runs of it still contend on the same lock."""
    assert mod.violations(src) == [], name


@pytest.mark.parametrize(
    "name, src",
    [
        # A redirect on another command lasts only for that command.
        ("a per-command redirect", "cmd 9>/dev/null\nflock -x 9\n"),
        # The shell picks the number, so 9 stays somebody else's.
        ("a shell-allocated descriptor", "exec {fd}>/var/lock/x\nflock -x 9\n"),
        # A different number is a different descriptor.
        ("another descriptor", "exec 8>/var/lock/x\nflock -x 9\n"),
        # `exec` inside a message a command prints opens nothing.
        ("an exec in a message", 'gb_warn "run exec 9>FILE first"\nflock -x 9\n'),
    ],
)
def test_an_exec_that_does_not_open_this_descriptor_still_flags(
    name: str, src: str
) -> None:
    assert mod.violations(src) == [2], name


# ── not flagged: the descriptor is not a literal ─────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        ("the remedy, a shell-allocated descriptor", 'flock -x "$lock_fd"\n'),
        ("a braced expansion", 'flock -x "${lock_fd}"\n'),
        ("a default expansion", 'flock -x "${lock_fd:-9}"\n'),
        ("a file operand", "flock -x /var/lock/deploy cmd\n"),
        ("a file operand with a command flag", "flock -x /var/lock/a -c 'cmd'\n"),
        ("a relative file operand", "flock lockfile cmd\n"),
        ("a directory operand", "flock -x . cmd\n"),
        ("no operand at all", "flock\n"),
    ],
)
def test_a_call_that_owns_its_descriptor_passes(name: str, src: str) -> None:
    assert mod.violations(src) == [], name


# ── not flagged: `flock` the shell never runs ────────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        # The two probes: text a command prints, and data written to a file.
        ("a logger's message string", 'gb_warn "do not write flock -x 9 here"\n'),
        ("an echoed instruction", 'echo "run flock -x 9 to reproduce"\n'),
        ("a heredoc body", "cat <<'EOF' >doc.txt\nflock -x 9\nEOF\n"),
        ("a comment", "# flock -x 9 would collide\n"),
        ("a discovery call", "command -v flock >/dev/null\n"),
        ("a word that only starts with the name", "flockctl -x 9\n"),
        ("another program's argument", "helper --lock flock 9\n"),
    ],
)
def test_a_flock_that_is_not_a_call_passes(name: str, src: str) -> None:
    assert mod.violations(src) == [], name


def test_a_redirect_is_not_read_as_the_operand() -> None:
    """`9>` is a file_redirect, a sibling of the command — never an argument.

    A word scan that kept it would read the redirect's own number as the operand
    and report a call that names a path.
    """
    assert mod.violations("flock -x /var/lock/a cmd 9>/dev/null\n") == []


def test_a_command_run_under_the_lock_is_not_read_as_the_operand() -> None:
    """The operand is the FIRST non-option word; what follows belongs to the
    bounded command, so its own numeric argument is not a descriptor."""
    assert mod.violations("flock -x /var/lock/a kill -9 123\n") == []


# ── the opt-out ──────────────────────────────────────────────────────────
def test_an_annotated_call_passes() -> None:
    src = "flock -x 9 # allow-fixed-fd: this script opens fd 9 itself and runs alone\n"
    assert mod.violations(src) == []


def test_the_annotation_works_on_the_line_above() -> None:
    src = "# allow-fixed-fd: this script opens fd 9 itself\nflock -x 9\n"
    assert mod.violations(src) == []


def test_an_annotation_with_no_reason_does_not_suppress() -> None:
    assert mod.violations("flock -x 9 # allow-fixed-fd\n") == [1]


# ── the argv/exit-code contract ──────────────────────────────────────────
def _run(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / "check_flock_fixed_fd.py"), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_main_reports_the_path_and_line_and_exits_one(tmp_path: Path) -> None:
    script = tmp_path / "lock.sh"
    script.write_text(
        "#!/bin/bash\n# take the caller's lock\nflock -x 9\n", encoding="utf-8"
    )
    result = _run(script)
    assert result.returncode == 1
    assert f"{script}:3:" in result.stderr
    assert "lock_fd" in result.stderr


def test_main_exits_zero_on_a_clean_file(tmp_path: Path) -> None:
    script = tmp_path / "lock.sh"
    script.write_text(
        '#!/bin/bash\nexec {lock_fd}>/var/lock/x\nflock -x "$lock_fd"\n',
        encoding="utf-8",
    )
    assert _run(script).returncode == 0


def test_a_file_the_grammar_refuses_fails_loudly(tmp_path: Path) -> None:
    """A pathological input is reported, never skipped: a silent skip would
    false-green exactly the file an adversary controls."""
    script = tmp_path / "huge.sh"
    script.write_text("cmd |" * 3000, encoding="utf-8")
    result = _run(script)
    assert result.returncode == 1
    assert "pipe bytes" in result.stderr


def test_an_empty_run_refuses_rather_than_reporting_a_clean_pass() -> None:
    result = subprocess.run(
        [sys.executable, str(HOOKS_DIR / "check_flock_fixed_fd.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "no files to scan" in result.stderr
