#!/usr/bin/env python3
"""Fail when a Python module builds a ``git`` command line that names no
repository — the argv then acts on whatever directory the process is in.

Real incident: a helper ran ``subprocess.run(["git", *args])`` and one caller
passed ``["merge", "--abort"]``. A test driving that helper IN-PROCESS (no
``cwd=`` set) aborted the developer's own merge — staged work gone, nothing
printed that reads as damage.

The rule: a git call names its repository, either as the argv's FIRST option
(``["git", "-C", repo, ...]``) or as the call's own ``cwd=``, unless every
subcommand it can run only reads. ``["git", "merge", "--abort"]`` is flagged,
and so is ``["git", *args]``, whose subcommand can be anything — an
unclassified or unresolvable subcommand is treated as able to write, so a new
git verb, or an argv this check cannot read, fails closed.

The argv is found through Python's own grammar (``ast``), so a ``git`` string
that is data (a docstring, a log message) is never mistaken for a call, and a
call spelled with the ``args=`` keyword is read the same as a positional one.

Opt out with ``# cwd-git-ok: <reason>`` on the call's line or the line above.
``--read-only-subcommand NAME`` (repeatable) extends the built-in read-only
verb set with one this repository trusts.

Known gap: the argv must be a literal AT the call, so ``cmd = [...]`` then
``subprocess.run(cmd)`` needs dataflow this check does not do, and is not seen.

Invoked by pre-commit with the staged Python files as arguments.
"""

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    run_line_checks,
)

OPT_OUT = "cwd-git-ok"

# Subcommands that only READ. Anything outside this set — and anything this
# check cannot resolve to a literal — must name its repository. The list is
# deliberately a floor: a subcommand nobody has classified is treated as able
# to write, so a new git verb fails closed. ``--read-only-subcommand`` extends
# it; it never replaces it.
READ_ONLY = frozenset(
    {
        "cat-file",
        "check-attr",
        "check-ignore",
        "config",
        "diff",
        "diff-tree",
        "for-each-ref",
        "grep",
        "hash-object",
        "log",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-list",
        "rev-parse",
        "show",
        "show-ref",
        "status",
        "symbolic-ref",
        "var",
        "version",
    }
)

# Git's own global options that take their value as the NEXT argv element.
# Reading that value as the subcommand is what would make
# `git -c http.sslCAInfo=<f-string> ls-remote <url>` unresolvable, and so
# flagged, though every subcommand it can run only reads.
_VALUE_TAKING_GLOBALS = frozenset({"-c", "--config-env"})

# Stands for a subcommand slot this check cannot read — a splat, a variable, an
# f-string. It is not a git verb, so it matches no name below, which is how an
# unresolvable subcommand fails closed. Kept apart from the ``None`` an argv
# with NO subcommand returns: ``["git"]`` and ``["git", "--bare"]`` run nothing
# and touch no repository.
_UNRESOLVED = "\0unresolved"


def _argv_node(node: ast.Call) -> ast.expr | None:
    """The first positional argument of a call, however it was spelled.

    ``subprocess.run(args=["git", …])`` passes the argv as a keyword, and
    reading only ``node.args`` would let that spelling through unchecked.
    """
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg == "args":
            return keyword.value
    return None


def _git_argv(node: ast.Call) -> ast.List | ast.Tuple | None:
    """The argv sequence literal of a subprocess call whose program is ``git``."""
    argv = _argv_node(node)
    if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
        return None
    first = argv.elts[0]
    if isinstance(first, ast.Constant) and first.value == "git":
        return argv
    return None


def _names_a_repo(node: ast.Call, argv: ast.List | ast.Tuple) -> bool:
    """Whether the call names the repository it acts on.

    Either as the argv's first option — a later ``-C`` sits after a
    subcommand that has already chosen its repository — or as the subprocess
    call's own ``cwd=``, which sets the child's working directory outright.
    """
    if any(keyword.arg == "cwd" for keyword in node.keywords):
        return True
    if len(argv.elts) < 2:
        return False
    second = argv.elts[1]
    return isinstance(second, ast.Constant) and second.value == "-C"


def _subcommand(argv: ast.List | ast.Tuple) -> str | None:
    """The git subcommand this argv runs, :data:`_UNRESOLVED`, or ``None`` for
    none at all.

    An ``--option=value`` carries its own value, so only the separated
    spelling skips the element after it.
    """
    skip = False
    for element in argv.elts[1:]:
        if skip:
            skip = False
            continue
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return _UNRESOLVED
        word = element.value
        if word in _VALUE_TAKING_GLOBALS:
            skip = True
            continue
        if word.startswith("-"):
            continue
        return word
    return None


def _creates_its_own_repo(argv: ast.List | ast.Tuple) -> bool:
    """Whether this argv CREATES a repository somewhere other than the process
    directory, which is what makes naming one unnecessary.

    ``git clone`` always writes into a new directory and refuses a
    destination that already holds files, so it can never act on the ambient
    repository. ``git init`` can, unless the argv ends in a path the code
    BUILT — a trailing string LITERAL is not enough, because
    ``git init -b main`` ends in a branch name and initializes the process
    directory.
    """
    subcommand = _subcommand(argv)
    if subcommand == "clone":
        return True
    if subcommand == "init":
        return not isinstance(argv.elts[-1], ast.Constant)
    return False


def _writes(argv: ast.List | ast.Tuple, read_only: frozenset[str]) -> bool:
    """Whether this argv needs a repository, i.e. can run something that writes."""
    subcommand = _subcommand(argv)
    return subcommand is not None and subcommand not in read_only


def _suppressed(node: ast.Call, physical: list[str]) -> bool:
    end = getattr(node, "end_lineno", node.lineno)
    return annotated_near(physical, node.lineno, OPT_OUT, span_end=end)


def violations(text: str, read_only: frozenset[str] = READ_ONLY) -> list[int]:
    """1-based line numbers of git calls in TEXT that name no repository."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    physical = text.splitlines()
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        argv = _git_argv(node)
        if argv is None or _names_a_repo(node, argv) or _creates_its_own_repo(argv):
            continue
        if not _writes(argv, read_only) or _suppressed(node, physical):
            continue
        hits.append(node.lineno)
    return sorted(hits)


MESSAGE = (
    "git argv names no repository — it acts on whatever directory the process "
    "is in, so an in-process run reaches the caller's own checkout. Put "
    "`-C <repo>` first in the argv, pass the call's own `cwd=`, or annotate "
    f"`# {OPT_OUT}: <reason>`."
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--read-only-subcommand",
        action="append",
        default=[],
        metavar="NAME",
        dest="extra_read_only",
        help="an additional git subcommand that only reads (repeatable); "
        "extends the built-in read-only set rather than replacing it",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if not args.files:
        print(
            "check_cwd_scoped_git: no files to scan. This check reads only "
            "the paths you give it, so an empty run would report a clean "
            "pass over nothing.",
            file=sys.stderr,
        )
        return 2
    read_only = READ_ONLY | frozenset(args.extra_read_only)
    return run_line_checks(
        args.files, lambda text: violations(text, read_only), MESSAGE
    )


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
