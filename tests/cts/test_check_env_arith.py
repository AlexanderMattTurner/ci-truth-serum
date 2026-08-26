"""Tests for ci_truth_serum/check_env_arith.py — the lint banning an ALL-CAPS
variable the script never assigns from appearing inside bash `$(( ))`
arithmetic.

Drives ``violations()`` for the parsing rules and ``main()`` for the argv/exit-
code contract, plus the shell-lint-parsing probes: the banned idiom inside a
message string and inside a heredoc body must both pass clean.
"""

import subprocess
import sys

import pytest

from tests._helpers import REPO_ROOT, load_hook

mod = load_hook("check_env_arith.py", "check_env_arith")


# ── flagged: an unassigned ALL-CAPS name inside $(( )) ─────────────────────
def test_flags_the_canonical_offender() -> None:
    text = "deadline=$((SECONDS + ${FOO:-90}))\n"
    assert mod.violations(text) == [1]


def test_flags_bare_var_form() -> None:
    assert mod.violations("n=$((BAR * 2))\n") == [1]


def test_flags_a_name_assigned_only_later() -> None:
    assert mod.violations("n=$((FOO + 1))\nFOO=5\n") == [1]


def test_arith_inside_quotes_still_flagged() -> None:
    text = 'echo "waiting $((SECONDS + ${FOO:-90}))s"\n'
    assert mod.violations(text) == [1]


def test_multiline_arithmetic_expansion_is_still_seen() -> None:
    # The old line scanner could not see a $(( )) spanning physical lines;
    # the grammar joins them into one expansion.
    text = "n=$((FOO +\n  1))\n"
    assert mod.violations(text) == [1]


# ── not flagged: bound before use, in every binding shape the grammar sees ─
def test_passes_the_bound_variable_rewrite() -> None:
    text = (
        'foo_timeout="$(int_or "${FOO:-90}" 90)"\ndeadline=$((SECONDS + foo_timeout))\n'
    )
    assert mod.violations(text) == []


def test_plain_assignment_earlier_exempts() -> None:
    assert mod.violations("FOO=5\nn=$((FOO + 1))\n") == []


def test_for_loop_variable_exempts() -> None:
    text = "for FOO in a b; do\n  n=$((FOO+1))\ndone\n"
    assert mod.violations(text) == []


def test_read_target_exempts() -> None:
    assert mod.violations("read FOO\nn=$((FOO+1))\n") == []


def test_read_with_flags_exempts_the_real_target() -> None:
    text = 'read -t 5 -p "prompt" FOO\nn=$((FOO+1))\n'
    assert mod.violations(text) == []


def test_mapfile_target_exempts() -> None:
    # `-t` is a value-taking option for `read`'s timeout but a bare boolean
    # flag for `mapfile`'s trim — the value-opts list is shared and textual
    # (a documented blind spot), so a bare-flag `mapfile FOO` is the case
    # this exempts; `mapfile -t FOO` still misreads `FOO` as `-t`'s value.
    assert mod.violations("mapfile FOO < f\nn=$((FOO+1))\n") == []


def test_declare_without_value_exempts() -> None:
    assert mod.violations("declare -i FOO\nn=$((FOO+1))\n") == []


def test_printf_v_target_exempts() -> None:
    text = 'printf -v FOO "%s" x\nn=$((FOO+1))\n'
    assert mod.violations(text) == []


@pytest.mark.parametrize("builtin", ["SECONDS", "RANDOM", "LINENO", "BASHPID"])
def test_bash_builtins_are_exempt(builtin: str) -> None:
    assert mod.violations(f"n=$(({builtin} + 1))\n") == []


def test_command_substitution_is_not_arithmetic() -> None:
    assert mod.violations("x=$(cmd FOO)\n") == []


# ── comments never contribute a finding ────────────────────────────────────
def test_comment_lines_are_ignored() -> None:
    text = "# example: deadline=$((SECONDS + ${FOO:-90}))\n"
    assert mod.violations(text) == []


def test_trailing_comment_is_ignored() -> None:
    text = "x=1 # was $((SECONDS + ${FOO:-90}))\n"
    assert mod.violations(text) == []


def test_empty_file_is_clean() -> None:
    assert mod.violations("") == []


# ── non-vacuity: the detector actually distinguishes the two shapes ────────
def test_detector_is_non_vacuous() -> None:
    assert mod.violations("n=$((FOO + 1))\n") == [1]
    assert mod.violations("FOO=1\nn=$((FOO + 1))\n") == []


# ── opt-out ──────────────────────────────────────────────────────────────
def test_opt_out_with_reason_exempts_the_line() -> None:
    text = "deadline=$((SECONDS + ${FOO:-90}))  # env-arith-ok: build-time constant\n"
    assert mod.violations(text) == []


def test_opt_out_without_reason_does_not_exempt() -> None:
    text = "deadline=$((SECONDS + ${FOO:-90}))  # env-arith-ok:\n"
    assert mod.violations(text) == [1]


def test_opt_out_on_closing_line_of_a_multiline_expansion() -> None:
    text = "n=$((FOO +\n  1))  # env-arith-ok: reason\n"
    assert mod.violations(text) == []


# ── the two shell-lint-parsing probes ──────────────────────────────────────
def test_probe_message_string_does_not_fire() -> None:
    # Single-quoted: bash performs no expansion inside `'...'`, so the
    # grammar carries no `arithmetic_expansion` node here at all — the text
    # is inert, unlike the same idiom inside a DOUBLE-quoted string (which
    # genuinely expands at runtime; see test_arith_inside_quotes_still_flagged).
    text = "gb_warn 'example: deadline=$((SECONDS + ${FOO:-90}))'\n"
    assert mod.violations(text) == []


def test_probe_heredoc_body_does_not_fire() -> None:
    text = "cat <<EOF > doc.txt\ndeadline=$((SECONDS + ${FOO:-90}))\nEOF\n"
    assert mod.violations(text) == []


# ── main() argv/exit-code contract ─────────────────────────────────────────
def test_main_reports_and_exits_nonzero(tmp_path, capsys) -> None:
    p = tmp_path / "s.bash"
    p.write_text("deadline=$((SECONDS + ${FOO:-90}))\n", encoding="utf-8")
    assert mod.main([str(p)]) == 1
    err = capsys.readouterr().err
    assert f"{p}:1:" in err
    assert "set -e" in err
    assert "env-arith-ok" in err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    p = tmp_path / "s.bash"
    p.write_text("FOO=5\nn=$((FOO + 1))\n", encoding="utf-8")
    assert mod.main([str(p)]) == 0


def test_main_skips_unreadable_path(tmp_path) -> None:
    assert mod.main([str(tmp_path / "missing.bash")]) == 0


def test_empty_argv_exits_2_via_run_file_cli() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ci_truth_serum.check_env_arith"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "no files to scan" in proc.stderr
