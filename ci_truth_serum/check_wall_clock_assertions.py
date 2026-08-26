#!/usr/bin/env python3
"""Ban a test assertion that compares a wall-clock duration against a number.

A shared CI runner gives a test no promise about when it runs — a spawn, a
page fault, or a descheduled thread routinely costs hundreds of milliseconds
that belong to the machine's load, not the code under test. An assertion on
the resulting duration measures the runner, so it flakes in both directions:
``assert elapsed < N`` claims the code is fast (assert the bound firing's own
observable instead — an exit status, a marker file); ``assert elapsed >= N``
claims the code waited (install a recording ``sleep`` stub and assert the
seconds it was asked for).

A VIOLATION is an ``assert`` whose test compares a wall-clock DELTA against a
numeric literal. A delta is a subtraction involving ``time.monotonic()``,
``time.time()``, ``time.perf_counter()`` or their ``_ns`` forms in Python, or
``Date.now()``/``performance.now()`` in JavaScript/TypeScript — written
inline, reached through a local bound to one earlier in the same function, or
returned by a helper the same module defines. NOT flagged: a deadline poll
(``while time.monotonic() < deadline``), and a comparison against a
non-literal (the subject's own budget can't be moved by runner load).

Both halves — Python (stdlib ``ast``) and JavaScript/TypeScript (``_js_ast``,
tree-sitter) — read the real grammar, never text: a clock mention inside a
string or a comment is not a reading, and an opt-out spelled inside one must
not disarm the check that reads it.

A repo-specific helper that multiplies a hard-coded bound for a slow runner
leg (``scale_timeout(20)``) is still a hard-coded bound — name it with
``--scaler NAME`` (repeatable) so its result counts as a numeric literal too.
None is assumed by default.

Exempt with a same-line or preceding-line ``# allow-wall-clock: <reason>``
(``//`` in JavaScript/TypeScript). Reason required.

Invoked by pre-commit with the staged test files, Python or JavaScript/TypeScript.
"""

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import iter_nodes  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _js_ast import is_js_source, parse  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import annotated_near, is_python_source, is_test_path, run_file_cli  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _py_ast import lines, trees  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-wall-clock"

MESSAGE = (
    "compares a wall-clock duration against a number — a loaded shared "
    "runner inflates the delta, so this measures the runner, not the code. "
    "Assert the observable of the bound firing instead, or annotate "
    f"`{OPT_OUT}: <reason>`"
)

# `time.<name>()` readings whose difference is a wall-clock duration. Every one
# advances with the machine's clock rather than with this process's work, so a
# descheduled thread inflates the delta.
_CLOCKS = frozenset(
    {
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "process_time",
        "time",
        "time_ns",
    }
)

# Builtins that return a number derived from their argument, so a duration
# passed in is still a duration coming out.
_DURATION_PRESERVING = frozenset({"abs", "float", "int", "round"})

# Where a helper's return value carries a duration: `None` for a scalar
# `return elapsed`, an int for that position of a returned tuple.
_ReturnMap = dict[str, frozenset[int | None]]


# ── Python ───────────────────────────────────────────────────────────────
def _is_clock_call(node: ast.AST) -> bool:
    """True when NODE is a `time.<clock>()` reading."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _CLOCKS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "time"
    )


def _callee_name(func: ast.expr) -> str | None:
    """The bare name a call's callee ends in, for `f()` and `mod.f()` alike."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _returned_positions(value: ast.expr, returns: _ReturnMap) -> frozenset[int | None]:
    """Where a duration sits in VALUE, when VALUE calls a duration-returning helper."""
    if not isinstance(value, ast.Call):
        return frozenset()
    return returns.get(_callee_name(value.func) or "", frozenset())


def _mentions_clock(node: ast.expr, names: set[str]) -> bool:
    """True when NODE reads a clock, or reads a name already bound to one."""
    for child in ast.walk(node):
        if _is_clock_call(child):
            return True
        if isinstance(child, ast.Name) and child.id in names:
            return True
    return False


