#!/usr/bin/env python3
"""Flag a test-side shell function that SHADOWS a production PURE PREDICATE.

A test harness legitimately redefines a production function to intercept a side
effect it cannot afford in a test: `as_root`, `log`/`status`/`warn`, a network
call, a container invocation. That is stubbing a DEPENDENCY, and it is what lets
the real code under test run at all.

A pure predicate is categorically different. Its body only tests and returns —
there is no side effect to intercept and no dependency to fake — so a test that
redefines one is not stubbing anything: it is substituting its own copy of the
logic and then asserting against that copy. The test then stays green through any
regression in the shipped predicate, and the copy is routinely WEAKER than the
original: a `valid_host_port` stub carrying `[[ $1 =~ ^[0-9]+$ ]]` accepts the
leading-zero and overlong-digit-run ports the shipped `^[1-9][0-9]{0,4}$` exists
to reject, so the security regression it was meant to cover cannot be seen.
Source the real definition instead.

The definition, kept literal on purpose:
  * PRODUCTION shell file = a tracked `.bash`/`.sh` file (or an extensionless
    tracked file with a `#!…sh` shebang) that is not a test file.
  * TEST shell file = the same, under a `tests/`-style path — the shared
    `is_test_path` classifier every lint in this pack scopes itself with.
  * A function is a PURE PREDICATE when every statement of its body is a `[[ … ]]`
    / `[ … ]` test, an arithmetic `(( … ))` evaluation, or a bare `return [n]` /
    `true` / `false` — combined only by `&&`, `||` and `!` — AND the body performs
    no assignment, increment, redirection, command substitution, or declaration.
    Those exclusions are what keep a printer, a reader, or a state-mutating helper
    from reading as pure.
  * A VIOLATION is a function defined in a test shell file whose name is also
    defined, as a pure predicate, in some production shell file.

Purity is decided on the real bash grammar (the shared ``_bash_ast`` parser), so
a body wrapped across continuations, a one-liner, and a `$(…)` hiding inside a
`[[ … ]]` are all read exactly as bash reads them. Anything the grammar does not
recognise as one of the shapes above (an `if`, a `case`, a nested brace group, a
span tree-sitter parsed as ERROR) reads as IMPURE — the conservative direction,
which can only lose a hit, never invent one.

A violation is a PAIR — a test-side definition and a production-side predicate —
so the scan is only ever narrowed by the passed file list, never defined by it.
The production set is always the whole tracked tree (`git ls-files`), and the
test side widens to the whole tree whenever a production file is passed, because
that commit can create the pair from the side pre-commit is not passing. Only
when no production file changed does the scan narrow to the passed test files.

Opt out on the redefinition line with `# predicate-shadow-ok: <reason>`.
"""

import sys
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import iter_nodes, parse  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    is_test_path,
    run_file_cli,
    tracked_shell_files,
)

OPT_OUT = "predicate-shadow-ok"

