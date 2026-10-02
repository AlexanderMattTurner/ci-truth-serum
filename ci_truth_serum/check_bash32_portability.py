#!/usr/bin/env python3
"""Ban GNU-only flags and bash-4+ syntax in scripts that must run on macOS.

macOS ships BSD userland and a frozen `/bin/bash` 3.2. A Linux CI runner has GNU
tools and bash 5, so `grep -P` or `${v,,}` passes CI and then aborts on a Mac.

Two rule sets, each a node shape in the bash grammar (``_cts_bash_ast``):
  * bsd: a GNU-only flag that BSD `tail`/`head`/`grep`/`find`/`date`/`sort`/
    `sha256sum` rejects. A row exists only where BSD aborts, not where it ignores.
  * bash32: bash-4+ syntax — `declare -A`/`-n`, `mapfile`, `${v,,}`, `exec {fd}<`
    and `${a[-1]}`. A file that reads `BASH_VERSINFO` checks its own bash version,
    so this set skips that file.

The check judges every file it gets, so the CONSUMER names its macOS scripts with
the hook's `files:`. A script that runs only on Linux belongs in `exclude:`. Pass
`--only bsd` or `--only bash32` to run one set, so two hook entries can scope the
sets to different files.

Text that no shell runs is not a finding: a quoted string, a comment, and a quoted
heredoc body hold no commands. Opt out with `# bash32-portability-ok: <reason>`
on the line or the comment block above it. The reason is REQUIRED.
"""

import argparse
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_words,
    iter_nodes,
    node_text,
    parse,
    program_name,
    unquote,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_message_checks,
)

OPT_OUT = "bash32-portability-ok"
BSD = "bsd"
BASH32 = "bash32"
RULE_SETS = (BSD, BASH32)

_SUFFIX = (
    f" Annotate `# {OPT_OUT}: <reason>` when this line never runs on macOS, or "
    "exclude the file from the hook's `files:`."
)


class _Hit(NamedTuple):
    """One finding: where its construct starts and ends, and what to say."""

    line: int
    end_line: int
    message: str


class _Invocation(NamedTuple):
    """One `command` node, the program it runs, and that program's operands."""

    command: Node
    run: str
    operands: list[str]


def _short_flag(word: str, letter: str) -> bool:
    """True when WORD is a short-flag cluster carrying LETTER: `-z`, `-zn`, `-rV`."""
    return word.startswith("-") and not word.startswith("--") and letter in word[1:]


def _options(words: Sequence[str], value_letters: str = "") -> list[str]:
    """The words of WORDS that are options: none after `--`, and none that are
    the separate VALUE of an option whose letter is in VALUE_LETTERS.

    `grep -- -P f` and `grep -e -P f` search for the text `-P`, so `-P` is data.
    """
    options: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word == "--":
            break
        else:
            options.append(word)
            skip = bool(value_letters) and (
                word.startswith("-")
                and not word.startswith("--")
                and word[-1] in value_letters
                or word in _LONG_VALUE_OPTIONS
            )
    return options


# Long options that take their value as the next word, for the rows that read it.
_LONG_VALUE_OPTIONS = frozenset({"--regexp", "--file"})


def _flag(
    letter: str, long_form: str, value_letters: str = ""
) -> Callable[[Sequence[str]], bool]:
    """A predicate: an option is a short cluster with LETTER, or is LONG_FORM.

    An option after `--`, or the value of one in VALUE_LETTERS, is data.
    """
    return lambda words: any(
        _short_flag(word, letter) or word == long_form
        for word in _options(words, value_letters)
    )


def _operand(*wanted: str) -> Callable[[Sequence[str]], bool]:
    """A predicate: an operand is one of WANTED, alone or with its value attached.

    `date -d@0` and `date --date=@0` run the same GNU flag as `date -d @0`.
    """

    def attached(word: str) -> bool:
        return any(
            word.startswith(f"{flag}=")
            if flag.startswith("--")
            else word.startswith(flag)
            for flag in wanted
            if flag.startswith("--") or len(flag) == 2
        )

    return lambda words: any(
        word in wanted or attached(word) for word in _options(words)
    )


def _checks_stdin(words: Sequence[str]) -> bool:
    """True when `sha256sum` checks a list it reads from a pipe or a here-string.

    Apple's `sha256sum` reads a checksum list only from a FILE operand or `-`.
    """
    reads_a_file = any(word == "-" or not word.startswith("-") for word in words)
    return _flag("c", "--check")(words) and not reads_a_file


class _GnuRow(NamedTuple):
    """One GNU-only construct: its label, the programs, the operand test, the fix."""

    label: str
    programs: frozenset[str]
    flagged: Callable[[Sequence[str]], bool]
    fix: str


