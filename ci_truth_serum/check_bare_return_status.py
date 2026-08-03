#!/usr/bin/env python3
"""Require an explicit status on a `return` or `exit` that a `&&` or `||` guards.

A bare `return` gives back the status of the last command that ran. The operator
in front of it decides which command that was, so the operator also decides the
value:

  * `A && return` runs the return only after A SUCCEEDED. The status is always 0.
  * `A || return` runs the return only after A FAILED. The status is A's own code.

Both spellings look the same, and they return opposite values. Real incident: a
session-start hook held three `[[ … ]] && return` guards. Somebody added a fourth
guard in the opposite polarity, `[[ … ]] || return`. That guard returned 1, the
function was the last statement of the script, and the hook failed on every
session. It turned the hook smoke tests and 9 test shards red.

The check flags a bare `return` or `exit` whose forwarded status is a CONSTANT,
because there the author gains nothing from the bare spelling and loses the
reader's ability to see the value:

  * after `&&`, whatever the left side is — the status is always 0.
  * after `||`, when the left side can only fail with status 1. That means a
    `[[ … ]]` or `[ … ]` test, a `test` or `[` command, an arithmetic `(( … ))`
    command, or a negated command (`! cmd`).

The check does NOT flag `||` after any other command. There the bare `return`
forwards a real code — 127 for a missing program, 2 for a `grep` error — and
`return 1` would destroy it. This carve-out is the reason the check is precise.
The grammar spells `(( … ))` and `{ …; }` with the same node type, so the check
reads the opening token to tell them apart: a brace group fails with the status
of the last command in it, which is not a constant.

Two misses are deliberate. A nested list of tests (`[[ a ]] && [[ b ]] || return`)
does fail with a constant 1, but the check does not follow constancy through
nesting. A bare `return` in an `if` body or a `case` arm has an implicit status
too, but no operator states the intent, and this pack keeps no baseline to hold
the noise.

The block is found with tree-sitter-bash (the shared ``_bash_ast`` grammar), so a
continued list, a nested list, and a `return` quoted inside a string or a heredoc
are all read the way bash reads them.

Opt out with `# allow-bare-return: <reason>` on the guard line, or on the line
above it, when the forwarded status really is the one you want.

Invoked by pre-commit with the staged shell files as arguments.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_name,
    command_words,
    iter_nodes,
    parse,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_line_checks,
)

OPT_OUT = "allow-bare-return"

# The builtins that hand a status back to a caller. Both read the last command's
# status when they carry no argument of their own.
_STATUS_BUILTINS = frozenset({"return", "exit"})

# The commands that ARE a test, written as a command rather than as `[[ … ]]`.
_TEST_BUILTINS = frozenset({"test", "["})

# Node types that can only ever fail with status 1. `test_command` covers `[[ … ]]`
# and `[ … ]`; `negated_command` covers `! cmd`, which fails only when cmd
# succeeded.
_CONSTANT_FAILURE_TYPES = frozenset({"test_command", "negated_command"})

MESSAGE = (
    "this bare `return`/`exit` always hands back the same status: 0 after `&&`, "
    "or 1 after a `||` whose left side is a test. The operator decides the value, "
    "not the code, so a guard written in the other polarity returns the opposite "
    "status by accident. Write the status you mean (`return 0` or `return 1`), or "
    f"annotate `# {OPT_OUT}: <reason>` when the forwarded status is the one you want."
)


def _is_arithmetic_command(node) -> bool:
    """True for an arithmetic `(( … ))` command.

    The grammar gives `(( … ))` and a brace group `{ …; }` the SAME node type, and
    the two fail differently: an arithmetic command fails with a constant 1, while
    a brace group fails with the status of the last command in it. The opening
    token is what separates them.
    """
    return (
        node.type == "compound_statement"
        and bool(node.children)
        and node.children[0].type == "(("
    )


def _fails_with_constant_status(node) -> bool:
    """True when NODE can only fail with status 1.

    This refusal is what keeps `cmd || return` unflagged: a real command fails with
    its own code (127, 42), and demanding an explicit status there would ask the
    author to destroy that code.
    """
    if node.type in _CONSTANT_FAILURE_TYPES:
        return True
    if _is_arithmetic_command(node):
        return True
    return node.type == "command" and command_name(node) in _TEST_BUILTINS


def _is_bare_status_builtin(node) -> bool:
    """True for a `return` or `exit` that carries no status argument.

    Read through `command_words`, which is the command's name followed by its
    argument words, so a bare builtin is exactly one word. `command_arguments`
    would be the wrong question here: it yields the NAME's children too, so a bare
    `return` looks like it takes an argument.
    """
    if node.type != "command":
        return False
    words = command_words(node)
    return len(words) == 1 and words[0] in _STATUS_BUILTINS


def _operands(list_node) -> tuple:
    """The left and right operands of a `&&`/`||` list, ignoring comments."""
    parts = [child for child in list_node.named_children if child.type != "comment"]
    return (parts[0], parts[-1]) if len(parts) >= 2 else (None, None)


def _operator(list_node) -> str | None:
    """The `&&` or `||` token that joins the list's operands."""
    found = [child.type for child in list_node.children if child.type in ("&&", "||")]
    return found[-1] if found else None


def violations(text: str) -> list[int]:
    """1-based line numbers of `&&`/`||` guards whose bare `return`/`exit` forwards
    a constant status (and that carry no opt-out annotation)."""
    physical = text.splitlines()
    hits: list[int] = []
    for list_node in iter_nodes(parse(text), "list"):
        left, right = _operands(list_node)
        if right is None or not _is_bare_status_builtin(right):
            continue
        operator = _operator(list_node)
        if operator is None:
            continue
        if operator == "||" and not _fails_with_constant_status(left):
            continue
        lineno = list_node.start_point[0] + 1
        # A list continues across lines on a trailing `&&` or a backslash, so the
        # annotation window has to cover the whole construct — otherwise the
        # opt-out cannot be written on the line the `return` is on.
        if annotated_near(
            physical, lineno, OPT_OUT, span_end=list_node.end_point[0] + 1
        ):
            continue
        hits.append(lineno)
    return sorted(hits)


def main(argv: list[str]) -> int:
    """Run the detector over ARGV through the shared read/report loop, one path at
    a time so a file the grammar refuses to parse safely fails LOUDLY (naming the
    path, exit 1) instead of being silently skipped, while every remaining path is
    still checked."""
    status = 0
    for path in argv:
        try:
            status = max(status, run_line_checks([path], violations, MESSAGE))
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