# Commands a predicate may run: they only produce an exit status. `:` is
# deliberately absent — `f() { :; }` is an empty no-op stub with no logic to have
# been copied, and admitting it would flag every do-nothing dependency stub.
_PURE_COMMANDS = frozenset({"return", "true", "false"})
# Node types that make a body impure WHEREVER they appear in it: a shell-out, a
# redirection, a variable write. Scanning the whole subtree is what catches the
# ones nested inside an otherwise test-shaped statement, e.g. `[[ $(id -u) == 0 ]]`.
_IMPURE_NODES = frozenset(
    {
        "command_substitution",
        "process_substitution",
        "file_redirect",
        "heredoc_redirect",
        "variable_assignment",
        "declaration_command",
        "ERROR",
    }
)
# Operator tokens that MUTATE inside an arithmetic `(( … ))` evaluation. Looked
# for only there: in a `[[ … ]]` test the very same `=` is a comparison, so a
# body-wide scan for the token would read `[[ $x = y ]]` as an assignment.
_ARITHMETIC_MUTATORS = frozenset(
    {"=", "++", "--", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|="}
)


def _is_arithmetic(node: Node) -> bool:
    """True for a `(( … ))` evaluation. The grammar spells it as a
    `compound_statement` too (the same type as a `{ … }` group), told apart by the
    opening token."""
    return (
        node.type == "compound_statement"
        and bool(node.children)
        and node.children[0].type == "(("
    )


def _pure_statement(node: Node) -> bool:
    """True when NODE is one of the statement shapes a pure predicate may contain.

    Recursive only through the combinators (`&&`/`||`/`!`): everything else is
    matched by its own node type, so an unrecognised construct is impure by
    default rather than by omission.
    """
    if node.type == "test_command":
        return True
    if _is_arithmetic(node):
        return next(iter_nodes(node, *_ARITHMETIC_MUTATORS), None) is None
    if node.type == "command":
        if not node.named_children:
            return False
        name, *parts = node.named_children
        return (
            # A leading `VAR=x` would make the first named child an assignment
            # rather than the command name — and an assignment is a side effect.
            name.type == "command_name"
            and name.text.decode() in _PURE_COMMANDS
            # `return 1` carries a status; anything richer (a variable, a string)
            # is not the bare form this admits.
            and all(part.type in ("number", "word") for part in parts)
        )
    # `a && b` / `a || b` and `! a`: the combinators are anonymous tokens, so the
    # named children are exactly the operands to recurse into.
    if node.type in ("list", "negated_command"):
        return all(_pure_statement(child) for child in node.named_children)
    return False


def is_pure_predicate(body: Node) -> bool:
    """True when BODY (a function's `compound_statement`) only tests and returns.

    An empty body is NOT pure: there is no logic in it to have been copied.
    """
    # Named children only: `{`, `}` and the `;` separators are anonymous tokens,
    # punctuation the grammar already distinguishes from statements.
    statements = [child for child in body.named_children if child.type != "comment"]
    if not statements:
        return False
    if any(True for _ in iter_nodes(body, *_IMPURE_NODES)):
        return False
    return all(_pure_statement(statement) for statement in statements)


def function_definitions(text: str) -> list[tuple[str, int]]:
    """(name, 1-based line) for every shell function TEXT defines, in source order.

    Both the `name() { … }` and `function name { … }` forms; the name is the
    definition's first bare word either way.
    """
    defs: list[tuple[str, int]] = []
    for node in iter_nodes(parse(text), "function_definition"):
        name = next((c for c in node.children if c.type == "word"), None)
        if name is not None:
            defs.append((name.text.decode(), node.start_point[0] + 1))
    return defs


def pure_predicates(text: str) -> dict[str, int]:
    """{name: 1-based line} for every function TEXT defines as a pure predicate.

    A name defined more than once keeps its FIRST pure definition, so the report
    points at the definition a reader finds first.
    """
    found: dict[str, int] = {}
    for node in iter_nodes(parse(text), "function_definition"):
        name = next((c for c in node.children if c.type == "word"), None)
        body = next((c for c in node.children if c.type == "compound_statement"), None)
        if name is None or body is None:
            continue
        key = name.text.decode()
        if key not in found and is_pure_predicate(body):
            found[key] = node.start_point[0] + 1
    return found


class Shadow(NamedTuple):
    """A test-side redefinition of a production pure predicate: the test file
    ``path`` and ``lineno`` it is defined at, the function ``name``, and ``source``,
    the production file whose pure predicate it shadows."""

    path: str
    lineno: int
    name: str
    source: str


def _read(path: str) -> str | None:
    """PATH's text, or None when it cannot be read — a deleted/renamed path
    pre-commit still lists, or a binary file with a shell-ish name."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def production_predicates(paths: list[str]) -> dict[str, str]:
    """{function name: defining file} over every pure predicate PATHS ship.

    First definition wins, over path-sorted input, so the reported source file is
    stable across runs.
    """
    predicates: dict[str, str] = {}
    for path in sorted(paths):
        text = _read(path)
        if text is None:
            continue
        for name in pure_predicates(text):
            predicates.setdefault(name, path)
    return predicates


def find_shadowed(test_paths: list[str], predicates: dict[str, str]) -> list[Shadow]:
    """Every redefinition of a PREDICATES name in TEST_PATHS, sorted for a stable
    report.

    The opt-out is read from the definition's own line.
    """
    hits: list[Shadow] = []
    for path in sorted(test_paths):
        text = _read(path)
        if text is None:
            continue
        lines = text.splitlines()
        for name, lineno in function_definitions(text):
            if name not in predicates or annotated_near(lines, lineno, OPT_OUT):
                continue
            hits.append(Shadow(path, lineno, name, predicates[name]))
    return sorted(hits)


def scan_targets(argv: list[str]) -> tuple[list[str], list[str]]:
    """(test paths to scan, production paths to read predicates from) for ARGV.

    A violation is a PAIR — a test-side definition and a production-side pure
    predicate — so which files changed decides only where the scan can be
    NARROWED, never what the answer depends on. The production side is therefore
    always the whole tracked tree, and the test side widens to the whole tree
    whenever a production file is in play: a commit that makes a shipped function
    a pure predicate (or renames one onto a name a long-untouched test already
    defines) creates the pair from the side pre-commit is not passing, and
    scanning only the passed files would report clean on exactly that commit.

    Narrowing to the passed test files is sound in the remaining case: with no
    production file changed, only a test-side definition can be new.
    """
    passed = [arg for arg in argv if not arg.startswith("--")]
    tracked = tracked_shell_files()
    production = [path for path in tracked if not is_test_path(path)]
    every_test = [path for path in tracked if is_test_path(path)]
    if "--all" in argv or any(not is_test_path(path) for path in passed):
        return every_test, production
    return [path for path in passed if is_test_path(path)], production


def main(argv: list[str]) -> int:
    test_paths, production = scan_targets(argv)
    if not test_paths:
        return 0  # nothing test-side in view can violate
    predicates = production_predicates(production)
    status = 0
    for path, lineno, name, source in find_shadowed(test_paths, predicates):
        print(
            f"{path}:{lineno}: {name}() redefines the pure predicate shipped at "
            f"{source} — this test asserts against its own copy of the logic, so a "
            f"regression in the real one cannot fail it. Source {source} instead, "
            f"or annotate the line with `# {OPT_OUT}: <reason>`.",
            file=sys.stderr,
        )
        status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
