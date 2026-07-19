#!/usr/bin/env python3
"""Require a justification marker on drift-guard tests.

A drift guard — a test that asserts two duplicated sources agree — is a design
smell: with a true single source of truth nothing can drift, so no such test is
needed. The guard is legitimate only when an SSOT is genuinely infeasible (an
external value you don't control, a hard cross-language or cross-process
boundary), and that judgement belongs in the open. Any test whose name or
docstring uses drift-guard intent ("drift guard", "can't drift", "must stay in
sync", ...) MUST carry

    @pytest.mark.drift_guard("why a true SSOT is infeasible")

so review checks the stated reason, not the mere existence of the guard.
Detection is by convention, not proof — a guard worded to dodge the phrasing
slips through, like the other heuristic lints in this pack.

Invoked by pre-commit with the staged Python files as arguments.
"""

import ast
import re
import sys
from pathlib import Path

# Phrases that express guard INTENT — the author is asserting two sources can't
# diverge — rather than merely mentioning the word "drift" (which a test of
# drift-detection tooling, e.g. test_main_check_mode_detects_drift, also does).
# Kept deliberately specific: broad words like "mirror"/"parity"/"matches" recur
# in unrelated tests, and bare "lockstep" often names a runtime mechanism — so
# the copies-agree phrasings ("in lockstep", "kept in sync") are required, not
# just the word.
_GUARD_PATTERNS = (
    r"drift[- ]guard",
    r"anti[- ]?drift",
    r"(?:can't|cannot|never|won't) (?:drift|diverge)",
    r"must (?:stay|remain) in sync",
    r"in lockstep",
    r"kept in (?:sync|step)",
)
_GUARD_RE = re.compile("|".join(_GUARD_PATTERNS), re.IGNORECASE)

_MARKER = "drift_guard"


def _is_drift_guard(name: str, docstring: str) -> bool:
    """A test reads as a drift guard if its name (underscores read as spaces) or
    its docstring uses guard-intent phrasing."""
    return bool(_GUARD_RE.search(name.replace("_", " ")) or _GUARD_RE.search(docstring))


def _justification(decorator: ast.expr) -> str | None:
    """The non-empty justification string of a @pytest.mark.drift_guard(...) call,
    or None if this decorator is not that marker / carries no string reason."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not (isinstance(func, ast.Attribute) and func.attr == _MARKER):
        return None
    if not decorator.args:
        return None
    arg = decorator.args[0]
    if (
        isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
        and arg.value.strip()
    ):
        return arg.value
    return None


def violations(source: str) -> list[tuple[int, str]]:
    """(1-based line, function name) for every test in SOURCE that reads as a
    drift guard but lacks a justified @pytest.mark.drift_guard marker. A file
    that does not parse as Python produces no findings (other tooling owns
    syntax errors)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        if not _is_drift_guard(node.name, ast.get_docstring(node) or ""):
            continue
        if any(_justification(dec) for dec in node.decorator_list):
            continue
        hits.append((node.lineno, node.name))
    return hits


def main(argv: list[str]) -> int:
    status = 0
    for path in argv:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, name in violations(source):
            print(
                f"{path}:{lineno}: drift guard {name!r} lacks a justification — "
                "prefer removing the duplication (make one source authoritative), "
                f'or add @pytest.mark.{_MARKER}("why a true SSOT is infeasible").',
                file=sys.stderr,
            )
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