def _clock_names(
    scope: ast.AST, inherited: frozenset[str], returns: _ReturnMap
) -> set[str]:
    """Names bound to a clock reading, or to a duration derived from one, in
    SCOPE. Walked in source order and grown as it goes, so `elapsed =
    time.monotonic() - start` is recognised once `start` is known to be a
    reading. Resolved PER FUNCTION, seeded by the module-level bindings — a
    one-letter name like `r` is reused by nearly every test in a module, and a
    module-wide set would let one function's duration make every other
    function's same-named local a duration too."""
    names = set(inherited)
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        positions = _returned_positions(value, returns)
        if isinstance(target, ast.Name):
            if _mentions_clock(value, names) or None in positions:
                names.add(target.id)
        elif isinstance(target, ast.Tuple):
            for index, element in enumerate(target.elts):
                if isinstance(element, ast.Name) and index in positions:
                    names.add(element.id)
    return names


def _own_returns(func: ast.AST) -> list[ast.Return]:
    """The `return` statements FUNC itself runs, skipping a nested def's — a
    closure returning its own duration must not make its enclosing function a
    duration-returning helper too."""
    found: list[ast.Return] = []
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return):
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _duration_returns(tree: ast.Module) -> _ReturnMap:
    """{helper name: where its return value is a duration}, for this module's
    defs. `return rc, time.monotonic() - start` carries a duration across a
    function boundary; grown to a fixed point, so a helper returning another
    helper's duration counts too. A helper imported from elsewhere is not
    read: only what this module defines is visible."""
    inherited = _module_level_names(tree, {})
    candidates = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        values = [r.value for r in _own_returns(node) if r.value is not None]
        if values:
            candidates.append((node, values))

    returns: _ReturnMap = {}
    grew = True
    while grew:
        grew = False
        for func, values in candidates:
            names = _clock_names(func, inherited, returns)
            found: set[int | None] = set(returns.get(func.name, frozenset()))
            for value in values:
                if isinstance(value, ast.Tuple):
                    found.update(
                        index
                        for index, element in enumerate(value.elts)
                        if _is_duration(element, names, returns)
                    )
                elif _is_duration(value, names, returns):
                    found.add(None)
            if found != set(returns.get(func.name, frozenset())):
                returns[func.name] = frozenset(found)
                grew = True
    return returns


def _is_duration(node: ast.expr, names: set[str], returns: _ReturnMap) -> bool:
    """True when NODE is a wall-clock DURATION rather than a point in time. A
    bare reading is NOT a duration: `time.monotonic() < deadline` is a poll,
    which this check steers toward rather than away from."""
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Sub) and _mentions_clock(node, names):
            return True
        return _is_duration(node.left, names, returns) or _is_duration(
            node.right, names, returns
        )
    if isinstance(node, ast.Call):
        if None in _returned_positions(node, returns):
            return True
        callee = node.func
        if (
            node.args
            and isinstance(callee, ast.Name)
            and callee.id in _DURATION_PRESERVING
        ):
            return any(_is_duration(arg, names, returns) for arg in node.args)
    return False


