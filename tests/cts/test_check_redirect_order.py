"""Tests for ci_truth_serum/check_redirect_order.py — the lint that refuses an
input redirect written before the stderr redirect that should silence it.

The first test runs real bash on the refused order, so the defect is observed,
not assumed. The two probes every shell lint must survive — the idiom inside a
printed message, and inside a heredoc body — have their own cases.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, load_hook

mod = load_hook("check_redirect_order.py", "check_redirect_order")

# One refused line per input-redirect form, each before a silencer.
_INPUT_FORMS = (
    'cmd <"$path" 2>/dev/null',
    "cmd 0<&3 2>/dev/null",
    "cmd <&3 2>/dev/null",
    'cmd 0<"$path" 2>/dev/null',
    'cmd 0>"$path" 2>/dev/null',
    '<"$path" cmd 2>/dev/null',
)
# One refused line per silencer form, each after a plain `<` input redirect.
_SILENCER_FORMS = (
    'cmd <"$path" 2>>/dev/null',
    'cmd <"$path" 2>|/dev/null',
    'cmd <"$path" 2>&-',
    'cmd <"$path" &>/dev/null',
    'cmd <"$path" &>>/dev/null',
    'cmd <"$path" >&/dev/null',
)
_SAFE = (
    "{ cmd <f; } 2>/dev/null",
    "cmd 2>/dev/null <f",
    "cmd 2>/dev/null",
    "cmd </dev/null 2>/dev/null",
    "stty size <&2 2>/dev/null",
    "cmd <&- 2>/dev/null",
    'jq . <<<"$x" 2>/dev/null',
    "cmd <f >/dev/null",
    "cmd 0 >f 2>/dev/null",
    'cmd <"$path" 2>"$log"',
)


def _body(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def test_bash_prints_a_failed_open_through_the_refused_order(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    run = [
        subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )
        for script in (f"cat <{missing} 2>/dev/null", f"cat 2>/dev/null <{missing}")
    ]
    assert "No such file or directory" in run[0].stderr
    assert run[1].stderr == ""


@pytest.mark.parametrize("line", _INPUT_FORMS + _SILENCER_FORMS)
def test_each_input_redirect_before_a_silencer_is_refused(line: str) -> None:
    assert mod.violations(_body("f() {", f"  {line} || return 0", "}")) == [2]


@pytest.mark.parametrize("line", _SAFE)
def test_each_safe_order_passes(line: str) -> None:
    assert mod.violations(_body(line)) == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (_body("while read -r x; do", '  echo "$x"', 'done <"$f" 2>/dev/null'), [3]),
        (_body('x="$(tr -d " " <"$f" 2>/dev/null | head -1)"'), [1]),
        (_body('a <"$f" 2>/dev/null; b 2>/dev/null <"$g"'), [1]),
        (_body("cat <<EOF 2>/dev/null", "text <f 2>/dev/null", "EOF"), []),
        (_body('gb_warn "write cmd <f 2>/dev/null"'), []),
        (_body("cat <<'EOF' >doc.txt", "cmd <f 2>/dev/null", "EOF"), []),
    ],
    ids=["loop", "substitution", "two-commands", "heredoc", "message", "doc-heredoc"],
)
def test_the_redirect_list_is_one_command(body: str, expected: list[int]) -> None:
    assert mod.violations(body) == expected


def test_an_annotation_with_a_reason_suppresses_and_one_without_does_not() -> None:
    line = 'cmd <"$f" 2>/dev/null'
    assert (
        mod.violations(f"{line} # allow-redirect-order: the open error is wanted\n")
        == []
    )
    assert mod.violations(f"{line} # allow-redirect-order\n") == [1]


def _run(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / "check_redirect_order.py"), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_main_reports_the_path_and_line_and_exits_one(tmp_path: Path) -> None:
    script = tmp_path / "read.sh"
    script.write_text('#!/bin/bash\nread -r x <"$f" 2>/dev/null\n', encoding="utf-8")
    result = _run(script)
    assert result.returncode == 1
    assert f"{script}:2:" in result.stdout + result.stderr


def test_a_file_the_grammar_cannot_parse_is_refused_not_passed(tmp_path: Path) -> None:
    script = tmp_path / "rw.sh"
    script.write_text('#!/bin/bash\nexec 3<>"$path" 2>/dev/null\n', encoding="utf-8")
    result = _run(script)
    assert result.returncode != 0
    assert "could not parse" in result.stdout + result.stderr