# The short options of tail, head and sort take numeric values or none, so a `z`
# or a `V` anywhere in a cluster is the GNU flag. `cut -dz` takes a letter as data,
# which is why cut is not a row.
GNU_ROWS = (
    _GnuRow(
        "`tail`/`head` -z (--zero-terminated)",
        frozenset({"tail", "head"}),
        _flag("z", "--zero-terminated"),
        "BSD tail and head have no -z. Use newline-delimited names, or a "
        "`while IFS= read -r -d ''` loop.",
    ),
    _GnuRow(
        "`grep -P` (--perl-regexp)",
        frozenset({"grep"}),
        _flag("P", "--perl-regexp", "ef"),
        "BSD grep has no PCRE. Rewrite the pattern as a POSIX ERE for `grep -E`.",
    ),
    _GnuRow(
        "`find -printf`",
        frozenset({"find"}),
        _operand("-printf"),
        "BSD find has no -printf. Use `-print0` with `xargs -0`, or `-exec`.",
    ),
    _GnuRow(
        "`date -d` (--date)",
        frozenset({"date"}),
        _operand("-d", "--date"),
        "BSD date has no -d. It parses a date with `-j -f <format>` and does "
        "arithmetic with `-v`.",
    ),
    _GnuRow(
        "`sort -V` (--version-sort)",
        frozenset({"sort"}),
        _flag("V", "--version-sort"),
        "BSD sort has no -V, so a version gate reads every version as older. "
        "Compare the dotted fields in shell instead.",
    ),
    _GnuRow(
        "`sha256sum -c` with the list on stdin",
        frozenset({"sha256sum"}),
        _checks_stdin,
        "Apple's sha256sum reads a checksum list only from a file operand, so this "
        "answers a usage error on a good file. Compare the digest in shell, or pass "
        "`-` to name stdin.",
    ),
)

# The options of `env` that take a value in the next word.
_ENV_VALUE_FLAGS = frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"})


def _unwrapped(name: str, words: list[str]) -> tuple[str, list[str]]:
    """The program NAME really runs, past a `command` or `env` prefix that forwards.

    `command -v grep` and `env --help` print and run nothing, so each keeps its own
    name and no row reads it.
    """
    if name == "command":
        rest = list(words)
        while rest and rest[0].startswith("-"):
            if "v" in rest[0][1:].lower():
                return name, words
            rest.pop(0)
        return (program_name(rest[0]), rest[1:]) if rest else (name, words)
    if name != "env" or "--help" in words or "--version" in words:
        return name, words
    rest = list(words)
    while rest:
        word = rest[0]
        if word in _ENV_VALUE_FLAGS:
            rest = rest[2:]
        elif word.startswith("-") or "=" in word:
            rest.pop(0)
        else:
            break
    return (program_name(rest[0]), rest[1:]) if rest else (name, words)


def _span(node: Node) -> tuple[int, int]:
    """NODE's first and last 1-based line."""
    return node.start_point[0] + 1, node.end_point[0] + 1


def _invocations(root: Node) -> list[_Invocation]:
    """Every `command` under ROOT, with the program it runs and that program's
    operands. Quotes are removed, so `'grep' '-P'` reads as `grep -P`."""
    found = []
    for command in iter_nodes(root, "command"):
        words = command_words(command)
        if not words:
            continue
        run, operands = _unwrapped(
            program_name(words[0]), [unquote(word) for word in words[1:]]
        )
        found.append(_Invocation(command, run, operands))
    return found


def _gnu_hits(root: Node) -> list[_Hit]:
    """Every invocation of a GNU-only construct under ROOT.

    The grammar names the command, so `docker exec box tail -z` is a `docker`
    call and no `tail` row sees it.
    """
    hits = []
    for command, run, operands in _invocations(root):
        for row in GNU_ROWS:
            if run in row.programs and row.flagged(operands):
                hits.append(
                    _Hit(
                        *_span(command),
                        f"{row.label} is GNU-only, so it aborts on macOS. {row.fix}"
                        + _SUFFIX,
                    )
                )
    return hits


_DECLARE_BUILTINS = frozenset({"declare", "local", "typeset"})


def _declaration_flags(root: Node) -> list[tuple[Node, list[str]]]:
    """Every `declare`/`local`/`typeset` under ROOT, with its option words.

    The grammar reads a negated `! declare -A m` as a plain command, so both
    shapes are read here.
    """
    found = []
    for node in iter_nodes(root, "declaration_command", "command"):
        words = (
            [node_text(child) for child in node.children]
            if node.type == "declaration_command"
            else command_words(node)
        )
        if words and words[0] in _DECLARE_BUILTINS:
            found.append((node, [unquote(word) for word in words[1:]]))
    return found


_CASE_OPERATORS = frozenset({",", ",,", "^", "^^"})


def _operators(expansion: Node) -> list[str]:
    """The operator tokens of a `${…}` EXPANSION node."""
    return [
        node_text(child)
        for index, child in enumerate(expansion.children)
        if expansion.field_name_for_child(index) == "operator"
    ]


