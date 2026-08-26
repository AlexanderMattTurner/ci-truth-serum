#!/usr/bin/env python3
"""Fail when a type annotation uses a positional ``tuple[...]`` of many fixed
elements — a "cursed tuple" begging to be a named structure.

A fixed-length heterogeneous tuple (``tuple[str, int, bool]``) forces every
call site to remember what position means what. The fields have no names, a
reordered pair is a silent bug, and the annotation documents nothing. Past
the ``--min-elements`` threshold (default 3) the readability cost dominates:
convert it to a ``typing.NamedTuple`` (a drop-in — it still unpacks, indexes,
hashes, and ``== plaintuple``) so the fields carry names.

Scope: the ``.py`` paths given on argv, tests excluded (a test's ad-hoc tuple
carries no production-runtime contract). Flags an annotation subscripting
``tuple`` / ``Tuple`` whose slice is a fixed tuple of at least
``--min-elements`` elements. Variadic ``tuple[X, ...]`` (a homogeneous
sequence, not a positional record) is never flagged.

Where the marker may sit: the enclosing parameter, assignment, or — for a
bare return annotation — the function signature (never its body), so a
formatter that wraps a long annotation across lines never strands the
marker. A genuinely justified case — a table row type, an interop shape — is
exempted with ``# big-tuple-ok: <reason>`` anywhere in that span; the reason
is mandatory so the exemption is review-visible.
"""

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    is_test_path,
    run_file_cli,
)
from _py_ast import lines as py_lines  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _py_ast import trees  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "big-tuple-ok"
DEFAULT_MIN_ELEMENTS = 3

_TUPLE_NAMES = frozenset({"tuple", "Tuple"})


def _is_tuple_subscript(node: ast.Subscript) -> bool:
    value = node.value
    if isinstance(value, ast.Name):
        return value.id in _TUPLE_NAMES
    if isinstance(value, ast.Attribute):
        return value.attr in _TUPLE_NAMES
    return False


def _fixed_element_count(node: ast.Subscript) -> int:
    """The count of fixed positional elements, or 0 for a variadic / single-arg
    tuple this guard never flags."""
    sl = node.slice
    if not isinstance(sl, ast.Tuple):
        return 0  # tuple[X] — a one-element (or unparametrized) tuple, not a record
    elts = sl.elts
    # Variadic tuple[X, ...] is exactly this two-element shape (PEP 484) — a
    # homogeneous sequence, not a positional record. An Ellipsis anywhere else
    # (e.g. a bogus trailing `tuple[str, int, bool, ...]`) is still a fixed
    # record and must not slip past the guard unflagged.
    if (
        len(elts) == 2
        and isinstance(elts[1], ast.Constant)
        and elts[1].value is Ellipsis
    ):
        return 0
    return len(elts)


def _suppression_span(node: ast.AST, parents: dict[int, ast.AST]) -> tuple[int, int]:
    """The line range the ``big-tuple-ok:`` marker may sit in to exempt NODE.

    The formatter freely wraps a long annotation, so the marker can land a
    line or two off the subscript's own span (on the closing ``]`` or
    ``] = (``). Climb to the enclosing logical unit — the parameter
    (``ast.arg``), the annotated/plain assignment, or, for a bare return
    annotation, the function SIGNATURE (never its body) — and accept the
    marker anywhere in that unit.
    """
    cur: ast.AST | None = node
    while cur is not None:
        if isinstance(cur, (ast.arg, ast.AnnAssign, ast.Assign)):
            return cur.lineno, cur.end_lineno or cur.lineno
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_start = (
                cur.body[0].lineno if cur.body else (cur.end_lineno or cur.lineno)
            )
            return cur.lineno, body_start - 1
        cur = parents.get(id(cur))
    # Fallback: node is typed ast.AST (no lineno on the base class), so read
    # positionally via getattr — a flagged Subscript always carries both.
    lineno = getattr(node, "lineno", 1)
    return lineno, getattr(node, "end_lineno", None) or lineno


def violations(
    text: str, min_elements: int = DEFAULT_MIN_ELEMENTS
) -> list[tuple[int, int]]:
    """(1-based line, element count) for every unexempted fixed tuple[...] of
    at least MIN_ELEMENTS elements.

    Parsed through ``_py_ast.trees``: a file that does not parse as a whole
    still gets a per-line best-effort scan rather than reporting "no
    findings" — the false green this pack refuses to produce for source it
    was actually handed.
    """
    lines = py_lines(text)
    hits: list[tuple[int, int]] = []
    for tree in trees(text):
        parents = {
            id(child): parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript) or not _is_tuple_subscript(node):
                continue
            count = _fixed_element_count(node)
            if count < min_elements:
                continue
            start, end = _suppression_span(node, parents)
            if annotated_near(lines, start, OPT_OUT, span_end=end):
                continue
            hits.append((node.lineno, count))
    return sorted(hits)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-elements",
        type=int,
        default=DEFAULT_MIN_ELEMENTS,
        help=f"minimum fixed-element count that triggers a finding (default {DEFAULT_MIN_ELEMENTS})",
    )
    parser.add_argument("paths", nargs="*", metavar="FILE")
    args = parser.parse_args(argv)

    rc = 0
    for arg in args.paths:
        if is_test_path(arg):
            continue
        path = Path(arg)
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, count in violations(source, args.min_elements):
            print(
                f"{path}:{lineno}: positional tuple[...] of {count} elements — "
                "convert to a typing.NamedTuple so the fields have names "
                f"(or exempt with '# {OPT_OUT}: <reason>').",
                file=sys.stderr,
            )
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
