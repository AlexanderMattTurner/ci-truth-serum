"""Tests for ci_truth_serum/check_soft_timeout.py — the lint that flags a `timeout`
bound sending only SIGTERM, which the bounded command can ignore.

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

mod = load_hook("check_soft_timeout.py", "check_soft_timeout")


# ── flagged: a bound with no SIGKILL escalation ──────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        ("bare command", "timeout 60 cmd\n"),
        ("unit suffix", "timeout 5m cmd\n"),
        ("fractional duration", "timeout 1.5 cmd\n"),
        ("computed duration", 'timeout "$CAP" cmd\n'),
        ("default-expansion duration", 'timeout "${CAP:-600}" cmd\n'),
        ("absolute path", "/usr/bin/timeout 5 cmd\n"),
        ("macOS coreutils name", "gtimeout 5 cmd\n"),
        ("a signal that is not KILL", "timeout -s TERM 60 cmd\n"),
        ("an unrelated option", "timeout --foreground 60 cmd\n"),
        ("inside a command substitution", "out=$(timeout 60 cmd)\n"),
        ("wrapped by another launcher", "sbx exec box -- timeout 60 cat /x\n"),
        ("wrapped by sudo", "sudo timeout 300 apt-get update\n"),
        ("array literal", "run=(timeout 600)\n"),
        ("array literal, computed duration", 'run=(timeout "${T:-600}")\n'),
        ("array append", "to+=(timeout 30)\n"),
    ],
)
def test_a_bound_that_only_asks_is_flagged(name: str, src: str) -> None:
    assert mod.violations(src) == [1], name


def test_every_bound_is_reported_at_its_own_line() -> None:
    src = "timeout 30 a\ntimeout --kill-after=5 30 b\ntimeout 30 c\n"
    assert mod.violations(src) == [1, 3]


def test_two_bounds_on_one_line_report_that_line_once() -> None:
    assert mod.violations("timeout 30 a && timeout 30 b\n") == [1]


def test_the_finding_sits_on_the_timeout_word_not_the_statement_start() -> None:
    """A continued command anchors on the line the reader adds the flag to."""
    src = "sudo \\\n  timeout 300 apt-get update\n"
    assert mod.violations(src) == [2]


# ── not flagged: the bound escalates ─────────────────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        ("long option with =", "timeout --kill-after=10 60 cmd\n"),
        ("long option, value in next word", "timeout --kill-after 10 60 cmd\n"),
        ("short option, value in next word", "timeout -k 10 60 cmd\n"),
        ("short option, attached value", "timeout -k10 60 cmd\n"),
        ("short cluster ending in k", "timeout -vk 10 60 cmd\n"),
        ("SIGKILL sent first", "timeout -s KILL 60 cmd\n"),
        ("SIGKILL, attached value", "timeout -sKILL 60 cmd\n"),
        ("SIGKILL by long option", "timeout --signal=SIGKILL 60 cmd\n"),
        ("SIGKILL by number", "timeout -s 9 60 cmd\n"),
        ("escalation in an array literal", "run=(timeout --kill-after=5 600)\n"),
        ("escalation with an option between", "timeout --foreground -k 5 60 cmd\n"),
    ],
)
def test_a_bound_that_escalates_passes(name: str, src: str) -> None:
    assert mod.violations(src) == [], name


# ── not flagged: `timeout` the shell never runs as a bound ───────────────
@pytest.mark.parametrize(
    "name, src",
    [
        # The two probes: text a command prints, and data written to a file.
        ("a logger's message string", 'gb_warn "raise the timeout 60 seconds"\n'),
        ("an echoed instruction", 'echo "run timeout 60 curl to test it"\n'),
        (
            "a heredoc body",
            "cat <<'EOF' >doc.txt\ntimeout 60 cmd\nEOF\n",
        ),
        ("a comment", "# timeout 60 cmd would hang\n"),
        ("a variable of that name", "((timeout > 0))\n"),
        ("a discovery call", "command -v timeout >/dev/null\n"),
        ("another program's flag", "curl --timeout 60 https://example.invalid\n"),
        ("a word that only starts with the name", "timeoutctl 60 cmd\n"),
        ("no duration at all", "timeout cmd\n"),
    ],
)
def test_a_timeout_that_is_not_a_bound_passes(name: str, src: str) -> None:
    assert mod.violations(src) == [], name


def test_a_redirect_is_not_read_as_the_duration() -> None:
    """`>&2` is a file_redirect, a sibling of the command — never an argument.

    A word scan that kept it would read the redirect as the bounded command and
    accept `timeout 60 >&2` as a complete call, or reject a real one.
    """
    assert mod.violations("timeout 60 cmd >&2\n") == [1]


def test_a_flag_of_the_bounded_command_does_not_count_as_escalation() -> None:
    """Words past the duration belong to the inner program.

    `ffmpeg --kill-after=1` is ffmpeg's own flag, and reading it as this bound's
    would excuse the very defect the lint exists to report.
    """
    assert mod.violations("timeout 60 ffmpeg --kill-after=1 in.mp4\n") == [1]


# ── the opt-out ──────────────────────────────────────────────────────────
def test_an_annotated_bound_passes() -> None:
    src = "timeout 60 dpkg -i x.deb # allow-soft-timeout: dpkg holds SIGTERM\n"
    assert mod.violations(src) == []


def test_the_annotation_works_on_the_line_above() -> None:
    src = "# allow-soft-timeout: dpkg holds SIGTERM\ntimeout 60 dpkg -i x.deb\n"
    assert mod.violations(src) == []


def test_an_annotation_with_no_reason_does_not_suppress() -> None:
    assert mod.violations("timeout 60 cmd # allow-soft-timeout\n") == [1]


# ── the argv/exit-code contract ──────────────────────────────────────────
def _run(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / "check_soft_timeout.py"), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_main_reports_the_path_and_line_and_exits_one(tmp_path: Path) -> None:
    script = tmp_path / "boot.sh"
    script.write_text("#!/bin/bash\ntimeout 60 cmd\n", encoding="utf-8")
    result = _run(script)
    assert result.returncode == 1
    assert f"{script}:2:" in result.stderr
    assert "--kill-after" in result.stderr


def test_main_exits_zero_on_a_clean_file(tmp_path: Path) -> None:
    script = tmp_path / "boot.sh"
    script.write_text("#!/bin/bash\ntimeout -k 10 60 cmd\n", encoding="utf-8")
    assert _run(script).returncode == 0


def test_a_file_the_grammar_refuses_fails_loudly(tmp_path: Path) -> None:
    """A pathological input is reported, never skipped: a silent skip would
    false-green exactly the file an adversary controls."""
    script = tmp_path / "huge.sh"
    script.write_text("cmd |" * 3000, encoding="utf-8")
    result = _run(script)
    assert result.returncode == 1
    assert "pipe bytes" in result.stderr