def _negative_index(expansion: Node) -> bool:
    """True when EXPANSION reads an array element at a negative index.

    The grammar bounds the index, so `${a[i-1]}` and `${a[@]:-1}` stay out.
    """
    return any(
        child.type == "subscript"
        and (index := child.child_by_field_name("index")) is not None
        and node_text(index).strip().startswith("-")
        for child in expansion.children
    )


_VARNAME_FD = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _allocates_fd(redirect: Node) -> bool:
    """True when REDIRECT names its descriptor as `{var}`, which bash 4.1 added.

    The grammar parses `{fd}` as a word that ends where the redirect starts. A
    space between them makes it a plain argument, so adjacency decides.
    """
    tail = redirect.prev_sibling
    while tail is not None and tail.end_byte == redirect.start_byte:
        # The grammar gives `{fd}` no node of its own, so its leaf text decides.
        if _VARNAME_FD.fullmatch(node_text(tail)):
            return True
        tail = tail.children[-1] if tail.children else None
    return False


def _bash4_hit(node: Node, label: str, fix: str) -> _Hit:
    return _Hit(
        *_span(node),
        f"{label} needs bash 4 or later, so it aborts under macOS's /bin/bash 3.2. "
        f"{fix}" + _SUFFIX,
    )


_GUARD_FIX = "Or make the script check BASH_VERSINFO and re-run under a newer bash."


def _bash4_hits(root: Node) -> list[_Hit]:
    """Every bash-4+ construct under ROOT."""
    hits = []
    for node, flags in _declaration_flags(root):
        if any(_short_flag(flag, "A") for flag in flags):
            hits.append(
                _bash4_hit(node, "`declare -A`", f"Use an indexed array. {_GUARD_FIX}")
            )
        if any(_short_flag(flag, "n") for flag in flags):
            hits.append(
                _bash4_hit(
                    node, "`declare -n`", f"Pass the variable's name. {_GUARD_FIX}"
                )
            )
    for command, run, _operands in _invocations(root):
        if run in {"mapfile", "readarray"}:
            hits.append(
                _bash4_hit(
                    command,
                    f"`{run}`",
                    f"Use a `while IFS= read -r` loop. {_GUARD_FIX}",
                )
            )
    for expansion in iter_nodes(root, "expansion"):
        if _CASE_OPERATORS & set(_operators(expansion)):
            hits.append(
                _bash4_hit(
                    expansion,
                    "`${v,,}`/`${v^^}` case conversion",
                    f"Use `tr` or `awk`. {_GUARD_FIX}",
                )
            )
        if _negative_index(expansion):
            hits.append(
                _bash4_hit(
                    expansion,
                    "`${a[-1]}` negative index",
                    "Index from `${#a[@]}` instead.",
                )
            )
    for redirect in iter_nodes(root, "file_redirect"):
        if _allocates_fd(redirect):
            hits.append(
                _bash4_hit(
                    redirect,
                    "`{fd}` descriptor allocation",
                    f"Use a fixed descriptor number. {_GUARD_FIX}",
                )
            )
    return hits


def checks_bash_version(root: Node) -> bool:
    """True when the script reads `BASH_VERSINFO`, as code and not as text.

    A comment, a message string, or a single-quoted probe of another bash holds
    no `variable_name` node, so none of them exempts the file.
    """
    return any(
        node_text(node) == "BASH_VERSINFO" for node in iter_nodes(root, "variable_name")
    )


def violations(text: str, rules: Sequence[str] = RULE_SETS) -> list[tuple[int, str]]:
    """(1-based line, message) for each non-portable construct in TEXT.

    RULES names the sets to apply. The bash32 set skips a file that reads
    `BASH_VERSINFO`, because that file handles its own version.
    """
    root = parse(text)
    hits = _gnu_hits(root) if BSD in rules else []
    if BASH32 in rules and not checks_bash_version(root):
        hits += _bash4_hits(root)
    lines = text.split("\n")
    return sorted(
        {
            (hit.line, hit.message)
            for hit in hits
            if not annotated_near(lines, hit.line, OPT_OUT, span_end=hit.end_line)
        }
    )


def main(argv: list[str]) -> int:
    """Check each path in ARGV. A file the grammar refuses fails loudly by name."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=RULE_SETS,
        help="apply one rule set: `bsd` (GNU-only flags) or `bash32` (bash-4+ "
        "syntax). Both apply by default.",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if not args.files:
        print(
            "check_bash32_portability: no files to scan. This check reads only "
            "the paths you give it, so an empty run would report a clean pass "
            "over nothing.",
            file=sys.stderr,
        )
        return 2
    rules = (args.only,) if args.only else RULE_SETS
    status = 0
    for path in args.files:
        try:
            status = max(
                status,
                run_message_checks([path], lambda text, _path: violations(text, rules)),
            )
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
