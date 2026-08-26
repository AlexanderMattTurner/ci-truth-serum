#!/usr/bin/env python3
"""Ban a new hand-rolled attempt-and-sleep loop.

The defect is a loop with an ATTEMPT BUDGET that sleeps between attempts: it
steps a counter and compares it (or spells the budget out as `for attempt in
1 2 3`), and its own body runs `sleep`. Three structural facts separate it
from a live non-retry loop:

  * The `sleep` is a COMMAND THIS LOOP RUNS, not one in a comment, an inert
    heredoc, a nested loop, or a function defined in the body.
  * The loop is COUNTER-BOUND. One repeating until the world changes — a stop
    file, a dead process, an answering port — has no attempt budget, and nor
    has a stepped frame index nobody compares.
  * The loop does not COMPARE A CLOCK (`SECONDS`, `EPOCHSECONDS`,
    `EPOCHREALTIME`): that is a deadline loop, whose worst case survives a
    slow machine a fixed attempt count would cut short.

Configure the repo's one retry primitive with `--retry-helper TEXT` (used only
in the failure message) and the names of loop-body calls that already
delegate to it with a repeatable `--wrapper NAME`. A loop that must stay
hand-rolled opts out with `# retry-loop-ok: <reason>` (a reason is required)
on its own `for`/`while`/`until` line, or in the comment block above it.

KNOWN BLIND SPOT: the pause must be `sleep` itself.
"""

import argparse
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    command_name,
    command_words,
    node_text,
    parse,
    unquote,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    run_source_checks,
)

OPT_OUT = "retry-loop-ok"

# `until` is a `while_statement` whose first child is the `until` keyword, so
# the grammar needs no separate entry for it.
_LOOP_TYPES = frozenset({"while_statement", "for_statement", "c_style_for_statement"})

# The expression nodes of bash arithmetic, where a bare word is a variable read.
_ARITHMETIC = frozenset(
    {
        "binary_expression",
        "unary_expression",
        "postfix_expression",
        "ternary_expression",
        "parenthesized_expression",
    }
)

# Arithmetic that COUNTS (`i++`, `i += 1`); a plain `i = i + 1` is read below,
# separately, by looking for the target name on both sides.
_STEP_OPERATORS = frozenset({"++", "--", "+=", "-="})

# The operators that COMPARE, deciding when a loop ends; a name under `=`/`+`
# is only being computed.
_COMPARISONS = frozenset({"<", "<=", ">", ">=", "==", "!="})

# Bash's own wall clocks. A loop that COMPARES one of these is bounded by
# TIME, so its worst case survives a slow machine a fixed attempt count would
# cut short. Comparing, not merely mentioning: a status line reading
# `${SECONDS}` decides nothing about when the loop ends.
_CLOCKS = frozenset({"SECONDS", "EPOCHSECONDS", "EPOCHREALTIME"})

# Command words that do not bound the command that follows them — a real
# bound is a wrapper (a retry helper, a timeout) standing in front of one.
_TRANSPARENT_PREFIXES = frozenset(
    {"time", "command", "builtin", "nohup", "sudo", "exec"}
)


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _own_nodes(body: Node) -> Iterator[Node]:
    """The nodes BODY owns directly: its whole subtree, minus every nested
    loop and function definition, which own their own contents."""
    stack = list(reversed(body.children))
    while stack:
        node = stack.pop()
        if node.type in _LOOP_TYPES or node.type == "function_definition":
            continue
        yield node
        stack.extend(reversed(node.children))


def _is_arithmetic_chain(node: Node) -> bool:
    """True when NODE is part of a `(( … ))` chain rather than a `[[ … ]]`
    test — `[[ … ]]` spells its comparisons `-lt`/`-le` where arithmetic
    spells `<`/`<=`."""
    parent = node.parent
    while parent is not None and parent.type in _ARITHMETIC:
        parent = parent.parent
    return parent is not None and parent.type != "test_command"


def _is_arithmetic_operand(node: Node) -> bool:
    parent = node.parent
    if parent is None or parent.type not in _ARITHMETIC:
        return False
    return _is_arithmetic_chain(parent)