def _is_number(node: ast.expr, scalers: frozenset[str]) -> bool:
    """True when NODE is a numeric literal, a negated one, or a SCALERS call
    over one — a repo's `scale_timeout(20)` multiplies a hard-coded bound for
    a slower runner leg, and the product is as hard-coded as its argument."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_number(node.operand, scalers)
    if isinstance(node, ast.Call) and _callee_name(node.func) in scalers:
        return bool(node.args) and all(_is_number(a, scalers) for a in node.args)
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float))


def _offends(
    node: ast.Assert, names: set[str], returns: _ReturnMap, scalers: frozenset[str]
) -> bool:
    """Whether this assert compares a wall-clock duration against a number."""
    for compare in ast.walk(node.test):
        if not isinstance(compare, ast.Compare):
            continue
        sides = [compare.left, *compare.comparators]
        if any(_is_duration(s, names, returns) for s in sides) and any(
            _is_number(s, scalers) for s in sides
        ):
            return True
    return False


def _module_level_names(tree: ast.Module, returns: _ReturnMap) -> frozenset[str]:
    """Clock-derived names bound at module level, which every function inherits."""
    module_only = ast.Module(
        body=[n for n in tree.body if isinstance(n, ast.Assign)], type_ignores=[]
    )
    return frozenset(_clock_names(module_only, frozenset(), returns))


def _scopes(
    tree: ast.Module, returns: _ReturnMap
) -> list[tuple[ast.AST, frozenset[str]]]:
    """Each function body paired with the clock-derived names it inherits,
    plus the module itself for asserts written outside any function."""
    inherited = _module_level_names(tree, returns)
    outside = ast.Module(
        body=[
            n
            for n in tree.body
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ],
        type_ignores=[],
    )
    scopes: list[tuple[ast.AST, frozenset[str]]] = [(outside, inherited)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((node, inherited))
    return scopes


def _py_violations(source: str, scalers: frozenset[str]) -> list[int]:
    """1-based lines in SOURCE of every unexempted wall-clock assertion."""
    physical = lines(source)
    hits: set[int] = set()
    for tree in trees(source):
        returns = _duration_returns(tree)
        for scope, inherited in _scopes(tree, returns):
            names = _clock_names(scope, inherited, returns)
            for node in ast.walk(scope):
                if (
                    isinstance(node, ast.Assert)
                    and _offends(node, names, returns, scalers)
                    and not annotated_near(
                        physical, node.lineno, OPT_OUT, span_end=node.end_lineno
                    )
                ):
                    hits.add(node.lineno)
    return sorted(h for h in hits if h <= len(physical))


# ── JavaScript / TypeScript ─────────────────────────────────────────────
_JS_CLOCKS = frozenset({"Date.now", "performance.now"})
_JS_COMPARISONS = frozenset({"<", "<=", ">", ">=", "===", "==", "!==", "!="})


def _js_callee(node) -> str | None:
    """The dotted callee a call_expression invokes, or None for anything else."""
    if node.type != "call_expression":
        return None
    function = node.child_by_field_name("function")
    return function.text.decode("utf-8", "replace") if function is not None else None


def _js_is_clock_call(node) -> bool:
    """True when NODE is a `Date.now()` / `performance.now()` reading."""
    return _js_callee(node) in _JS_CLOCKS


def _js_mentions_clock(node, names: set[str]) -> bool:
    """True when NODE reads a clock, or a name already bound to one."""
    for descendant in iter_nodes(node, "call_expression", "identifier"):
        if _js_is_clock_call(descendant):
            return True
        if (
            descendant.type == "identifier"
            and descendant.text.decode("utf-8", "replace") in names
        ):
            return True
    return False


_JS_SCOPE_TYPES = frozenset(
    {
        "arrow_function",
        "function_declaration",
        "function_expression",
        "generator_function",
        "generator_function_declaration",
        "method_definition",
    }
)


def _js_scope_body(scope):
    """Every descendant of SCOPE (exclusive), pre-order, stopping at — but
    still yielding — a nested function's own boundary node rather than
    descending into it. This is what keeps one function's locals from
    leaking into a SIBLING function: each scope's own declarators and
    assert calls are read from here, never from a nested or enclosing one."""
    found = []
    for child in scope.children:
        found.append(child)
        if child.type not in _JS_SCOPE_TYPES:
            found.extend(_js_scope_body(child))
    return found


def _js_names_in(scope, inherited: frozenset[str]) -> set[str]:
    """Names bound to a wall-clock reading, or to a duration derived from one,
    in SCOPE's own body — grown in source order, seeded by INHERITED."""
    names = set(inherited)
    for node in _js_scope_body(scope):
        if node.type != "variable_declarator":
            continue
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is None or value is None or name.type != "identifier":
            continue
        if _js_mentions_clock(value, names):
            names.add(name.text.decode("utf-8", "replace"))
    return names


