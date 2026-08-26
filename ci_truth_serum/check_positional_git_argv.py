#!/usr/bin/env python3
"""Ban a test that models git's argv POSITIONALLY.

A wrapper that inserts a VARIABLE number of global options between ``git`` and
its subcommand (``--no-pager``, a config override per key) shifts the
subcommand off any fixed argv index. A test that asserts on ``argv[0]`` — or a
shell git stub keyed on ``"$1"`` — breaks the moment that prefix grows: an
anchored assertion goes red (visible), but a ``"$1"``-keyed stub goes SILENT —
it stops intercepting, and the test that depends on it passes while asserting
nothing.

Two shapes are flagged in a Python test file:

* a Python comparison or ``str.startswith`` call anchoring a recorded command
  line at ``git <subcommand>`` (``line == "git rev-parse HEAD"``,
  ``ln.startswith("git fetch")``);
* a shell fragment — built as a Python string literal — that tests a
  positional parameter against a git-only subcommand name (``[ "$1" =
  ls-remote ]``, a ``case "$1" in rev-parse) …`` arm).

Locate the subcommand instead of indexing to it. Fix by searching the
recorded argv for the subcommand token rather than a fixed index, or by
keying a stub on the subcommand it reads out of its own argv, not on ``$1``.

Comments are read through ``_comments`` (Python's own tokenizer under it), so
a mention of the banned form in a docstring or a ``#`` note is prose, never an
instance of it. Locating where a flagged shell fragment's text starts on its
line uses Python's tokenizer too (the STRING token's own start column),
replacing a whole-line quote-guessing regex with the boundary the grammar
already knows. The fragments this check reads are Python string literals
representing shell text, not a complete script — there is nothing here for a
bash grammar to parse (a fixture builds a stub one line, one string literal,
at a time), so this stays a line-oriented scan of the DECODED literal, never a
tree.

Only files a test path names are scanned (``tests/``, ``test_*.py``,
``*_test.py`` — see ``_linecheck.is_test_path``): the defect is a test
modelling host git's argv, and production code never indexes its own argv
this way.

Known gaps: the shell-fragment half only flags subcommand names unique to
git (``rev-parse``, ``ls-remote``, …), so a stub keyed on a name other CLIs
share (``fetch``, ``clone``, ``log``) passes; and an f-string's interpolated
``{…}`` segment is not inspected, since its value is unknown at lint time.

Opt a call site out with ``# allow-positional-git-argv: <reason>`` on its own
line or the line above. ``--subcommand NAME`` (repeatable) extends the
git-only subcommand set this check recognizes.

Invoked by pre-commit with the staged Python test files as arguments.
"""

import argparse
import io
import re
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _comments import comment_lines  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    is_test_path,
    run_file_cli,
)

OPT_OUT = "allow-positional-git-argv"

# Subcommand names unique to git, so a `"$1"` match on one is a git stub for sure.
_GIT_ONLY_SUBCOMMANDS = frozenset(
    {
        "rev-parse",
        "rev-list",
        "ls-remote",
        "ls-files",
        "ls-tree",
        "for-each-ref",
        "symbolic-ref",
        "show-ref",
        "update-ref",
        "merge-base",
        "hash-object",
        "write-tree",
        "commit-tree",
        "cat-file",
        "check-ignore",
        "diff-index",
        "diff-tree",
        "diff-files",
        "format-patch",
        "range-diff",
        "whatchanged",
        "cherry-pick",
        "reflog",
        "worktree",
    }
)


def _subcommand_alt(subcommands: frozenset[str]) -> str:
    return "|".join(re.escape(s) for s in sorted(subcommands))


# `ln.startswith("git fetch …")` / `line == "git rev-parse …"`. `(?!-)` keeps
# `startswith("git --no-pager")`-style global-option checks out of scope.
_ANCHORED_COMMAND_LINE = re.compile(
    r"""(?:\.startswith\(|[=!]=\s*)\s*(?:[a-zA-Z]{0,2})?(?:['"])git\s+(?!-)"""
)


def _positional_test_re(subcommands: frozenset[str]) -> "re.Pattern[str]":
    """`[ "$1" = ls-remote ]`, `[[ "$2" == "rev-parse" ]]`, `case "$1" in
    rev-parse)`."""
    return re.compile(
        rf""""\$[1-9]"\s*(?:==?|in)\s*['"]?(?:{_subcommand_alt(subcommands)})\b"""
    )


