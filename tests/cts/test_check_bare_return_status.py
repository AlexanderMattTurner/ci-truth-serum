"""Tests for ci_truth_serum/check_bare_return_status.py — the lint that requires an
explicit status on a `return` or `exit` guarded by `&&` or `||`, so the operator
stops deciding what the function hands back.

Drives ``violations()`` for the parsing rules and ``main()`` for the argv/exit-code
contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_bare_return_status.py", "check_bare_return_status")


def _fn(body: str) -> str:
    return f"f() {{\n{body}\n}}\n"


# ── flagged: `&&` always forwards 0, whatever the left side is ───────────────
@pytest.mark.parametrize(
    "guard",
    [
        '[[ -n "$x" ]] && return',
        '[ -f "$f" ] && return',
        'test -n "$x" && return',
        "(( n > 0 )) && return",
        "! is_ready && return",
        "grep -q foo file && return",  # a real command: `&&` is still always 0
        'realpath "$1" 2>/dev/null && return',
        "command -v uv >/dev/null 2>&1 && return",
        '[[ -n "$x" ]] && exit',
        "cmd && exit",
    ],
)
def test_and_guards_are_flagged(guard: str) -> None:
    assert mod.violations(_fn(f"  {guard}")) == [2]


# ── flagged: `||` after something that can only fail with status 1 ───────────
@pytest.mark.parametrize(
    "guard",
    [
        '[[ -n "$x" ]] || return',
        '[ -f "$f" ] || return',
        'test -n "$x" || return',
        "(( n > 0 )) || return",
        "! is_ready || return",
        '[[ -n "$x" ]] || exit',
    ],
)
def test_or_guards_after_a_test_are_flagged(guard: str) -> None:
    assert mod.violations(_fn(f"  {guard}")) == [2]


def test_the_real_incident_shape() -> None:
    """Three same-polarity guards and one flipped guard — the shape that made a
    session-start hook exit 1 on every session."""
    src = _fn(
        '  [[ "${A:-}" = "" ]] && return\n'
        '  [[ "${B:-}" = "yes" ]] && return\n'
        '  [[ "${C:-}" != "1" ]] || return'
    )
    assert mod.violations(src) == [2, 3, 4]


def test_several_guards_report_each_line() -> None:
    src = _fn('  [[ -n "$x" ]] && return\n  echo mid\n  (( n )) || return')
    assert mod.violations(src) == [2, 4]


# ── NOT flagged: `||` after a command forwards that command's own code ───────
@pytest.mark.parametrize(
    "guard",
    [
        "cmd || return",  # 127 when cmd is missing
        "bash -c 'exit 42' || return",  # 42, a value `return 1` would destroy
        "grep -q foo file || return",  # 2 on a grep error, 1 on no match
        'realpath "$1" 2>/dev/null || return',
        "retry_stdout gh api /rate_limit || return",
        "_ensure_github_apt_source || return",
        "cmd || exit",
        "a && b || return",  # a nested list is not a constant-failure operand
        "{ cmd; } || return",  # a brace group forwards its last command's status
        "( cmd ) || return",  # so does a subshell
    ],
)
def test_or_guards_after_a_command_pass(guard: str) -> None:
    assert mod.violations(_fn(f"  {guard}")) == []


# ── NOT flagged: the status is already written down ──────────────────────────
@pytest.mark.parametrize(
    "guard",
    [
        '[[ -n "$x" ]] && return 0',
        '[[ -n "$x" ]] || return 1',
        '[[ -n "$x" ]] && exit 0',
        "cmd || return 1",
        "(( n > 0 )) || return 2",
        'test -n "$x" && return "$rc"',
    ],
)
def test_explicit_status_passes(guard: str) -> None:
    assert mod.violations(_fn(f"  {guard}")) == []


# ── NOT flagged: no short-circuit operator states the intent ─────────────────
@pytest.mark.parametrize(
    "body",
    [
        '  if [[ -n "$x" ]]; then\n    return\n  fi',
        '  case "$1" in\n    a) return ;;\n    *) : ;;\n  esac',
        '  [[ -n "$x" ]]\n  return',
    ],
)
def test_returns_outside_a_short_circuit_pass(body: str) -> None:
    assert mod.violations(_fn(body)) == []


# ── quoted and heredoc shapes are data, not guards ───────────────────────────
@pytest.mark.parametrize(
    "src",
    [
        'gb_warn "[[ -n \\$x ]] && return"\n',
        "echo '[[ -n $x ]] || return'\n",
        "# [[ -n $x ]] && return\n",
        "cat <<'EOF' > doc.txt\n[[ -n $x ]] && return\n[[ -n $x ]] || return\nEOF\n",
    ],
)
def test_quoted_and_heredoc_shapes_pass(src: str) -> None:
    assert mod.violations(src) == []


# ── a continued list is one construct ────────────────────────────────────────
def test_continued_guard_is_reported_at_the_list_start() -> None:
    src = _fn('  [[ -n "$x" ]] &&\n    return')
    assert mod.violations(src) == [2]


def test_opt_out_reaches_the_continued_line() -> None:
    """The annotation window must cover the whole list, so the opt-out can sit on
    the `return` line of a continued guard."""
    src = _fn('  [[ -n "$x" ]] &&\n    return # allow-bare-return: caller wants 0')
    assert mod.violations(src) == []


# ── top level, not only inside a function ────────────────────────────────────
def test_guard_at_top_level_is_flagged() -> None:
    assert mod.violations('#!/bin/bash\n[[ -n "$X" ]] && exit\n') == [2]


# ── opt-out ──────────────────────────────────────────────────────────────────
def test_opt_out_on_the_guard_line() -> None:
    src = _fn("  cmd && return # allow-bare-return: 0 is the documented result")
    assert mod.violations(src) == []


def test_opt_out_on_the_line_above() -> None:
    src = _fn("  # allow-bare-return: 0 is the documented result\n  cmd && return")
    assert mod.violations(src) == []


def test_opt_out_without_a_reason_does_not_suppress() -> None:
    src = _fn("  cmd && return # allow-bare-return")
    assert mod.violations(src) == [2]


def test_a_similar_token_does_not_suppress() -> None:
    src = _fn("  cmd && return # allow-bare-returns: typo in the token")
    assert mod.violations(src) == [2]


# ── main ─────────────────────────────────────────────────────────────────────
def test_main_reports_and_exits_nonzero(tmp_path, capsys) -> None:
    p = tmp_path / "s.sh"
    p.write_text('f() {\n  [[ -n "$x" ]] || return\n}\n')
    assert mod.main([str(p)]) == 1
    assert f"{p}:2:" in capsys.readouterr().err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    p = tmp_path / "s.sh"
    p.write_text('f() {\n  [[ -n "$x" ]] || return 1\n}\n')
    assert mod.main([str(p)]) == 0
