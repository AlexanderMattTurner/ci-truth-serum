"""Tests for ci_truth_serum/check_case_default.py — the lint that requires every shell
`case … esac` block to carry a bare `*)` default arm, so an unexpected value
runs SOMETHING instead of silently falling through.

Drives ``violations()`` for the parsing rules and ``main()`` for the argv/exit-
code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_case_default.py", "check_case_default")


def _case(arms: str) -> str:
    return f'case "$1" in\n{arms}esac\n'


# ── flagged: no default arm ──────────────────────────────────────────────
def test_case_without_default_is_flagged_at_the_case_line() -> None:
    src = "#!/bin/bash\n" + _case("  major)\n    v=2 ;;\n  minor)\n    v=1 ;;\n")
    assert mod.violations(src) == [2]


def test_glob_arms_are_not_defaults() -> None:
    # `*.txt)` and `--*)` match subsets, not everything.
    src = _case("  *.txt)\n    t=1 ;;\n  --*)\n    o=1 ;;\n")
    assert mod.violations(src) == [1]


def test_two_incomplete_cases_both_flagged() -> None:
    src = _case("  a)\n    : ;;\n") + _case("  b)\n    : ;;\n")
    assert mod.violations(src) == [1, 5]


# ── not flagged: a default arm in any accepted spelling ──────────────────
@pytest.mark.parametrize(
    "arms",
    [
        "  a)\n    : ;;\n  *)\n    die unknown ;;\n",
        "  a)\n    : ;;\n  * )\n    : ;;\n",  # spaced
        "  a)\n    : ;;\n  (*)\n    : ;;\n",  # paren-wrapped
        "  a|*)\n    : ;;\n",  # bare * as one alternative
        "  a | * | b)\n    : ;;\n",  # spaced alternative list
        "  a) x ;; *) y ;;\n",  # compact same-line arms
    ],
)
def test_default_arm_spellings_pass(arms: str) -> None:
    assert mod.violations(_case(arms)) == []


def test_nested_case_blocks_tracked_independently() -> None:
    src = (
        'case "$1" in\n'
        "  a)\n"
        '    case "$2" in\n'
        "      x)\n"
        "        : ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *)\n"
        "    : ;;\n"
        "esac\n"
    )
    # Outer has a default; the inner (line 3) does not.
    assert mod.violations(src) == [3]


# ── quoted/commented shapes are data, not case blocks ────────────────────
@pytest.mark.parametrize(
    "src",
    [
        'echo "case x in"\n',  # case quoted in a string
        "# case $x in a) ;; esac\n",  # commented out
        'echo "esac"\n',
        'echo "case $x in a) : ;; esac"\n',  # a whole quoted block is data
    ],
)
def test_quoted_shapes_pass(src: str) -> None:
    assert mod.violations(src) == []


# ── regression: single-line case … esac is a real block and is checked ───
def test_single_line_case_without_default_is_flagged() -> None:
    """A one-line `case … esac` is the same block bash runs — the pre-AST
    stack scanner pushed a frame on the `case` opener and `continue`d before
    ever seeing the same-line `esac`, leaking the frame and never checking
    the block."""
    assert mod.violations('case "$1" in a) : ;; esac\n') == [1]


def test_single_line_case_with_default_passes() -> None:
    assert mod.violations('case "$1" in a) : ;; *) die x ;; esac\n') == []


# ── opt-out ──────────────────────────────────────────────────────────────
def test_opt_out_on_case_line() -> None:
    src = 'case "$1" in # case-default-ok: only these two values exist\n  a)\n    : ;;\nesac\n'
    assert mod.violations(src) == []


def test_opt_out_on_line_above() -> None:
    src = (
        '# case-default-ok: fallthrough intended\ncase "$1" in\n  a)\n    : ;;\nesac\n'
    )
    assert mod.violations(src) == []


# ── main ─────────────────────────────────────────────────────────────────
def test_main_reports_and_exits_nonzero(tmp_path, capsys) -> None:
    p = tmp_path / "s.sh"
    p.write_text(_case("  a)\n    : ;;\n"))
    assert mod.main([str(p)]) == 1
    assert f"{p}:1:" in capsys.readouterr().err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    p = tmp_path / "s.sh"
    p.write_text(_case("  a)\n    : ;;\n  *)\n    : ;;\n"))
    assert mod.main([str(p)]) == 0