def _case_arm_re(subcommands: frozenset[str]) -> "re.Pattern[str]":
    return re.compile(
        rf"""^\s*(?:{_subcommand_alt(subcommands)})(?:\s*\|[\w|\s-]*)?\)"""
    )


# A `case "$1" in` whose arms sit on later lines, closed by `esac`.
_CASE_ON_POSITIONAL = re.compile(r"""case\s+"\$[1-9]"\s+in""")
# The WORD `esac`, so a longer identifier that merely contains it does not
# close the window early and hide the arms below.
_CASE_END = re.compile(r"\besac\b")

# The prefix + opening quote(s) of a Python string literal, once the
# tokenizer has already said exactly where the token starts on its line — so
# this only has to name how many characters the literal's own decoration
# takes, never guess whether a quote earlier on the line belongs to it.
_QUOTE_OPENER = re.compile(r"^[a-zA-Z]{0,2}(?:'''|\"\"\"|'|\")")


def _string_starts(text: str) -> dict[int, int]:
    """1-based physical line -> the column where a STRING token BEGINS on
    that line, from Python's own tokenizer — one entry per token's first
    line, so a line that opens no string literal of its own has no entry.

    Empty on source the tokenizer cannot read: the caller then treats every
    line as plain text, which is the same fallback the shared comment reader
    takes for the same reason.
    """
    starts: dict[int, int] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.STRING:
                starts.setdefault(tok.start[0], tok.start[1])
    except (tokenize.TokenError, SyntaxError):
        return {}
    return starts


def _shell_text(line: str, start_col: int | None) -> str:
    """The shell-fragment content of LINE, with its Python string literal's
    own prefix and opening quote stripped — or LINE itself, stripped of
    leading whitespace, when no string token starts on it."""
    if start_col is None:
        return line.strip()
    opener = _QUOTE_OPENER.match(line[start_col:])
    content_start = start_col + (opener.end() if opener else 0)
    return line[content_start:].rstrip()


def _code_view(physical: list[str], comments: dict[int, str]) -> list[str]:
    """PHYSICAL with each line's trailing comment blanked, so a line that
    carries CODE and a trailing comment is still scanned for a violation in
    its code — a python comment always runs to end of line, so the comment
    text is exactly that line's own tail."""
    return [
        line[: len(line) - len(comments[i])] if i in comments else line
        for i, line in enumerate(physical, 1)
    ]


def violations(
    text: str,
    path: str = "test.py",
    subcommands: frozenset[str] = _GIT_ONLY_SUBCOMMANDS,
) -> list[int]:
    """1-based line numbers that index into git's argv positionally, unannotated."""
    comments = comment_lines(text, path)
    physical = text.splitlines()
    code = _code_view(physical, comments)
    starts = _string_starts(text)
    positional_test = _positional_test_re(subcommands)
    case_arm = _case_arm_re(subcommands)
    hits: list[int] = []
    in_case = False
    for lineno, line in enumerate(code, 1):
        if _CASE_ON_POSITIONAL.search(line):
            in_case = True
        # A whole `case … esac` on one line leaves no window open below it.
        if _CASE_END.search(line):
            in_case = False
        if annotated_near(physical, lineno, OPT_OUT):
            continue
        if _ANCHORED_COMMAND_LINE.search(line) or positional_test.search(line):
            hits.append(lineno)
            continue
        if in_case and case_arm.match(_shell_text(line, starts.get(lineno))):
            hits.append(lineno)
    return hits


MESSAGE = (
    "this models git's argv positionally, but a wrapper can insert a variable "
    "number of global options before the subcommand — an assertion anchored "
    "this way goes red and a `$1`-keyed git stub goes SILENTLY vacuous. Locate "
    "the subcommand instead of indexing to it, or annotate "
    f"`# {OPT_OUT}: <reason>`."
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subcommand",
        action="append",
        default=[],
        metavar="NAME",
        dest="extra_subcommands",
        help="an additional git-only subcommand name (repeatable); extends "
        "the built-in set rather than replacing it",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if not args.files:
        print(
            "check_positional_git_argv: no files to scan. This check reads "
            "only the paths you give it, so an empty run would report a "
            "clean pass over nothing.",
            file=sys.stderr,
        )
        return 2
    subcommands = _GIT_ONLY_SUBCOMMANDS | frozenset(args.extra_subcommands)
    status = 0
    for path in args.files:
        if not is_test_path(path):
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        for lineno in violations(text, path, subcommands):
            print(f"{path}:{lineno}: {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
