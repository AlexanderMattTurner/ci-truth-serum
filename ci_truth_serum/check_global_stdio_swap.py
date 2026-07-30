#!/usr/bin/env python3
"""Ban swapping the process-global standard streams in source Python.

Reassigning ``sys.stdin`` / ``sys.stdout`` / ``sys.stderr`` (or wrapping a region
in ``contextlib.redirect_stdout`` / ``redirect_stderr``) to capture a function's
I/O is not thread-safe: the streams are process-global, so when the same code runs
concurrently — e.g. a request handler running under a ThreadingHTTPServer —
overlapping calls clobber each other's swap and a losing thread's output lands
in another thread's buffer. That is the silent "produced no output" failure mode.
The fix is to PARAMETERIZE the I/O (pass the input in, return the output) or bind
it per-thread, not to swap globals.

The source is read through Python's own grammar (``_py_ast``), so "is this stream
being ASSIGNED, or merely read?" is answered by whether it is an assignment
TARGET — not by whether it sits left of the first ``=`` on its line. That is what
keeps a read (``saved = sys.stdout``, ``print(x, file=sys.stdout)``) clean while
catching the swap however it is spelled: bound across lines, unpacked from a
tuple, bound by a ``with … as`` or a ``for``, or reached through ``setattr(sys,
"stdout", …)``. A stream named inside a string literal or a comment is text the
program builds, not a swap, and the grammar never offers it as a target at all.

Fires on an assignment to ``sys.stdin``/``sys.stdout``/``sys.stderr`` (however
qualified), on a ``setattr`` naming one, and on any use of
``redirect_stdout``/``redirect_stderr``. A test harness or a genuinely
single-threaded one-shot that must swap opts out with a trailing
``# allow-stdio-swap: <reason>`` on the reported line. Scope this hook (via
``files:``/``exclude:``) to the source dirs that run concurrently — tests
legitimately swap stdio.

Invoked by pre-commit with the staged Python files as arguments.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import annotated  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import run_line_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _py_ast import lines, name_of, trees  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-stdio-swap"

MESSAGE = (
    "swaps a process-global stdio stream (not thread-safe); "
    "parameterize the I/O or bind it per-thread, or annotate "
    f"`# {OPT_OUT}: <reason>`"
)

_STREAMS = frozenset({"stdin", "stdout", "stderr"})
# The context managers that swap a stream for the duration of a block — the same
# process-global mutation one `with` layer up.
_REDIRECTS = frozenset({"redirect_stdout", "redirect_stderr"})


def _is_stream(node: ast.AST) -> bool:
    """True when NODE names a global stream — ``sys.stdout`` however qualified
    (``mon.sys.stdin`` is the same object reached through another module)."""
    parts = (name_of(node) or "").split(".")
    return len(parts) >= 2 and parts[-2] == "sys" and parts[-1] in _STREAMS


def _targets(node: ast.AST) -> list[ast.AST]:
    """Every expression NODE binds a value to.

    Enumerated per statement kind rather than by looking for ``ast.Store``
    contexts, so the list stays readable and a reviewer can check it against the
    grammar: an assignment, an annotated or augmented one, a walrus, a loop
    variable, and a ``with … as``."""
    if isinstance(node, ast.Assign):
        bound = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        bound = [node.target]
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        bound = [node.target]
    elif isinstance(node, ast.withitem):
        bound = [node.optional_vars] if node.optional_vars is not None else []
    else:
        return []
    # A target may nest: `sys.stdin, buf = a, b` and `[*rest, sys.stdout] = xs`
    # both bind the stream inside a Tuple/List/Starred wrapper. The `Store`
    # context is what separates a bound name from one merely READ inside a target
    # (`registry[sys.stdout] = x` reads the stream to index with it).
    return [
        inner
        for target in bound
        for inner in ast.walk(target)
        if isinstance(inner, (ast.Name, ast.Attribute))
        and isinstance(inner.ctx, ast.Store)
    ]


def _is_setattr_swap(node: ast.AST) -> bool:
    """True when NODE is ``setattr(sys, "stdout", …)`` — the same rebinding, one
    layer of indirection away from the attribute assignment."""
    if not isinstance(node, ast.Call) or name_of(node.func) != "setattr":
        return False
    if len(node.args) < 2:
        return False
    target = (name_of(node.args[0]) or "").split(".")[-1]
    attribute = node.args[1]
    return (
        target == "sys"
        and isinstance(attribute, ast.Constant)
        and attribute.value in _STREAMS
    )


def _swap_lines(tree: ast.Module) -> list[int]:
    """Every line in TREE that swaps a global stream."""
    hits: list[int] = []
    for node in ast.walk(tree):
        hits += [t.lineno for t in _targets(node) if _is_stream(t)]
        if _is_setattr_swap(node):
            hits.append(node.lineno)
        elif isinstance(node, (ast.Name, ast.Attribute)) and (
            (name_of(node) or "").split(".")[-1] in _REDIRECTS
        ):
            hits.append(node.lineno)
        elif isinstance(node, ast.alias) and node.name in _REDIRECTS:
            hits.append(node.lineno)  # `from contextlib import redirect_stdout`
    return hits


def violations(text: str) -> list[int]:
    """1-based line numbers in TEXT that swap a global stream without an opt-out."""
    physical = lines(text)
    hits = {line for tree in trees(text) for line in _swap_lines(tree)}
    return sorted(
        lineno
        for lineno in hits
        if lineno <= len(physical) and not annotated(physical[lineno - 1], OPT_OUT)
    )


def main(argv: list[str]) -> int:
    return run_line_checks(argv, violations, MESSAGE)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
