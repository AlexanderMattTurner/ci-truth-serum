"""Tests for ci_truth_serum/check_flag_arity.py — the pre-commit lint that flags a
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


def test_read_before_guard_on_same_line_is_flagged() -> None:
    # The arity guard follows the $2 read, so $2 is dereferenced raw (and crashes
    # under set -u) before the guard ever runs.
    body = '  --x)\n    X="$2"; [[ $# -ge 2 ]] || die\n    shift 2\n    ;;'
    assert _flagged_lines(_parser(body)) == [5]


def test_guard_before_read_on_same_line_passes() -> None:
    # Mirror image: the same tokens in the correct order (guard, then read) pass.
    body = '  --x)\n    [[ $# -ge 2 ]] || die; X="$2"; shift 2\n    ;;'
    assert _flagged_lines(_parser(body)) == []


# ── C9: the guard must prove the arity the arm actually reads ────────────
def test_guard_proving_less_than_read_requires_is_flagged() -> None:
    # `[[ $# -ge 2 ]] || die` proves only $# >= 2, but the arm goes on to read $3
    # and `shift 3` (needs $# >= 3). The lower read $2 is cleared; the raw $3 is not.
    body = '  --two)\n    [[ $# -ge 2 ]] || die; A="$2"; B="$3"; shift 3\n    ;;'
    hits = mod.violations(_parser(body))
    assert [line for line, _ in hits] == [5]  # the B="$3" read
    assert "$# >= 3" in hits[0][1]


def test_guard_proving_exact_arity_the_arm_reads_passes() -> None:
    # Green control: a `-ge 3` guard covers the $3 read and shift 3.
    body = '  --two)\n    [[ $# -ge 3 ]] || die; A="$2"; B="$3"; shift 3\n    ;;'
    assert _flagged_lines(_parser(body)) == []
    # A `> 2` (i.e. >= 3) guard is equivalent.
    body_gt = '  --two)\n    [[ $# -gt 2 ]] || die; B="$3"; shift 3\n    ;;'
    assert _flagged_lines(_parser(body_gt)) == []


def test_if_guard_proving_less_than_read_requires_is_flagged() -> None:
    body = (
        '  --two)\n    if [[ $# -lt 2 ]]; then die; fi\n    B="$3"\n    shift 3\n    ;;'
    )
    assert _flagged_lines(_parser(body)) == [6]  # the B="$3" read


# ── C11: a locally-defined bail name that only warns is not a guard ──────
def test_local_warn_only_bail_name_is_not_trusted() -> None:
    # `error()` is defined in this script and only prints — it does NOT abort — so
    # `[[ $# -ge 2 ]] || error` is not a real guard and the following $2 is unguarded.
    src = (
        "#!/usr/bin/env bash\n"
        'error() { printf "%s\\n" "$1"; }\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in\n'
        '  --b) [[ $# -ge 2 ]] || error "need"; X="$2"; shift 2 ;;\n'
        "  esac\n"
        "done\n"
    )
    assert _flagged_lines(src) == [5]  # X="$2" is unguarded


def test_local_bail_name_that_actually_aborts_is_trusted() -> None:
    # Green control: the same helper, but its body exits — now it is a real bail, so
    # `[[ $# -ge 2 ]] || error` guards the $2 read.
    src = (
        "#!/usr/bin/env bash\n"
        'error() { printf "%s\\n" "$1"; exit 1; }\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in\n'
        '  --b) [[ $# -ge 2 ]] || error "need"; X="$2"; shift 2 ;;\n'
        "  esac\n"
        "done\n"
    )
    assert _flagged_lines(src) == []


def test_external_bail_name_is_still_trusted() -> None:
    # A conventional bail name NOT defined in this script (sourced from a lib) is
    # trusted as before — we cannot see its body, so the convention stands.
    body = '  --b) [[ $# -ge 2 ]] || die "need"; X="$2"; shift 2 ;;'
    assert _flagged_lines(_parser(body)) == []


def test_bare_arity_test_without_bail_is_not_a_guard() -> None:
    # `[[ $# -ge 2 ]]` whose result is discarded (no || die / && die / then die)
    # does not stop the read, so the following $2 is still unguarded.
    body = '  --x)\n    [[ $# -ge 2 ]]\n    X="$2"\n    shift 2\n    ;;'
    assert _flagged_lines(_parser(body)) == [6]


def test_negative_bail_with_ampersand_consequent_passes() -> None:
    body = '  --x)\n    [[ $# -lt 2 ]] && die "--x needs a value"\n    X="$2"\n    shift 2\n    ;;'
    assert _flagged_lines(_parser(body)) == []


def test_multiline_if_then_guard_passes() -> None:
    # The bail lives on its own line inside `if …; then … fi` — the common real
    # idiom. The opener parks the arm pending; the `die` on the next line resolves it.
    body = (
        "  --x)\n"
        "    if [[ $# -lt 2 ]]; then\n"
        '      die "--x needs a value"\n'
        "    fi\n"
        '    X="$2"\n'
        "    shift 2\n"
        "    ;;"
    )
    assert _flagged_lines(_parser(body)) == []


def test_multiline_if_then_with_multi_statement_body_passes() -> None:
    # An `echo` before the `exit` in the then-body must not derail resolution.
    body = (
        "  --x)\n"
        "    if [[ $# -lt 2 ]]; then\n"
        '      echo "usage: --x VALUE" >&2\n'
        "      exit 1\n"
        "    fi\n"
        '    X="$2"\n'
        "    ;;"
    )
    assert _flagged_lines(_parser(body)) == []


def test_multiline_positive_bail_line_continuation_passes() -> None:
    # `[[ $# -ge 2 ]] ||` with the exiting command on the continuation line.
    body = '  --x)\n    [[ $# -ge 2 ]] ||\n      die "--x needs a value"\n    X="$2"\n    ;;'
    assert _flagged_lines(_parser(body)) == []


def test_multiline_if_then_that_never_bails_is_still_flagged() -> None:
    # A then-body that only warns (no exit) does not stop the read: `if too few, warn`
    # then fall through to `$2` still crashes. The read must remain flagged.
    body = (
        "  --x)\n"
        "    if [[ $# -lt 2 ]]; then\n"
        '      echo "warning: maybe missing" >&2\n'
        "    fi\n"
        '    X="$2"\n'
        "    ;;"
    )
    assert _flagged_lines(_parser(body)) == [8]  # the X="$2" line


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
        [sys.executable, "-m", "ci_truth_serum.check_flag_arity", "--all"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
