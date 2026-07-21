#!/usr/bin/env python3
"""Require a justification marker on drift-guard tests.

A drift guard — a test that asserts two duplicated sources agree — is a design
smell: with a true single source of truth nothing can drift, so no such test is
needed. The guard is legitimate only when an SSOT is genuinely infeasible (an
external value you don't control, a hard cross-language or cross-process
boundary), and that judgement belongs in the open. A guard MUST carry

    @pytest.mark.drift_guard("why a true SSOT is infeasible")

so review checks the stated reason, not the mere existence of the guard.

A Python guard is detected two ways, because either alone is evadable:

  1. INTENT PHRASING — the name or docstring says what it is ("drift guard",
     "must stay in sync", ...). Honest, but a guard reworded to dodge the
     phrasing — calling itself an "SSOT-coverage contract" instead — slips
     straight through. That laundering is the whole failure mode this check
     exists to stop, so phrasing cannot be the only trigger.
  2. COPIES-AGREE STRUCTURE — the test READS an external source (a file/config)
     and asserts a COLLECTION equality where one side is a hand-maintained copy
     (an in-source collection literal, or an UPPER_CASE constant / its
     `.keys()`). That is the mechanical signature of "this hand-kept list must
     match the live config", and it does not care what the docstring calls the
     test, so relabeling can't hide it.

The structural trigger is deliberately NARROW to stay quiet on legitimate tests
(precision over recall — a noisy guard trains reviewers to ignore it). It fires
only on read-source + maintained-copy-vs-collection; it does NOT fire on the
sanctioned single-source form (read one config, assert code handles every entry
via membership/iteration), nor on an ordinary output-vs-expected unit assertion.
A structural hit that is a genuine non-guard clears with an explicit, reasoned
opt-out comment anywhere in the function body:

    # not-a-drift-guard: <why this collection equality is not two copies>

Copies-agree tests also live in JavaScript/TypeScript (``*.test.mjs``) and shell
suites, which carry no ``@pytest.mark``. For those a SIBLING phrase pass runs
(``text_violations``): any line expressing drift-guard intent must carry a
same-line or immediately-preceding ``drift-guard-ok: <why a true SSOT is
infeasible>`` annotation, or it is flagged. That non-Python surface is
phrase-only — it has the same dodge-the-phrasing weakness the structural trigger
closes for Python; a JS-side structural pass is the honest follow-up.

Honest limits, stated so this check is not itself laundered: detection is a
heuristic, not proof. A copies-agree comparison the AST can't see (a hand-rolled
element-by-element loop, two module constants compared with no file read, a value
fetched at runtime) still slips. The triggers close the common cases; they do not
make laundering impossible.

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

# The Python structural opt-out: `# not-a-drift-guard: <reason>` clears a
# STRUCTURAL hit (a genuine collection-equality unit test), with a non-empty
# reason so the escape is a stated judgement, not a silent mute.
_OPTOUT_RE = re.compile(r"#\s*not-a-drift-guard:\s*\S", re.IGNORECASE)

# Callables that construct/return a collection, and the collection-view methods.
_COLLECTION_CTORS = frozenset({"set", "frozenset", "sorted", "list", "tuple", "dict"})
_COLLECTION_METHODS = frozenset({"keys", "values", "items"})

# unittest asserts that compare two collections for equality.
_COLLECTION_ASSERTS = frozenset(
    {
        "assertEqual",
        "assertCountEqual",
        "assertSetEqual",
        "assertListEqual",
        "assertDictEqual",
    }
)

# Calls that read an external source into the test — the other half of the
# copies-agree signature (a *maintained copy* is only a drift guard when it is
# pinned against a *separate source*, typically a file/config read here).
_SOURCE_READS = frozenset(
    {"read_text", "read_bytes", "read", "load", "loads", "safe_load", "open"}
)


def _is_drift_guard(name: str, docstring: str) -> bool:
    """A test reads as a drift guard if its name (underscores read as spaces) or
    its docstring uses guard-intent phrasing."""
    return bool(_GUARD_RE.search(name.replace("_", " ")) or _GUARD_RE.search(docstring))


def _is_collection_shaped(node: ast.expr) -> bool:
    """True when NODE is or constructs a collection — a set/list/dict/tuple
    literal, a `set()/sorted()/list()/tuple()/dict()/frozenset()` call, or a
    `.keys()/.values()/.items()` call."""
    if isinstance(node, (ast.Set, ast.List, ast.Dict, ast.Tuple)):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _COLLECTION_CTORS:
            return True
        if isinstance(func, ast.Attribute) and func.attr in _COLLECTION_METHODS:
            return True
    return False


def _is_maintained_copy(node: ast.expr) -> bool:
    """True when NODE is a HAND-MAINTAINED collection: an in-source
    set/list/dict/tuple literal, an UPPER_CASE module constant (or its
    `.keys()/.values()/.items()`), or a `set()/sorted()/...` wrapping one of
    those. This is the side of a drift-guard equality that a human keeps in step
    with a separate source by hand — the thing that drifts."""
    if isinstance(node, (ast.Set, ast.List, ast.Dict, ast.Tuple)):
        return True
    if isinstance(node, ast.Name):
        return node.id.isupper()
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _COLLECTION_CTORS and node.args:
            return _is_maintained_copy(node.args[0])
        if isinstance(func, ast.Attribute) and func.attr in _COLLECTION_METHODS:
            return isinstance(func.value, ast.Name) and func.value.id.isupper()
    return False


def _reads_source(node: ast.AST) -> bool:
    """True when the function body reads an external source (a file/config load).
    Half of the structural signature — a maintained copy pinned against a
    separately-read source is the drift guard."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr in _SOURCE_READS:
                return True
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            if child.func.id == "open":
                return True
    return False


