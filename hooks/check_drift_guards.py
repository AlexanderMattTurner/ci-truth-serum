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

Python tests carry the ``@pytest.mark.drift_guard`` marker; but copies-agree tests
also live in JavaScript/TypeScript (``*.test.mjs``) and shell suites, which have no
such decorator. For those a SIBLING phrase pass runs: any line expressing
drift-guard intent must carry a same-line or immediately-preceding
``drift-guard-ok: <why a true SSOT is infeasible>`` annotation, or it is flagged.
Same heuristic, different surface — so the backstop is not silently Python-only.

Invoked by pre-commit with the staged Python / JS / TS / shell files as arguments.
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

# The non-Python opt-out: a comment `drift-guard-ok: <reason>` with a non-empty
# reason. (The bare token `drift-guard` inside it also matches _GUARD_RE, but the
# annotation check runs first, so an annotation line never flags itself.)
_ALLOW_MARKER = re.compile(r"drift-guard-ok:\s*\S", re.IGNORECASE)


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


def text_violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, matched phrase) for every line of TEXT that expresses
    drift-guard intent without a reason-bearing ``drift-guard-ok:`` annotation on
    that line or the one immediately above.

    The non-AST sibling of ``violations()``: JS/TS/shell tests carry no
    ``@pytest.mark``, so intent is detected by phrase and excused inline instead."""
    lines = text.splitlines()
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        match = _GUARD_RE.search(line)
        if not match:
            continue
        if _ALLOW_MARKER.search(line):
            continue
        if i > 0 and _ALLOW_MARKER.search(lines[i - 1]):
            continue
        hits.append((i + 1, match.group(0)))
    return hits


def main(argv: list[str]) -> int:
    status = 0
    for path in argv:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.endswith(".py"):
            for lineno, name in violations(source):
                print(
                    f"{path}:{lineno}: drift guard {name!r} lacks a justification — "
                    "prefer removing the duplication (make one source authoritative), "
                    f'or add @pytest.mark.{_MARKER}("why a true SSOT is infeasible").',
                    file=sys.stderr,
                )
                status = 1
            continue
        for lineno, phrase in text_violations(source):
            print(
                f"{path}:{lineno}: drift-guard intent ({phrase!r}) lacks a "
                "justification — prefer removing the duplication (make one source "
                "authoritative), or annotate "
                "`drift-guard-ok: <why a true SSOT is infeasible>`.",
                file=sys.stderr,
            )
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