def _js_scopes(root) -> list[tuple[object, frozenset[str]]]:
    """Each function's own subtree paired with the clock-derived names it
    inherits from top-level bindings, plus the program itself for asserts
    written outside any function — the JS analogue of the Python arm's
    `_scopes`, so a one-letter name in one test does not make a same-named
    local in an unrelated test a duration too. Every entry is seeded by the
    SAME top-level names, matching the Python arm, which inherits only
    module-level bindings and never an enclosing function's locals."""
    inherited = frozenset(_js_names_in(root, frozenset()))
    scopes: list[tuple[object, frozenset[str]]] = [(root, inherited)]
    for func in iter_nodes(root, *_JS_SCOPE_TYPES):
        scopes.append((func, inherited))
    return scopes


def _js_is_duration(node, names: set[str]) -> bool:
    """True when NODE is a wall-clock DURATION rather than a point in time. A
    bare reading is not a duration — a poll's own comparison stays silent."""
    if node.type == "identifier":
        return node.text.decode("utf-8", "replace") in names
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        if (
            operator is not None
            and operator.text == b"-"
            and _js_mentions_clock(node, names)
        ):
            return True
        left, right = (
            node.child_by_field_name("left"),
            node.child_by_field_name("right"),
        )
        return (left is not None and _js_is_duration(left, names)) or (
            right is not None and _js_is_duration(right, names)
        )
    return False


def _js_is_number(node) -> bool:
    """True when NODE is a numeric literal, or its unary negation."""
    if node.type == "unary_expression":
        operator = node.child_by_field_name("operator")
        argument = node.child_by_field_name("argument")
        return (
            operator is not None
            and operator.text
            in (
                b"-",
                b"+",
            )
            and argument is not None
            and _js_is_number(argument)
        )
    return node.type == "number"


def _js_offends(node, names: set[str]) -> bool:
    """Whether a binary_expression NODE compares a wall-clock duration against
    a numeric literal."""
    operator = node.child_by_field_name("operator")
    if (
        operator is None
        or operator.text.decode("utf-8", "replace") not in _JS_COMPARISONS
    ):
        return False
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    if left is None or right is None:
        return False
    sides = (left, right)
    return any(_js_is_duration(s, names) for s in sides) and any(
        _js_is_number(s) for s in sides
    )


def _js_violations(source: str, path: str) -> list[int]:
    """1-based lines in SOURCE of every unexempted wall-clock assertion in one
    JavaScript/TypeScript test file. A hit is a comparison inside a call
    whose callee names `assert` — a bounded poll's own comparison
    (`if (Date.now() - start > timeoutMs)`) reaches no assert call."""
    root = parse(source, path)
    physical = source.split("\n")
    hits: set[int] = set()
    for scope, inherited in _js_scopes(root):
        names = _js_names_in(scope, inherited)
        for call in (n for n in _js_scope_body(scope) if n.type == "call_expression"):
            callee = _js_callee(call)
            if callee is None or "assert" not in callee:
                continue
            for compare in iter_nodes(call, "binary_expression"):
                if _js_offends(compare, names) and not annotated_near(
                    physical,
                    call.start_point[0] + 1,
                    OPT_OUT,
                    span_end=call.end_point[0] + 1,
                ):
                    hits.add(call.start_point[0] + 1)
    return sorted(h for h in hits if h <= len(physical))


def violations(
    text: str, path: str, scalers: frozenset[str] = frozenset()
) -> list[int]:
    """1-based lines in TEXT with an unexempted wall-clock assertion. PATH
    picks the language; a path of neither has none."""
    if is_js_source(path):
        return _js_violations(text, path)
    if is_python_source(path):
        return _py_violations(text, scalers)
    return []


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scaler",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "a helper that multiplies a hard-coded bound for a slower runner "
            "leg (e.g. scale_timeout); repeatable. None by default."
        ),
    )
    parser.add_argument("paths", nargs="*", metavar="FILE")
    args = parser.parse_args(argv)
    scalers = frozenset(args.scaler)

    status = 0
    for arg in args.paths:
        if not is_test_path(arg):
            continue
        path = Path(arg)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno in violations(text, arg, scalers):
            print(f"{arg}:{lineno}: {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
