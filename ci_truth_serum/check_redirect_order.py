#!/usr/bin/env python3
"""Refuse an input redirect written before the stderr redirect that should silence it.

Bash applies the redirects of a command from left to right. In
`cmd <"$path" 2>/dev/null`, bash opens `$path` first. When that open fails, bash
prints the error on the old stderr, because `2>/dev/null` is not in effect yet.
Measured: a merge-queue build failed when a `/proc` entry vanished before a read.

  * An INPUT REDIRECT is a `<` or `<&` redirect, or any redirect of descriptor 0.
    A close (`<&-`), `</dev/null` and `<&2` cannot print an error, so they are not.
  * A VIOLATION is an input redirect written before one of `SILENCERS` in the
    redirect list of one command or one compound command (`done <f 2>/dev/null`).

A heredoc, a here-string or a process substitution has no open that can fail, and
`< <(gen) 2>/dev/null` swapped would also hide the errors of `gen`. A redirect in
a printed string or a heredoc body is text, because the check reads the grammar.
Opt out with `# allow-redirect-order: <reason>` on the line or the line above.
"""

import sys
from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    RedirectParts,
    iter_nodes,
    parse,
    redirect_parts,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    run_line_checks,
    source_lines,
)
from _cts_comments import shell_comments  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-redirect-order"

MESSAGE = (
    "an input redirect comes before the stderr redirect, so a failed open prints "
    "its error before `2>/dev/null` takes effect (bash applies redirects left to right). "
    "Write the stderr redirect first (`cmd 2>/dev/null <f`), or group the command "
    f"(`{{ cmd <f; }} 2>/dev/null`), or annotate `# {OPT_OUT}: <reason>`."
)

_NULL = "/dev/null"
# (descriptor, operator, destination) triples that send stderr nowhere. A close
# names no destination.
SILENCERS = frozenset(
    {("2", op, _NULL) for op in (">", ">>", ">|")}
    | {("2", ">&-", "")}
    | {(None, op, _NULL) for op in ("&>", "&>>", ">&")}
)


def is_input(node: Node, redirect: RedirectParts) -> bool:
    """Whether the redirect NODE, read as REDIRECT, opens a path whose failure prints."""
    destination = node.child_by_field_name("destination")
    if destination is not None and destination.type == "process_substitution":
        return False
    if redirect.destination in ("", _NULL):
        return False
    if redirect.operator == "<&" and redirect.destination == "2":
        return False
    return redirect.operator in ("<", "<&") or redirect.descriptor == "0"


def _own_redirects(node: Node) -> Iterator[Node]:
    """NODE's own `file_redirect` children, and those a heredoc redirect carries."""
    for child in node.children:
        if child.type == "file_redirect":
            yield child
        elif child.type == "heredoc_redirect":
            yield from (c for c in child.children if c.type == "file_redirect")


def _redirect_lists(root: Node) -> Iterator[list[Node]]:
    """One list per command or compound command: the redirects bash applies together.

    A redirect written before the arguments of a command is a child of the
    command. A redirect written after them is a child of the enclosing
    `redirected_statement`. A redirected `{ …; }` or loop has no `command` node,
    so the redirects of its statement form a list of their own."""
    for node in iter_nodes(root, "command", "redirected_statement"):
        if node.type == "command":
            scopes = [node]
            if node.parent is not None and node.parent.type == "redirected_statement":
                scopes.append(node.parent)
            yield [r for scope in scopes for r in _own_redirects(scope)]
            continue
        body = node.child_by_field_name("body")
        if body is None or body.type != "command":
            yield list(_own_redirects(node))


def violations(text: str) -> list[int]:
    """1-based lines of each input redirect written before a silencer of its command."""
    lines = source_lines(text)
    said = shell_comments(text)
    hits: set[int] = set()
    for nodes in _redirect_lists(parse(text)):
        ordered = sorted(nodes, key=lambda n: n.start_byte)
        parts = [redirect_parts(n) for n in ordered]
        first = next(
            (i for i, (n, p) in enumerate(zip(ordered, parts)) if is_input(n, p)), None
        )
        if first is None:
            continue
        if any(tuple(p) in SILENCERS for p in parts[first + 1 :]):
            hits.add(ordered[first].start_point[0] + 1)
    return sorted(
        line for line in hits if not annotated_near(lines, line, OPT_OUT, comments=said)
    )


def main(argv: list[str]) -> int:
    """Run the detector over ARGV, refusing by name a file it cannot read."""
    return run_line_checks(argv, violations, MESSAGE)


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
