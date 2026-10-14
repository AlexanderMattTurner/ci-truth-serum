#!/usr/bin/env python3
"""Flag a path found by counting the parents of ``__file__``.

``Path(__file__).resolve().parents[3]`` states a depth that nothing checks. Move
the file one directory and the expression still evaluates. It now names a
directory that holds no repository. A check that globs under that directory finds
nothing, reports a clean result, and exits 0 on every later run.

The fix is to find the root by a marker. Walk up from ``__file__`` to the first
directory that holds ``.git`` or ``pyproject.toml``, and raise when no directory
holds it. ``git rev-parse --show-toplevel`` gives the same answer.

The rule is a node shape (``_cts_py_ast``): a ``parents[…]`` subscript whose
object expression reads ``__file__``. A ``parents[1]`` on a path from somewhere
else counts inside a structure the caller already holds, so it passes. A
``.parent.parent`` chain does not match.

Opt out with ``# allow-counted-root: <reason>`` on a line the expression spans,
or in the comment block directly above it. The reason is REQUIRED.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import annotated_near, run_file_cli, run_line_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_py_ast import lines, trees, walk  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-counted-root"

MESSAGE = (
    "this path counts parents up from `__file__`. Nothing checks that depth, so "
    "a moved file points at a directory that does not exist, and a scan under it "
    "reports a clean result. Find the root by a marker instead: walk up to the "
    "first directory that holds `.git` (or `pyproject.toml`) and raise when none "
    f"does. Or annotate a deliberate count `# {OPT_OUT}: <reason>`."
)


def _reads_dunder_file(node: ast.AST) -> bool:
    """True when ``__file__`` appears anywhere in the expression NODE."""
    return any(
        isinstance(sub, ast.Name) and sub.id == "__file__" for sub in ast.walk(node)
    )


def _counted_roots(tree: ast.Module) -> list[ast.Subscript]:
    """Every ``<…__file__…>.parents[…]`` subscript in TREE."""
    return [
        node
        for node in walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and _reads_dunder_file(node.value.value)
    ]


def violations(text: str) -> list[int]:
    """1-based line numbers in TEXT that count a root off ``__file__``.

    The finding sits on the line where the subscript starts. The opt-out may sit
    on any line the subscript spans.
    """
    physical = lines(text)
    hits = {
        node.lineno
        for tree in trees(text)
        for node in _counted_roots(tree)
        if not annotated_near(physical, node.lineno, OPT_OUT, span_end=node.end_lineno)
    }
    return sorted(hits)


def main(argv: list[str]) -> int:
    return run_line_checks(argv, violations, MESSAGE)


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
