"""Tests for hooks/check_flag_arity.py — the pre-commit lint that flags a
value-taking CLI flag arm which consumes ``$2`` / ``shift 2`` without proving
the value exists.

Drives ``violations()`` directly for the parsing rules and ``main()`` for the
argv/exit-code contract. The scaffold mirrors the shape the lint must catch:
an outer ``while [[ $# -gt 0 ]]`` that proves only ``$1``.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, load_hook

mod = load_hook("check_flag_arity.py", "check_flag_arity")


def _flagged_lines(src: str) -> "list[int]":
    return [line for line, _ in mod.violations(src)]


def _parser(body: str) -> str:
    """Wrap a case-arm body in the standard while/case scaffold so the arm's
    outer-loop guard proves only $1 — exactly the shape the lint must catch."""
    return (
        "#!/usr/bin/env bash\n"
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in\n'
        f"{body}\n"
        "  *) shift ;;\n"
        "  esac\n"
        "done\n"
    )


def test_bare_positional_and_shift_flagged_once_at_the_read() -> None:
    hits = mod.violations(_parser('  --branch)\n    BRANCH="$2"\n    shift 2\n    ;;'))
    assert len(hits) == 1
    assert hits[0][0] == 5  # the BRANCH="$2" line
    assert "arity guard" in hits[0][1]


@pytest.mark.parametrize(
    "body, line",
    [
        ('  --branch)\n    BRANCH="$2"\n    shift 2\n    ;;', 5),
        ("  --x)\n    shift 2\n    ;;", 5),  # shift 2 alone, no $2 read
        ("  --x)\n    shift 3\n    ;;", 5),  # shift N>=2
        ('  --x)\n    Y="${2}"\n    shift 2\n    ;;', 5),  # ${2} brace form
        ('  --x)\n    Y="$3"\n    ;;', 5),  # a higher positional
        ('  -f | --file)\n    FF="$2"\n    shift 2\n    ;;', 5),  # multi-alt label
        ('  --privacy=*)\n    M="$2"\n    ;;', 5),  # glob flag label
    ],
)
def test_unguarded_value_flag_arms_are_flagged(body: str, line: int) -> None:
    assert _flagged_lines(_parser(body)) == [line]


# Every accepted guard idiom => zero findings. One member per idiom so a dropped
# branch of _has_arity_guard / _calls_allowlisted_helper / _reads_self_guarded is
# caught.
@pytest.mark.parametrize(
    "name, body",
    [
        (
            "positive [[ $# -ge 2 ]] || die",
            '  --a)\n    [[ $# -ge 2 ]] || die "--a needs a value"\n    A="$2"\n    shift 2\n    ;;',
        ),
        (
            "[[ $# -gt 1 ]]",
            '  --a)\n    [[ $# -gt 1 ]] || die x\n    A="$2"\n    shift 2\n    ;;',
        ),
        (
            "(( $# >= 2 ))",
            '  --a)\n    (( $# >= 2 )) || die x\n    A="$2"\n    shift 2\n    ;;',
        ),
        (
            "negative bail [[ $# -lt 2 ]]",
            '  --a)\n    if [[ $# -lt 2 ]]; then die x; fi\n    A="$2"\n    shift 2\n    ;;',
        ),
        (
            'negative bail quoted [[ "$#" -lt 2 ]]',
            '  --a)\n    if [[ "$#" -lt 2 ]]; then die x; fi\n    A="$2"\n    shift 2\n    ;;',
        ),
        (
            "-le 1 bail",
            '  --a)\n    if [[ $# -le 1 ]]; then die x; fi\n    A="$2"\n    shift 2\n    ;;',
        ),
        ("self-guard ${2:?…}", '  --b) B="${2:?--b needs a value}"; shift 2 ;;'),
        ("default ${2:-x}", '  --c) C="${2:-x}"; shift 2 ;;'),
        ("assign-default ${2:=x}", '  --c) C="${2:=x}"; shift 2 ;;'),
        (
            "need_val helper",
            '  --d)\n    need_val "$@"\n    D="$2"\n    shift 2\n    ;;',
        ),
        (
            "need_arg helper",
            '  --d)\n    need_arg "$@"\n    D="$2"\n    shift 2\n    ;;',
        ),
    ],
)
def test_guarded_arm_passes(name: str, body: str) -> None:
    assert _flagged_lines(_parser(body)) == [], name


# Value reads outside a flag-labelled arm are never the target.
@pytest.mark.parametrize(
    "name, src",
    [
        (
            "subcommand dispatch doctor)",
            'case "$1" in\ndoctor)\n  sub="$2"\n  shift 2\n  ;;\nesac',
        ),
        (
            "subcommand read) / write)",
            'case "$sub" in\nread) x="$2"; shift 2 ;;\nwrite) y="$2"; shift 2 ;;\nesac',
        ),
        ("catch-all *)", 'case "$1" in\n*)\n  rest="$2"\n  shift 2\n  ;;\nesac'),
        (
            "function-internal local x=$1; shift 2 (no case)",
            'f() {\n  local x="$1" y="$2"\n  shift 2\n}',
        ),
    ],
)
def test_not_flagged_outside_flag_arms(name: str, src: str) -> None:
    assert _flagged_lines(f"#!/usr/bin/env bash\n{src}\n") == [], name


def test_optout_with_reason_suppresses_same_and_preceding_line() -> None:
    same_line = _parser(
        '  --ok)\n    Z="$2" # flag-arity-ok: optional, defaulted below\n    shift 2\n    ;;'
    )
    assert _flagged_lines(same_line) == []
    prev_line = _parser(
        '  --ok)\n    # flag-arity-ok: optional, defaulted below\n    Z="$2"\n    shift 2\n    ;;'
    )
    assert _flagged_lines(prev_line) == []


def test_optout_with_empty_reason_is_itself_a_violation() -> None:
    hits = mod.violations(
        _parser('  --x)\n    W="$2" # flag-arity-ok:\n    shift 2\n    ;;')
    )
    assert len(hits) == 1
    assert hits[0][0] == 5
    assert "non-empty reason" in hits[0][1]


def test_dollar_hash_is_not_mistaken_for_a_comment() -> None:
    # strip_comment must keep `$#` intact so the arity guard is recognized.
    body = '  --a)\n    [[ $# -ge 2 ]] || die x # trailing note\n    A="$2"\n    ;;'
    assert _flagged_lines(_parser(body)) == []


def test_main_reads_files_from_argv_and_exit_code(tmp_path: Path) -> None:
    bad = tmp_path / "bad.sh"
    bad.write_text(_parser('  --branch)\n    BRANCH="$2"\n    shift 2\n    ;;'))
    good = tmp_path / "good.sh"
    good.write_text(
        _parser('  --branch)\n    B="${2:?need value}"\n    shift 2\n    ;;')
    )
    assert mod.main([str(bad)]) == 1
    assert mod.main([str(good)]) == 0


def test_main_ignores_a_nonexistent_path(tmp_path: Path) -> None:
    assert mod.main([str(tmp_path / "nope-does-not-exist.sh")]) == 0


def test_live_contract_all_over_the_repo_is_clean() -> None:
    # Dogfood: the pack's own tracked shell surface must pass flag-arity.
    proc = subprocess.run(
        [sys.executable, "-m", "hooks.check_flag_arity", "--all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