def _asserts_maintained_copy_equals(node: ast.AST) -> bool:
    """True when the body asserts a COLLECTION equality with a hand-maintained
    copy on one side — `assert MAINTAINED == other_collection`, or an
    `assertEqual/assertCountEqual/...` where one argument is a maintained copy.
    The maintained-copy requirement is what keeps this off ordinary
    output-vs-expected unit assertions."""
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Assert)
            and isinstance(child.test, ast.Compare)
            and len(child.test.ops) == 1
            and isinstance(child.test.ops[0], ast.Eq)
        ):
            left, right = child.test.left, child.test.comparators[0]
            if (
                _is_collection_shaped(left)
                and _is_collection_shaped(right)
                and (_is_maintained_copy(left) or _is_maintained_copy(right))
            ):
                return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in _COLLECTION_ASSERTS
            and len(child.args) >= 2
            and (
                _is_maintained_copy(child.args[0]) or _is_maintained_copy(child.args[1])
            )
        ):
            return True
    return False


def _is_structural_guard(node: ast.AST) -> bool:
    """The copies-agree structural signature: the test reads a separate source
    AND asserts a hand-maintained collection copy equals it."""
    return _reads_source(node) and _asserts_maintained_copy_equals(node)


def _has_optout(node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]) -> bool:
    """True when a `# not-a-drift-guard: <reason>` comment sits within the
    function's source span — the explicit escape for a genuine collection-equality
    unit test that the structural trigger would otherwise flag."""
    end = node.end_lineno or node.lineno
    return any(_OPTOUT_RE.search(line) for line in lines[node.lineno - 1 : end])


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
    drift guard — by intent PHRASING or copies-agree STRUCTURE — but lacks a
    justified @pytest.mark.drift_guard marker. A structural-only hit is cleared by
    a `# not-a-drift-guard:` opt-out; a phrasing hit is not (naming a test a guard
    is a self-declaration). A file that does not parse as Python produces no
    findings (other tooling owns syntax errors)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    lines = source.splitlines()
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        phrasing = _is_drift_guard(node.name, ast.get_docstring(node) or "")
        structural = _is_structural_guard(node)
        if not (phrasing or structural):
            continue
        if any(_justification(dec) for dec in node.decorator_list):
            continue
        if structural and not phrasing and _has_optout(node, lines):
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
                    f'add @pytest.mark.{_MARKER}("why a true SSOT is infeasible"), or — '
                    "for a genuine non-guard collection-equality — a "
                    "`# not-a-drift-guard: <reason>` comment.",
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
