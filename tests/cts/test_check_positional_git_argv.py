"""Tests for ci_truth_serum/check_positional_git_argv.py — the guard against a
test that models git's argv by fixed position, which a variable-length global-
option prefix silently defeats.

Ported from agent-glovebox's positional-git-argv.py lint half (the
``parse_git_argv``/``git_calls``/stub-script production helpers it also
tested belong to the consumer, not this pack). Drives ``violations()`` for the
parsing rules and ``main()`` for the argv/exit-code contract and the
``--subcommand`` extension flag.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, load_hook

_REL = "check_positional_git_argv.py"
_SRC = HOOKS_DIR / _REL
mod = load_hook(_REL, "check_positional_git_argv")


def _scan(fragment: str) -> list[int]:
    """FRAGMENT's violating line numbers, scanned the way the lint scans a
    real file.

    The lint reads a Python code view, so its input must be a whole module. A
    real test file opens with its own docstring, and prepending one here is
    what keeps a fragment's leading string literal a VALUE — at module top a
    lone string literal is the module docstring.
    """
    return [
        n - 1 for n in mod.violations('"""Module."""\n' + fragment, "tests/test_x.py")
    ]


@pytest.mark.parametrize(
    "text",
    [
        'assert any(ln.startswith("git fetch --no-tags") for ln in routed)',
        "assert routed[0].startswith('git rev-parse')",
        'assert line == "git ls-remote origin"',
        'assert line != "git status --short"',
        'assert first == f"git fetch {remote}"',
        "'if [ \"$1\" = ls-remote ]; then exec sleep 600; fi\\n'",
        '\'if [[ "$1" == "rev-parse" && "$2" == "--show-toplevel" ]]; then\\n\'',
        "'[ \"$2\" = for-each-ref ] && exit 1\\n'",
        "'case \"$1\" in\\n'\n'  rev-parse) echo /r ;;\\n'\n'esac\\n'",
        "'case \"$1\" in\\n'\nf'  merge-base|cat-file) exit 1 ;;\\n'\n'esac\\n'",
    ],
)
def test_lint_fires_on_positional_argv_modelling(text: str) -> None:
    assert _scan(text) != [], text


@pytest.mark.parametrize(
    "text",
    [
        'fetch, = git_calls(routed, "fetch")',
        '\'if [[ "$gb_cmd" == "rev-parse --show-toplevel" ]]; then\\n\'',
        "'if [ \"$gb_sub\" = ls-remote ]; then exec sleep 600; fi\\n'",
        # Position-independent stub matching was never the defect.
        '\'case " $* " in *" log --first-parent "*) exit 1 ;; esac\\n\'',
        # An unanchored substring check on user-facing advice text.
        'assert "git merge glovebox/x" in r.stderr',
        # A python-level program name, correctly at argv[0].
        'if argv[0] == "git":\n    pass',
        # An ssh URL, not a command line.
        'assert upstream == "git@github.com:u/s.git"',
        # Fixture list items feeding a source-text linter, not recorded argv.
        'x = [\n    "git fetch origin",\n]',
        # A global-option check is not a subcommand index.
        'assert line.startswith("git --no-pager")',
        # Stubs for other CLIs keep their $1 dispatch.
        "'case \"$1\" in\\n'\n'  pull) exit 0 ;;\\n'\n'  login) exit 0 ;;\\n'\n'esac\\n'",
        "'if [ \"$1\" = login ]; then cat >/dev/null; fi\\n'",
        # A closed case block does not extend over a later git-only word.
        "'case \"$1\" in\\n'\n'  pull) exit 0 ;;\\n'\n'esac\\n'\n'rev-parse) :\\n'",
        # …including when the whole block is written on one line.
        "'case \"$1\" in build) exit 0 ;; esac\\n'\n'  worktree) :\\n'",
    ],
)
def test_lint_ignores_legitimate_forms(text: str) -> None:
    assert _scan(text) == [], text


def test_a_comment_naming_the_banned_form_is_not_a_hit() -> None:
    """Prose describing the rule is not an instance of it. A lint that fires
    on its own explanation trains the next session to annotate the comment
    away."""
    assert _scan('# never write `[ "$1" = ls-remote ]` in a stub\n') == []


def test_a_case_window_is_closed_only_by_a_real_esac() -> None:
    """The window closes on the `esac` KEYWORD in code. The word inside a
    comment closes nothing, and neither does a longer identifier containing
    it — either one hides every arm below, which is the silent direction: an
    unflagged `$1`-keyed stub stops intercepting and its test asserts
    nothing."""
    in_a_comment = (
        "stub = (\n    'case \"$1\" in\\n'\n"
        "    # the esac below ends this block\n"
        "    '  rev-parse) echo /r ;;\\n'\n    'esac\\n'\n)\n"
    )
    assert _scan(in_a_comment) == [4]
    inside_a_longer_word = (
        "stub = (\n    'case \"$1\" in\\n'\n"
        "    'esacapade\\n'\n    '  rev-parse) echo /r ;;\\n'\n    'esac\\n'\n)\n"
    )
    assert _scan(inside_a_longer_word) == [4]


def test_lint_honours_a_same_line_annotation() -> None:
    line = (
        "'if [ \"$1\" = ls-remote ]; then exit 1; fi\\n'  "
        "# allow-positional-git-argv: bare-git CI script"
    )
    assert _scan(line) == []


def test_lint_honours_an_annotation_on_the_preceding_line() -> None:
    text = (
        "# allow-positional-git-argv: bare-git CI script\n"
        "'if [ \"$1\" = ls-remote ]; then exit 1; fi\\n'\n"
    )
    assert _scan(text) == []


def test_lint_rejects_an_annotation_with_no_reason() -> None:
    """An unexplained exemption is indistinguishable from a forgotten call site."""
    line = (
        "'if [ \"$1\" = ls-remote ]; then exit 1; fi\\n'  # allow-positional-git-argv:"
    )
    assert _scan(line) == [1]


def test_the_annotation_exempts_only_from_a_comment() -> None:
    """The exemption is the one path that turns this gate green, so it is
    read from the comment view. The same text inside a string literal — which
    this repo's own lint fixtures contain — must exempt nothing."""
    text = (
        'marker = "allow-positional-git-argv: fixture"\n'
        "'if [ \"$1\" = ls-remote ]; then exit 1; fi\\n'\n"
    )
    assert _scan(text) == [2]


