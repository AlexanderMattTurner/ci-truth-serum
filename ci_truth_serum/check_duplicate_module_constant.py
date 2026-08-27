#!/usr/bin/env python3
"""Flag a module-level name assigned more than once at the top level of one file.

A constant defined twice at module scope silently SHADOWS its first copy —
the second binding wins, and an edit to the first is discarded with no
error.

The rule, kept precise so it stays dogfood-clean:

- Consider ONLY statements directly in the module body. A name (re)bound on
  a conditional branch is a deliberate alternative definition, not a shadow.
- A binding is an ``ast.Assign`` or an ``ast.AnnAssign`` that carries a
  value; a bare annotation is a declaration. ``ast.AugAssign`` (``x += …``)
  is never counted.
- Assignment targets contribute a name only for plain ``Name`` targets,
  recursing through tuple/list unpacking and ``Starred``; Attribute/Subscript
  targets mutate an object, not a name.
- A re-binding whose value reads the name it binds (``x = x + 1``) is an
  intentional accumulation, never flagged.

Reported line numbers are the second and each later binding; the first
stays. Opt out with ``# allow-duplicate-constant: <reason>`` on any line the
OFFENDING statement itself spans — never on the line above, since a
duplicate is usually the very NEXT statement and a reason meant for the
first binding must not silently reach the second.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
    run_file_cli,
    run_line_checks,
)
from _cts_py_ast import lines as py_lines  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_py_ast import trees  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-duplicate-constant"


def _target_names(target: ast.expr) -> list[str]:
    """The plain ``Name`` ids a single target binds, recursing through
    tuple/list unpacking and ``Starred``. Attribute/Subscript targets
    contribute nothing."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_target_names(elt))
        return names
    return []


def _bound_names(stmt: ast.stmt) -> list[str]:
    """The module-level names a top-level statement BINDS with a value: every
    ``Name`` target of an ``Assign``, or the single target of a
    value-carrying ``AnnAssign``."""
    if isinstance(stmt, ast.Assign):
        names: list[str] = []
        for target in stmt.targets:
            names.extend(_target_names(target))
        return names
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        return _target_names(stmt.target)
    return []


def _value_reads_name(stmt: ast.stmt, name: str) -> bool:
    """True when the statement's value expression references NAME — an
    accumulation/rebuild (``x = x + 1``, ``__all__ = __all__ + more``) that
    reads the prior binding, so it is intentional, not a shadow."""
    value = getattr(stmt, "value", None)
    if value is None:
        return False
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(value))


def _line_at(lines: list[str], lineno: int) -> str:
    """LINES's 1-based LINENO, or "" out of range. A named lookup rather than
    an inline ``lines[lineno - 1]`` at each call site, so the opt-out check
    below reads one line at a time with no neighbour-reaching arithmetic."""
    return lines[lineno - 1] if 0 < lineno <= len(lines) else ""


def _suppressed(stmt: ast.stmt, lines: list[str]) -> bool:
    """True when OPT_OUT annotates a line the offending STATEMENT ITSELF
    spans — never the line above, which would usually be the unrelated FIRST
    binding this duplicate shadows."""
    end = stmt.end_lineno or stmt.lineno
    return any(
        annotated(_line_at(lines, n), OPT_OUT) for n in range(stmt.lineno, end + 1)
    )


def violations(text: str) -> list[int]:
    """1-based line numbers of module-level re-bindings that shadow an
    earlier binding of the same name (the second and each later one).

    Parsed through ``_cts_py_ast.trees``: a file that does not parse as a whole
    still gets a per-line best-effort scan rather than reporting "no
    findings" — the false green this pack refuses to produce for source it
    was actually handed.
    """
    lines = py_lines(text)
    seen: set[str] = set()
    hits: set[int] = set()
    for tree in trees(text):
        for stmt in tree.body:  # module-level statements ONLY — no nested branches
            for name in _bound_names(stmt):
                if name not in seen:
                    seen.add(name)
                    continue
                # A re-binding: shadow unless it reads the prior value
                # (accumulation) or carries an explicit opt-out.
                if _value_reads_name(stmt, name) or _suppressed(stmt, lines):
                    continue
                hits.add(stmt.lineno)
    return sorted(hits)


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "module-level name re-assigned at top level — the second binding "
        "silently SHADOWS the first, so an edit to the first copy is "
        "discarded. Delete the duplicate (or rename it if the two are meant "
        "to differ), or annotate a deliberate re-binding "
        f"`# {OPT_OUT}: <reason>`.",
    )


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
