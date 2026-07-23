"""Shared tree-sitter-bash parsing for the shell-analyzing lints.

`check_workflow_pipefail` and `check_flag_arity` used to approximate bash with
hand-rolled char-by-char quote/heredoc state machines and a stack-based
`case…esac` scanner. Those approximations mis-parsed real shell — an escaped
quote (`"a\\""`), a `$'…'` ANSI-C string, a nested `$()`/backtick command
substitution, or a heredoc all desynced the quote state and hid (or invented) a
pipe. This module hands both lints a REAL bash grammar instead, so what the lint
sees is what bash would run.

Fails LOUD when the grammar bindings are absent: a shell lint that silently
degrades to "no findings" on a missing dependency would be exactly the false
green this pack exists to catch, so the ImportError propagates rather than being
swallowed. The bindings are pinned as a hook runtime dependency
(pyproject `dependencies`, and each hook's `additional_dependencies`), so
pre-commit and CI always have them.
"""

import tree_sitter_bash
from tree_sitter import Language, Node, Parser


class PathologicalInputError(ValueError):
    """Raised instead of feeding tree-sitter an input shape measured to allocate
    quadratically. Deliberately LOUD: a lint that silently skipped the file
    would false-green exactly the input an adversary controls."""


# tree-sitter-bash's GLR machinery allocates roughly QUADRATICALLY in the number
# of chained pipeline stages: 5k `cmd |` stages cost ~330 MB, 20k cost ~3.3 GB,
# 50k exhaust a 16 GB host (measured via resource.ru_maxrss on tree-sitter-bash
# 0.25). A hostile or generated file can therefore take the whole process down
# inside the C parser — an allocation-failure segfault, not a Python exception —
# so `parse` refuses such inputs up front. Real shell sits orders of magnitude
# below the cap (this repo's largest script carries a few dozen `|` bytes).
_MAX_PIPE_BYTES = 2_000

# Building the Language once is cheap; reuse it across every parse in a run.
_PARSER: Parser | None = None


def _parser() -> Parser:
    global _PARSER
    if _PARSER is None:
        _PARSER = Parser(Language(tree_sitter_bash.language()))
    return _PARSER


def parse(script: str) -> Node:
    """The root node of SCRIPT parsed as bash.

    tree-sitter NEVER raises on malformed input — a syntax error surfaces as
    ``ERROR`` nodes in the tree, so callers fail OPEN (treat unparseable spans as
    benign) instead of crashing a pre-commit hook on an unrelated commit. The one
    exception is a pipe-byte count past ``_MAX_PIPE_BYTES``, which raises
    ``PathologicalInputError`` (loud, never a silent pass) rather than letting
    the C parser's quadratic allocation kill the process."""
    if script.count("|") > _MAX_PIPE_BYTES:
        raise PathologicalInputError(
            f"input carries more than {_MAX_PIPE_BYTES} pipe bytes; "
            "tree-sitter-bash allocates quadratically on chained pipelines, so "
            "parsing it could exhaust memory. Split the file or reduce the "
            "pipeline chain to lint it."
        )
    return _parser().parse(script.encode("utf-8")).root_node


def iter_nodes(node: Node, *types: str):
    """Every descendant of NODE (inclusive) whose ``type`` is in TYPES, yielded in
    document (pre-order) order."""
    want = set(types)
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in want:
            yield current
        # Reverse so children are popped left-to-right → pre-order, source order.
        stack.extend(reversed(current.children))