def test_lint_ignores_an_annotation_two_lines_above() -> None:
    text = (
        "# allow-positional-git-argv: for the call below, not this one\n"
        "unrelated = 1\n"
        "'if [ \"$1\" = ls-remote ]; then exit 1; fi\\n'\n"
    )
    assert _scan(text) == [3]


def test_an_unparseable_fragment_falls_back_to_a_text_scan_not_a_crash() -> None:
    """The stdlib tokenizer call inside the original hand-rolled comment pass
    raised straight through a syntax error. ``_comments`` falls back to a text
    scan instead, so a half-written test file still gets scanned rather than
    crashing the whole run."""
    broken = 'def broken(:\nassert routed[0].startswith("git rev-parse")\n'
    assert mod.violations(broken, "tests/test_broken.py") != []


# ── non-vacuity: the lint fires on a real hit and stays clean on a real fix ──


def test_non_vacuous_hit_and_fix() -> None:
    hit = 'assert routed[0].startswith("git rev-parse")\n'
    fixed = 'fetch, = git_calls(routed, "fetch")\n'
    assert mod.violations(hit, "tests/test_x.py") != []
    assert mod.violations(fixed, "tests/test_x.py") == []


# ── --subcommand extension ──────────────────────────────────────────────────


def test_subcommand_flag_extends_the_git_only_set() -> None:
    src = "'case \"$1\" in\\n'\n'  house-verb) echo hi ;;\\n'\n'esac\\n'"
    assert _scan(src) == []
    extended = mod._GIT_ONLY_SUBCOMMANDS | frozenset({"house-verb"})
    assert [
        n - 1
        for n in mod.violations('"""Module."""\n' + src, "tests/test_x.py", extended)
    ] != []


def test_the_default_subcommand_set_is_never_narrowed_by_extension() -> None:
    extended = mod._GIT_ONLY_SUBCOMMANDS | frozenset({"house-verb"})
    assert "rev-parse" in extended


# ── main: file-class scoping, argv/exit-code contract, the flag ────────────


def test_main_only_scans_test_paths(tmp_path, capsys) -> None:
    prod = tmp_path / "lib.py"
    prod.write_text('assert routed[0].startswith("git rev-parse")\n')
    assert mod.main([str(prod)]) == 0
    assert capsys.readouterr().err == ""


def test_main_scans_a_real_test_path(tmp_path, capsys) -> None:
    test_file = tmp_path / "tests" / "test_thing.py"
    test_file.parent.mkdir()
    test_file.write_text('assert routed[0].startswith("git rev-parse")\n')
    assert mod.main([str(test_file)]) == 1
    assert f"{test_file}:1:" in capsys.readouterr().err


def test_main_clean_file_exits_0(tmp_path) -> None:
    test_file = tmp_path / "tests" / "test_thing.py"
    test_file.parent.mkdir()
    test_file.write_text('fetch, = git_calls(routed, "fetch")\n')
    assert mod.main([str(test_file)]) == 0


def test_main_with_no_files_refuses_and_exits_2(capsys) -> None:
    assert mod.main([]) == 2
    assert "no files to scan" in capsys.readouterr().err


def test_main_flags_only_with_no_files_also_refuses(capsys) -> None:
    assert mod.main(["--subcommand", "house-verb"]) == 2
    assert "no files to scan" in capsys.readouterr().err


def test_main_subcommand_flag_reaches_the_scan(tmp_path, capsys) -> None:
    test_file = tmp_path / "tests" / "test_thing.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "'case \"$1\" in\\n'\n'  house-verb) echo hi ;;\\n'\n'esac\\n'\n"
    )
    assert mod.main([str(test_file)]) == 0
    assert mod.main(["--subcommand", "house-verb", str(test_file)]) == 1


def test_main_reports_a_gone_file_as_no_hits(tmp_path) -> None:
    test_file = tmp_path / "tests" / "test_gone.py"
    test_file.parent.mkdir()
    assert mod.main([str(test_file)]) == 0


def test_lint_script_reports_the_hit_and_exits_nonzero(tmp_path: Path) -> None:
    """End to end through the real entry point: the message names the file,
    the line, and the fix."""
    bad = tmp_path / "tests" / "test_bad.py"
    bad.parent.mkdir()
    bad.write_text(
        'assert any(ln.startswith("git fetch --no-tags") for ln in routed)\n',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(_SRC), str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert f"{bad}:1:" in proc.stderr
    assert "Locate the subcommand" in proc.stderr


def test_module_run_with_truly_empty_argv_exits_2() -> None:
    proc = subprocess.run(
        [sys.executable, str(_SRC)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 2
    assert "no files to scan" in proc.stderr
