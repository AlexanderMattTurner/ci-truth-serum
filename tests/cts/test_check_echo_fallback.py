"""Tests for ci_truth_serum/check_echo_fallback.py — the lint that bans `|| echo` /
`|| printf` fallbacks which convert a failure into a benign parseable string
(inside command substitutions, and as unaborted bare statements).

Drives ``violations()`` for the rules and ``main()`` for the argv/exit-code
contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_echo_fallback.py", "check_echo_fallback")


# ── flagged: fallback inside a substitution ──────────────────────────────
@pytest.mark.parametrize(
    "src",
    [
        'v=$(git describe || echo "error")\n',
        'diff=$(git diff "$a" "$b" || echo "Unable to get diff")\n',
        'v=$(cmd || printf "0.0.0")\n',
        "v=`cmd || echo fallback`\n",
        # exit inside a substitution only exits the subshell — still flagged
        'v=$(cmd || echo "x"; exit 1)\n',
        # multi-line substitution joined and flagged at its first line
        'v=$(curl -s url \\\n  || echo "000")\n',
    ],
)
def test_fallback_inside_substitution_is_flagged(src: str) -> None:
    assert mod.violations(src) == [1]


# ── flagged: bare statement that narrates but does not abort ─────────────
@pytest.mark.parametrize(
    "src",
    [
        'do_deploy || echo "deploy failed"\n',
        'make test || printf "tests failed"\n',
    ],
)
def test_bare_unaborted_fallback_is_flagged(src: str) -> None:
    assert mod.violations(src) == [1]


# ── legitimate corpus: ZERO findings ─────────────────────────────────────
@pytest.mark.parametrize(
    "src",
    [
        # message to stderr — diagnostics, not a value
        'cmd || echo "cmd failed" >&2\n',
        'v=$(cmd || echo "warn" >&2)\n',
        # narrate AND abort — a real recovery
        'cmd || { echo "failed" >&2; exit 1; }\n',
        'cmd || { echo "failed"; exit 1; }\n',
        'find_config || { echo "no config"; return 1; }\n',
        # no fallback at all
        "v=$(git describe)\n",
        "cmd || exit 1\n",
        "a || b\n",
        # message-printing line quoting the idiom
        'echo "usage: v=$(cmd || echo fallback)"\n',
        # comment quoting the idiom
        '# bad: v=$(cmd || echo "error")\n',
    ],
)
def test_legitimate_corpus_yields_zero_findings(src: str) -> None:
    assert mod.violations(src) == []


# ── opt-out ──────────────────────────────────────────────────────────────
def test_opt_out_same_line() -> None:
    src = 'code=$(curl -w "%{http_code}" url || echo "000") # echo-fallback-ok: 000 is the documented curl-failure sentinel\n'
    assert mod.violations(src) == []


def test_opt_out_line_above() -> None:
    src = '# echo-fallback-ok: sentinel the caller branches on\ncode=$(cmd || echo "000")\n'
    assert mod.violations(src) == []


def test_multiple_violations_report_each_line() -> None:
    src = 'a=$(x || echo "1")\n:\nb || echo "2"\n'
    assert mod.violations(src) == [1, 3]


# ── main ─────────────────────────────────────────────────────────────────
def test_main_reports_path_line_and_exits_nonzero(tmp_path, capsys) -> None:
    p = tmp_path / "s.sh"
    p.write_text('v=$(cmd || echo "error")\n')
    assert mod.main([str(p)]) == 1
    assert f"{p}:1:" in capsys.readouterr().err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    p = tmp_path / "s.sh"
    p.write_text('cmd || { echo "failed" >&2; exit 1; }\n')
    assert mod.main([str(p)]) == 0
