"""Tests for ci_truth_serum/check_bare_mkdir.py — the lint that flags a bare
`mkdir -p` (unverified against BSD's dangling-symlink lie).

Drives ``violations()`` for the parsing rules and ``main()`` for the argv/exit-
code contract, plus the shell-lint-parsing probes: the banned idiom inside a
message string and inside a heredoc body must both pass clean.
"""

import subprocess
import sys

import pytest

from tests._helpers import REPO_ROOT, load_hook

mod = load_hook("check_bare_mkdir.py", "check_bare_mkdir")


# ── flagged: a `-p`-carrying flag word reaches `mkdir` ────────────────────
@pytest.mark.parametrize(
    "line",
    [
        'mkdir -p "$dir"',
        "mkdir -pm 700 x",  # glued flag cluster
        "mkdir -mp x",  # p not first in the cluster
        'mkdir -m 700 -p "$x"',  # -p after another flag word
        'mkdir --parents "$x"',  # GNU long form
        '(umask 077 && mkdir -p "$dir")',  # subshell, after &&
        'as_root mkdir -p "$HOOK_DIR"',  # wrapped invocation
        '  mkdir -p "$a" "$b"',  # leading whitespace, two targets
    ],
)
def test_detects_p_variants(line: str) -> None:
    assert mod.violations(line + "\n") == [1]


# ── not flagged ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "line",
    [
        'mkdir "$dir"',  # no -p: fails loudly on its own, not flagged
        'mkdir -m 700 "$dir"',  # a flag cluster without p
        "rmdir -p x",  # not mkdir
        "sbx-mkdir -p x",  # mkdir as a suffix of another word
        "gb_ensure_dir=/usr/bin/mkdir",  # path mention, no invocation
        'mkdir "$a"; touch -p_flagless',  # -p-looking token after a separator
        'mkdir "$x" | filter -p',  # -p belongs to the next command in the pipe
        "# mkdir -p returns 0 on BSD",  # comment, not code
        'touch x # then mkdir -p "$y"',  # trailing comment
    ],
)
def test_ignores_non_violations(line: str) -> None:
    assert mod.violations(line + "\n") == []


def test_empty_file_is_clean() -> None:
    assert mod.violations("") == []


def test_wrapped_bash_c_string_is_a_documented_blind_spot() -> None:
    # `bash -c "mkdir -p \"$x\""` is one `command` node named `bash`; the
    # quoted script is a string argument, not a second parsed program, so it
    # is not seen. This is the blind spot the module docstring names.
    text = 'bash -c "mkdir -p \\"$x\\""\n'
    assert mod.violations(text) == []


# ── non-vacuity: the detector actually distinguishes the two shapes ────────
def test_detector_is_non_vacuous() -> None:
    assert mod.violations('mkdir -p "$dir"\n') == [1]
    assert mod.violations('mkdir "$dir"\n') == []


# ── post-condition verified nearby: not a violation ─────────────────────────
@pytest.mark.parametrize(
    "check",
    [
        '[[ -d "$dir" ]] || exit 1',
        '[ -d "$dir" ] || exit 1',
        'test -d "$dir" || exit 1',
    ],
)
def test_verified_on_next_line_is_clean(check: str) -> None:
    text = f'mkdir -p "$dir"\n{check}\n'
    assert mod.violations(text) == []


def test_verified_in_same_statement_via_and_is_clean() -> None:
    text = 'mkdir -p "$dir" && [[ -d "$dir" ]] || exit 1\n'
    assert mod.violations(text) == []


def test_verified_inside_a_function_body_is_clean() -> None:
    text = (
        "run_shard() {\n"
        '  mkdir -p "$CLAUDE_CONFIG_DIR"\n'
        '  [[ -d "$CLAUDE_CONFIG_DIR" ]] || die x\n'
        "}\n"
    )
    assert mod.violations(text) == []


def test_verified_for_a_different_directory_still_fires() -> None:
    text = 'mkdir -p "$dir"\n[[ -d "$other" ]] || exit 1\n'
    assert mod.violations(text) == [1]


def test_verified_past_the_window_still_fires() -> None:
    text = 'mkdir -p "$dir"\necho "unrelated"\n[[ -d "$dir" ]] || exit 1\n'
    assert mod.violations(text) == [1]


def test_negated_test_does_not_count_as_verification() -> None:
    text = 'mkdir -p "$dir"\n[[ ! -d "$dir" ]] && exit 1\n'
    assert mod.violations(text) == [1]


def test_only_the_unverified_of_several_creates_is_flagged() -> None:
    text = 'mkdir -p "$a"\nmkdir -p "$b"\n[[ -d "$b" ]] || exit 1\n'
    assert mod.violations(text) == [1]


# ── opt-out ──────────────────────────────────────────────────────────────
def test_opt_out_with_reason_exempts_the_line() -> None:
    text = 'mkdir -p "$a" # bare-mkdir-ok: guest image, msg.bash not shipped\n'
    assert mod.violations(text) == []


def test_opt_out_without_reason_does_not_exempt() -> None:
    text = 'mkdir -p "$a" # bare-mkdir-ok:\n'
    assert mod.violations(text) == [1]


def test_opt_out_on_line_above() -> None:
    text = '# bare-mkdir-ok: cannot source msg.bash here\nmkdir -p "$a"\n'
    assert mod.violations(text) == []


# ── the two shell-lint-parsing probes ──────────────────────────────────────
def test_probe_message_string_does_not_fire() -> None:
    # The banned idiom quoted inside a message a command PRINTS is data, not
    # code — no `command` node names `mkdir` there.
    text = 'gb_warn "use mkdir -p \\"$dir\\" here"\n'
    assert mod.violations(text) == []


def test_probe_heredoc_body_does_not_fire() -> None:
    text = 'cat <<EOF > doc.txt\nmkdir -p "$dir"\nEOF\n'
    assert mod.violations(text) == []


# ── main() argv/exit-code contract ─────────────────────────────────────────
def test_main_reports_and_exits_nonzero(tmp_path, capsys) -> None:
    p = tmp_path / "s.sh"
    p.write_text('mkdir -p "$dir"\n')
    assert mod.main([str(p)]) == 1
    err = capsys.readouterr().err
    assert f"{p}:1:" in err
    assert "dangling symlink" in err
    assert "bare-mkdir-ok" in err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    p = tmp_path / "s.sh"
    p.write_text('mkdir "$dir"\n')
    assert mod.main([str(p)]) == 0


def test_main_skips_unreadable_path(tmp_path) -> None:
    assert mod.main([str(tmp_path / "missing.sh")]) == 0


def test_empty_argv_exits_2_via_run_file_cli() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ci_truth_serum.check_bare_mkdir"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "no files to scan" in proc.stderr