def _variable_name(node: Node) -> str:
    """The name NODE refers to, or `""` when it names nothing: a plain
    `variable_name`; an arithmetic word (the `tries` of `((i < tries))` is a
    `word` node, not a `variable_name`); or an expansion giving up its
    `variable_name` child (`${tries:-30}` -> `tries`)."""
    if node.type == "variable_name":
        return node_text(node)
    if node.type == "word":
        return node_text(node) if _is_arithmetic_operand(node) else ""
    inner = next((c for c in node.children if c.type == "variable_name"), None)
    return node_text(inner) if inner is not None else ""


def _stepped_names(nodes: list[Node]) -> set[str]:
    """The variables NODES step by a constant: `i++`, `i--`, `i += 1`,
    `i -= 1`, and `i=$((i + 1))` (tell: the name on both sides)."""
    stepped: set[str] = set()
    for node in nodes:
        if node.type in _ARITHMETIC:
            target = _variable_name(node.children[0]) if node.children else ""
            operators = {c.type for c in node.children}
            steps = bool(operators & _STEP_OPERATORS) or (
                "=" in operators
                and target
                in {_variable_name(v) for c in node.children[2:] for v in _walk(c)}
            )
            if steps and target:
                stepped.add(target)
        elif node.type == "variable_assignment":
            target_node = node.child_by_field_name("name")
            value = node.child_by_field_name("value")
            if target_node is None or value is None:
                continue
            if value.type == "arithmetic_expansion" and node_text(target_node) in {
                _variable_name(v) for v in _walk(value)
            }:
                stepped.add(node_text(target_node))
    return stepped


def _compares(node: Node) -> bool:
    """True when the arithmetic chain rooted at NODE holds a comparison."""
    return any(c.type in _COMPARISONS for n in _walk(node) for c in n.children)


def _tested_names(roots: list[Node]) -> set[str]:
    """The variables the conditions under ROOTS read to decide when a loop
    ENDS. Inside `(( … ))` only a COMPARISON counts: `((n >= max))` bounds the
    loop, while `((waited = SECONDS - start))` merely computes, and taking
    every name there would buy the deadline exemption for a real retry that
    only ever PRINTS the clock. Everything else — a `[[ … ]]` test, a
    header's word list — is the condition entire."""
    names: set[str] = set()

    def visit(node: Node) -> None:
        if node.type in _ARITHMETIC and _is_arithmetic_chain(node):
            if _compares(node):
                names.update(_variable_name(n) for n in _walk(node))
            return
        names.add(_variable_name(node))
        for child in node.children:
            visit(child)

    for root in roots:
        visit(root)
    return names - {""}


def _is_arithmetic_command(node: Node) -> bool:
    """True for a `(( … ))` run as a command — the same `compound_statement`
    node type a `{ … }` group gets, distinguished by its opening delimiter."""
    return node.type == "compound_statement" and bool(
        node.children and node.children[0].type == "(("
    )


def _substitutes_seq(node: Node) -> bool:
    """True for a `$(seq …)` word."""
    if node.type != "command_substitution":
        return False
    return any(command_name(c) == "seq" for c in _walk(node) if c.type == "command")


def _iterates_a_budget(node: Node) -> bool:
    """True for a `for` whose word list is itself a fixed count, with no
    variable to step: `for x in 1 2 3`, `for x in {1..5}`, `for x in $(seq 1
    5)`. `brace_expression` is only ever the `{N..M}` range form — the
    grammar spells `{a,b}` a `concatenation` — so the node type alone proves
    it is numeric."""
    if node.type != "for_statement":
        return False
    words = [
        c
        for c in node.children
        if c.type not in ("for", "in", "variable_name", ";", "do_group")
    ]
    if not words:
        return False
    if all(c.type in ("number", "brace_expression") for c in words):
        return True
    return len(words) == 1 and _substitutes_seq(words[0])


@dataclass(frozen=True)
class Loop:
    """One `while`/`until`/`for` statement. `own_commands` covers only what
    this loop runs DIRECTLY, so a nested loop's `sleep` is not this one's.
    `compared_variables` is every variable the loop TESTS. `counter_bound` is
    true when the loop runs a fixed number of times."""

    line: int
    end_line: int
    own_commands: tuple[Node, ...]
    compared_variables: frozenset[str]
    counter_bound: bool


