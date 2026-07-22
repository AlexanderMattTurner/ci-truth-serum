#!/usr/bin/env python3
"""Fail if any Python file passes a regex literal with unnamed capture groups to a re.* call.

Named groups (?P<name>...) are required; non-capturing groups (?:...) are fine.
Skips f-strings and other non-literal patterns it can't statically evaluate.

Resolves how the file reaches ``re`` before matching call targets, so an aliased
import can't smuggle an unnamed group past the check: ``import re as x`` (``x.compile``)
and ``from re import compile`` (a bare ``compile(...)``) are inspected too.
"""

import ast
import re
import sys
from pathlib import Path

_RE_FUNCS = frozenset(
    {
        "compile",
        "match",
        "search",
        "fullmatch",
        "findall",
        "finditer",
        "sub",
        "subn",
        "split",
    }
)


def _has_unnamed_group(pattern: str) -> bool:
    try:
        compiled = re.compile(pattern)
        return compiled.groups > len(compiled.groupindex)
    except re.error:
        return False


def _literal_str(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _re_bindings(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Resolve how the module reaches ``re`` so an aliased import can't slip past.

    Returns (module_names, func_names): ``module_names`` are local names bound to
    the ``re`` module (``import re`` → ``re``; ``import re as x`` → ``x``), and
    ``func_names`` maps a locally-bound name to the ``re`` function it names
    (``from re import compile`` → ``compile: compile``; ``... import search as s``
    → ``s: search``). Bare ``re`` is always treated as the module so a file that
    calls ``re.compile`` without a visible ``import re`` is still checked."""
    module_names = {"re"}
    func_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "re":
                    module_names.add(alias.asname or "re")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "re" and node.level == 0:
                for alias in node.names:
                    func_names[alias.asname or alias.name] = alias.name
    return module_names, func_names


def _re_call_target(
    func: ast.expr, module_names: set[str], func_names: dict[str, str]
) -> str | None:
    """The ``re`` function a call is invoking, or None if the call isn't one.

    Handles both the attribute form (``re.search`` / an alias ``x.search``, where
    ``x`` is bound to the module) and the bare-name form (``compile`` imported via
    ``from re import compile``)."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in module_names and func.attr in _RE_FUNCS:
            return func.attr
    elif isinstance(func, ast.Name):
        mapped = func_names.get(func.id)
        if mapped in _RE_FUNCS:
            return mapped
    return None


def check_file(path: Path) -> list[tuple[int, str]]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"{path}: cannot read file — {e}", file=sys.stderr)
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    module_names, func_names = _re_bindings(tree)
    errors: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _re_call_target(node.func, module_names, func_names) is None:
            continue
        if not node.args:
            continue
        pattern = _literal_str(node.args[0])
        if pattern and _has_unnamed_group(pattern):
            errors.append((node.lineno, pattern))
    return errors


def main() -> int:
    rc = 0
    for arg in sys.argv[1:]:
        path = Path(arg)
        for lineno, pattern in check_file(path):
            print(
                f"{path}:{lineno}: unnamed capture group — "
                f"use (?P<name>...) or (?:...): {pattern!r}"
            )
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
