"""Tests for ci_truth_serum/check_rename_onto_symlink.py — the lint that bans
`mv "$f.tmp" "$f"`, the atomic write that replaces a destination symlink instead
of following it.

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

mod = load_hook("check_rename_onto_symlink.py", "check_rename_onto_symlink")

_BAD = 'mv "$cfg.tmp" "$cfg"\n'


@pytest.mark.parametrize(
    "line",
    [
        # the idiom, in several suffixes
        'mv "$f.tmp" "$f"',
        'mv "$f.seed-tmp" "$f"',
        'mv "$dest.part" "$dest"',
        # quoting does not change which path is renamed onto which
        "mv $f.tmp $f",
        'mv "${cfg}.tmp" "${cfg}"',
        'mv "$f".tmp "$f"',
        # a leading `~` expands per user, as a variable does
        "mv ~/.npmrc.tmp ~/.npmrc",
        # a positional parameter is a path the caller chose
        'mv "$1.tmp" "$1"',
        'mv "${2}.tmp" "${2}"',
        # a destination the shell computes
        'mv "$(pwd).tmp" "$(pwd)"',
        # options do not move which operands are the source and the destination
        'mv -f "$f.tmp" "$f"',
        'mv -- "$f.partial" "$f"',
        # a wrapper does not change what is renamed onto what
        'sudo mv "$f.tmp" "$f"',
        'as_root mv "$managed.tmp" "$managed"',
        '/bin/mv "$f.tmp" "$f"',
        # an expansion plus literal text is still a path the user reaches
        'mv "$HOME/.config.json.tmp" "$HOME/.config.json"',
        # a command inside a substitution is a command
        'out=$(mv "$f.tmp" "$f")',
    ],
)
def test_a_rename_onto_its_own_stem_is_flagged(line: str) -> None:
    assert mod.violations(line + "\n") == [1]


@pytest.mark.parametrize(
    "name, src",
    [
        ("a resolved destination", 'mv "$f.tmp" "$dest"\n'),
        ("a move-aside with no shared stem", 'mv "$live" "$parked"\n'),
        ("a rename that ADDS a suffix", 'mv "$f" "$f.disabled"\n'),
        ("a literal destination", 'mv "/tmp/build.tmp" "/tmp/build"\n'),
        ("a bare literal destination", "mv out.tmp out\n"),
        ("single quotes stop the expansion", "mv '$f.tmp' '$f'\n"),
        ("double quotes stop the tilde", 'mv "~/.npmrc.tmp" "~/.npmrc"\n'),
        ("a source in another directory", 'mv "$staging/f.tmp" "$f"\n'),
        ("one operand", 'mv "$f.tmp"\n'),
        ("a different command", 'cp "$f.tmp" "$f"\n'),
        ("a lookup", "command -v mv\n"),
        ("an unquoted word list a command prints", 'echo mv "$f.tmp" "$f"\n'),
        # The two probes: text a command prints, and data written to a file.
        ("a logger's message string", 'log_warn "run mv \\"$f.tmp\\" \\"$f\\""\n'),
        (
            "a heredoc body",
            'cat <<\'EOF\' > doc.txt\nmv "$f.tmp" "$f"\nEOF\n',
        ),
        ("a comment", '# mv "$f.tmp" "$f"\n'),
    ],
)
def test_a_rename_that_cannot_detach_a_link_passes(name: str, src: str) -> None:
    assert mod.violations(src) == [], name


def test_each_rename_is_reported_at_its_own_line() -> None:
    assert mod.violations(_BAD + 'mv "$other.tmp" "$other"\n') == [1, 2]


def test_two_renames_on_one_line_report_that_line_once() -> None:
    assert mod.violations('mv "$a.tmp" "$a"; mv "$b.tmp" "$b"\n') == [1]


def test_a_continued_command_is_reported_at_its_first_line() -> None:
    assert mod.violations('mv -f \\\n  "$f.tmp" \\\n  "$f"\n') == [1]


# ── the opt-out ──────────────────────────────────────────────────────────
def test_an_annotated_rename_passes() -> None:
    src = 'mv -T "$link.new" "$link" # allow-rename-onto-symlink: replacing it is the job\n'
    assert mod.violations(src) == []


def test_the_opt_out_works_on_the_line_above() -> None:
    assert mod.violations("# allow-rename-onto-symlink: scratch\n" + _BAD) == []


def test_the_opt_out_works_on_the_last_line_of_a_continued_command() -> None:
    src = 'mv -f \\\n  "$f.tmp" \\\n  "$f" # allow-rename-onto-symlink: scratch\n'
    assert mod.violations(src) == []


def test_an_opt_out_with_no_reason_does_not_suppress() -> None:
    assert mod.violations('mv "$f.tmp" "$f" # allow-rename-onto-symlink\n') == [1]


# ── the argv/exit-code contract ──────────────────────────────────────────
def _run(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / "check_rename_onto_symlink.py"), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_main_reports_the_path_and_line_and_exits_one(tmp_path: Path) -> None:
    script = tmp_path / "seed.sh"
    script.write_text("#!/bin/bash\n" + _BAD, encoding="utf-8")
    result = _run(script)
    assert result.returncode == 1
    assert f"{script}:2:" in result.stderr
    assert "readlink" in result.stderr
    assert "allow-rename-onto-symlink" in result.stderr


def test_main_exits_zero_on_a_clean_file(tmp_path: Path) -> None:
    script = tmp_path / "seed.sh"
    script.write_text('#!/bin/bash\nmv "$cfg.tmp" "$dest"\n', encoding="utf-8")
    assert _run(script).returncode == 0


def test_a_file_the_grammar_refuses_fails_loudly(tmp_path: Path) -> None:
    """A pathological input is reported, never skipped: a silent skip would
    false-green exactly the file an adversary controls."""
    script = tmp_path / "huge.sh"
    script.write_text("cmd |" * 3000, encoding="utf-8")
    result = _run(script)
    assert result.returncode == 1
    assert "pipe bytes" in result.stderr
