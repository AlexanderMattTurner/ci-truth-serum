#!/usr/bin/env python3
"""Flag a module that mutates module-level state at runtime and declares no reset.

WHY: a module-level binding written from inside a function outlives the call
that wrote it. A test runner that reuses one process across many tests (a
pytest-xdist worker, a long-lived CI job) imports the module once, so one
test's write is the next test's answer.

A VIOLATION is a module-level `NAME = ...` binding where BOTH hold:
  - some FUNCTION body writes NAME — `global NAME`, `NAME[k] = v`,
    `del NAME[k]`, `NAME.attr = v`, `NAME += v`, or a call to a mutator
    method (`append`, `update`, `clear`, …) on it; and
  - the module declares no top-level reset function, named by `--reset-name`
    (default `_reset_process_state`) — a per-repo convention this check does
    not invent, only looks for.

INVARIANT — only a write from inside a FUNCTION counts. A module-level loop
that fills a dict runs once at import, so the result is a mutable CONSTANT,
not a latch a reset would have anything to undo.

BLIND SPOTS — where the AST cannot decide, this check prefers a FALSE
NEGATIVE over a false alarm:
  - a name the writing function also binds locally is shadowed, not module
    state;
  - a write through an alias (`ref = _cache; ref["k"] = v`) is not followed;
  - a name written only by an IMPORTER is attributed to no module.

Opt out with `# allow-unreset-state: <reason>` on the binding's own line or
the line above it — the reason is required — matching the placement rule
every other check in this pack shares.
"""

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    run_source_checks,
)
from _py_ast import lines as py_lines  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-unreset-state"

DEFAULT_RESET_NAME = "_reset_process_state"

# Methods that write their receiver. Every one of these on a module-level
# binding is a runtime write to state the process keeps.
_MUTATORS = frozenset(
    {
        "add",
        "append",
        "appendleft",
        "clear",
        "difference_update",
        "discard",
        "extend",
        "extendleft",
        "insert",
        "intersection_update",
        "move_to_end",
        "pop",
        "popitem",
        "popleft",
        "remove",
        "rotate",
        "setdefault",
        "sort",
        "symmetric_difference_update",
        "update",
    }
)

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def message(reset_name: str) -> str:
    """The finding text for a module that declares no RESET_NAME."""
    return (
        "module-level state written at runtime, in a module that declares no "
        f"{reset_name}() — nothing can drop it between runs that must not see "
        f"each other. Declare `def {reset_name}() -> None:` and reset the "
        "state there, replace the state with an `lru_cache`-wrapped function "
        f"(found by its own `cache_clear`), or annotate a justified case with "
        f"`# {OPT_OUT}: <reason>`."
    )


def _binding_names(node: ast.AST) -> set[str]:
    """Every plain name a binding construct binds, ignoring subscripts and
    attributes."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            names.add(child.id)
    return names


def module_bindings(tree: ast.Module) -> dict[str, int]:
    """Each module-level name assigned a value, mapped to its first line.

    Descends through module-level `if`/`try`/`for`/`with` — a binding under a
    platform check is still module state — but never into a function or class
    body.
    """
    bindings: dict[str, int] = {}

    def visit(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, _SCOPES):
                continue
            if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        bindings.setdefault(target.id, stmt.lineno)
                continue
            for field in ("body", "orelse", "finalbody"):
                nested = getattr(stmt, field, None)
                if isinstance(nested, list):
                    visit([s for s in nested if isinstance(s, ast.stmt)])
            # ExceptHandler is an `excepthandler`, not a `stmt`, so its body
            # has to be reached explicitly or a binding made only in an
            # `except` is missed.
            for handler in getattr(stmt, "handlers", []):
                visit(handler.body)

    visit(tree.body)
    return bindings


def _local_names(func: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    """Names FUNC binds itself, so a write to one of them never reaches module
    scope. A name FUNC declares `global` is excluded: that write IS the
    module's state."""
    local = {arg.arg for arg in ast.walk(func) if isinstance(arg, ast.arg)}
    declared_global: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Global):
            declared_global.update(node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            local |= _binding_names(node)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            local |= _binding_names(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    local |= _binding_names(item.optional_vars)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            local |= {
                (alias.asname or alias.name).split(".")[0] for alias in node.names
            }
        elif (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node is not func
        ):
            local.add(node.name)
    return local - declared_global


def _written_in(func: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    """Module-scope names FUNC writes, excluding the ones it binds locally."""
    written: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Global):
            written.update(node.names)
        elif isinstance(node, ast.Call):
            callee = node.func
            if (
                isinstance(callee, ast.Attribute)
                and callee.attr in _MUTATORS
                and isinstance(callee.value, ast.Name)
            ):
                written.add(callee.value.id)
        elif isinstance(node, (ast.Subscript, ast.Attribute)) and isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            if isinstance(node.value, ast.Name):
                written.add(node.value.id)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            written.add(node.target.id)
    return written - _local_names(func)


def _outermost_functions(
    node: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda]:
    """Every function not nested inside another one.

    A nested function is left to its parent's walk, which subtracts the
    PARENT's locals too. Visiting it alone would read a closure write to one
    of those locals as a write to module scope.
    """
    found: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            found.append(child)
        else:
            found.extend(_outermost_functions(child))
    return found


def runtime_writes(tree: ast.Module) -> set[str]:
    """Every module-scope name some function body writes while the process runs."""
    written: set[str] = set()
    for func in _outermost_functions(tree):
        written |= _written_in(func)
    return written


def declares_reset(tree: ast.Module, reset_name: str = DEFAULT_RESET_NAME) -> bool:
    """Whether the module defines a top-level function named RESET_NAME."""
    return any(
        isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
        and stmt.name == reset_name
        for stmt in tree.body
    )


def violations(text: str, reset_name: str = DEFAULT_RESET_NAME) -> list[int]:
    """1-based lines of every unexempted module-level binding written at
    runtime by a module that declares no RESET_NAME."""
    tree = ast.parse(text)
    if declares_reset(tree, reset_name):
        return []
    physical = py_lines(text)
    written = runtime_writes(tree)
    return sorted(
        lineno
        for name, lineno in module_bindings(tree).items()
        if name in written and not annotated_near(physical, lineno, OPT_OUT)
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset-name",
        default=DEFAULT_RESET_NAME,
        metavar="NAME",
        help="the module-level function whose presence declares the reset "
        "(default: %(default)s)",
    )
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    if not args.paths:
        print(
            "check_unreset_module_state: no files to scan. This check reads "
            "only the paths you give it, so an empty run would report a "
            "clean pass over nothing.",
            file=sys.stderr,
        )
        return 2
    return run_source_checks(
        args.paths,
        lambda text, _path: violations(text, args.reset_name),
        message(args.reset_name),
    )


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
