#!/usr/bin/env python3
"""Fail if any Python file passes a regex literal with unnamed capture groups to a re.* call.

Named groups (?P<name>...) are required; non-capturing groups (?:...) are fine.
Skips f-strings and other non-literal patterns it can't statically evaluate.

Resolves how the file reaches ``re`` before matching call targets, so an aliased
import can't smuggle an unnamed group past the check: ``import re as x`` (``x.compile``)
and ``from re import compile`` (a bare ``compile(...)``) are inspected too. That
resolver lives in ``_py_ast``, shared with every other lint that matches ``re.*``.
"""

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _py_ast import re_bindings, re_call_target  # noqa: E402,I001  # pylint: disable=wrong-import-position

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

    module_names, func_names = re_bindings(tree)
    errors: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if re_call_target(node.func, module_names, func_names, _RE_FUNCS) is None:
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