def _build_loop(node: Node) -> Loop:
    body = next((c for c in node.children if c.type == "do_group"), None)
    if body is None:
        raise ValueError(f"loop node with no do_group: {node_text(node)!r}")
    header = [c for c in node.children if c is not body]
    owned = list(_own_nodes(body))
    # A stepped variable counts only where something also compares it — a
    # step nobody compares bounds nothing (a spinner-frame `i` steps
    # forever). A comparison lives either in the header (`while ((i <
    # steps))`) or in a test the body runs (the `((attempt >= max))` a
    # `while true` loop puts inside itself).
    tested = _tested_names(
        header
        + [c for c in owned if c.type == "test_command" or _is_arithmetic_command(c)]
    )
    stepped = _stepped_names([n for c in header for n in _walk(c)] + owned)
    return Loop(
        line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        own_commands=tuple(n for n in owned if n.type == "command"),
        compared_variables=frozenset(tested),
        counter_bound=bool(stepped & tested) or _iterates_a_budget(node),
    )


def _loops(source: str) -> list[Loop]:
    return [_build_loop(n) for n in _walk(parse(source)) if n.type in _LOOP_TYPES]


def _effective_first_word(node: Node) -> str:
    """The command's first word that names the program being run, past a
    transparent prefix (`command sleep 1` runs `sleep`)."""
    words = command_words(node)
    index = 0
    while index < len(words) - 1 and unquote(words[index]) in _TRANSPARENT_PREFIXES:
        index += 1
    return unquote(words[index]) if words else ""


def _delegates(own_commands: tuple[Node, ...], wrappers: frozenset[str]) -> bool:
    """True when a call to one of WRAPPERS appears anywhere in a command this
    loop runs directly — the loop already hands its retrying to that helper."""
    return bool(wrappers) and any(
        wrappers & set(command_words(node)) for node in own_commands
    )


def _hand_rolled_retry(loop: Loop, wrappers: frozenset[str]) -> bool:
    """True when LOOP is an attempt-and-sleep loop the retry primitive could
    run instead."""
    if not loop.counter_bound or loop.compared_variables & _CLOCKS:
        return False
    if _delegates(loop.own_commands, wrappers):
        return False
    return any(_effective_first_word(n) == "sleep" for n in loop.own_commands)


def violations(text: str, wrappers: frozenset[str] = frozenset()) -> list[int]:
    """1-based start lines of the counted attempt-and-sleep loops in TEXT that
    no `# retry-loop-ok:` annotation exempts."""
    physical = text.splitlines()
    hits: list[int] = []
    for loop in _loops(text):
        if not _hand_rolled_retry(loop, wrappers):
            continue
        if annotated_near(physical, loop.line, OPT_OUT, span_end=loop.line):
            continue
        hits.append(loop.line)
    return sorted(hits)


def _remedy(retry_helper: str) -> str:
    helper = (
        f"the retry primitive in `{retry_helper}`"
        if retry_helper
        else "your retry helper"
    )
    return f"call {helper}, or annotate `# {OPT_OUT}: <reason>` on the loop's own opening line."


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retry-helper",
        default="",
        metavar="TEXT",
        help="the path or name of this repo's retry primitive, named in the "
        "failure message only",
    )
    parser.add_argument(
        "--wrapper",
        action="append",
        default=[],
        metavar="NAME",
        help="a call to this name inside the loop body means the loop already "
        "delegates (repeatable)",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if not args.files:
        print(
            "check_retry_loop: no files to scan. This check reads only the "
            "paths you give it, so an empty run would report a clean pass "
            "over nothing.",
            file=sys.stderr,
        )
        print(
            "  to scan the whole tree: git ls-files -z | xargs -0 python -m "
            "ci_truth_serum.check_retry_loop",
            file=sys.stderr,
        )
        return 2
    wrappers = frozenset(args.wrapper)
    message = (
        "hand-rolled retry: a counted loop that sleeps between attempts. "
        "Written again here, its attempt count, backoff and give-up message "
        f"drift from every other copy. {_remedy(args.retry_helper)}"
    )
    return run_source_checks(
        args.files, lambda text, _path: violations(text, wrappers), message
    )


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
