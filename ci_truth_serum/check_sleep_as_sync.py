#!/usr/bin/env python3
"""Ban a fixed `sleep` used to synchronise with an event before an assertion.

WHY: a `time.sleep(N)` call before an assertion bets that the event lands
inside N seconds. It loses SILENTLY — a sleep long enough to hide the bug
still produces a green run. Poll the CONDITION with a deadline instead of
guessing a duration.

A VIOLATION is a bare `sleep(...)` statement — `time.sleep`, or any bare
`sleep` — followed LATER IN THE SAME BLOCK by an assertion: `assert`,
`pytest.raises`, or a call whose name starts with `assert_` (a hand-rolled
polling helper is exactly this shape). A sleep with no assertion after it is
pacing, not synchronisation, and is not flagged.

NOT flagged:
  - a sleep inside a `while` / `for` / `async for` body — the bounded poll
    this rule steers callers TOWARD;
  - a sleep whose interval is neither a numeric literal nor a module-level
    numeric constant (`scale_timeout(2)`, a parameter) — the caller who chose
    the value is where the question lives, not this call site. A module-level
    numeric constant (`_SETTLE = 0.6`) IS flagged: it is the same fixed bet
    under another name;
  - a fake-timer call (`freeze_time`, `useFakeTimers`, `clock.tick`) — out of
    scope for this Python-AST-only check.

BLIND SPOT: each module is read as one isolated tree, so a sleep in one
function and an assertion inside a helper it calls are invisible to this
check — only a sleep and an assertion in the same literal statement list are
matched.

Opt out with `# allow-sleep: <reason>` on the sleep's own line, on the line
above it, or anywhere inside a multi-line sleep call — the reason is
required.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    run_line_checks,
)
from _py_ast import lines as py_lines  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-sleep"

MESSAGE = (
    "a fixed sleep is synchronising this assertion — the scheduler settles "
    "the bet, so it flakes under load and passes silently when the event "
    "never arrives. Poll the condition with a deadline instead: a "
    '`wait_until(cond, ...)`-shaped helper for "X must happen", an '
    '`assert_stays(cond, ...)`-shaped one for "X must NOT happen". A test '
    "whose subject IS the timeout takes "
    f"`# {OPT_OUT}: <reason>` on the sleep's own line or the line above it."
)

# Fields that hold a statement list, so each is a BLOCK whose ordering decides
# whether an assertion comes after a sleep.
_BLOCK_FIELDS = ("body", "orelse", "finalbody")


def _numeric_constants(tree: ast.Module) -> set[str]:
    """Module-level names bound to a number, so `sleep(_SETTLE)` reads as the
    fixed interval it is rather than as a caller-supplied one."""
    names = set()
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if isinstance(target, ast.Name) and _is_number(statement.value):
            names.add(target.id)
    return names


def _is_number(node: ast.expr) -> bool:
    """True when NODE is a numeric literal."""
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float))


def _is_sleep_call(node: ast.expr, constants: set[str]) -> bool:
    """True when NODE is a `sleep(...)` call with a FIXED number of seconds.

    A non-literal interval (`time.sleep(interval)`, `time.sleep(scale_timeout(2))`)
    belongs to a helper's own parameter or to a runner-scaled bound, and the
    caller who chose the value is where the question lives."""
    if not isinstance(node, ast.Call) or not node.args:
        return False
    callee = node.func
    name = (
        callee.attr
        if isinstance(callee, ast.Attribute)
        else (callee.id if isinstance(callee, ast.Name) else None)
    )
    if name != "sleep":
        return False
    seconds = node.args[0]
    if isinstance(seconds, ast.Name):
        return seconds.id in constants
    return _is_number(seconds)


def _asserts(statements: list[ast.stmt]) -> bool:
    """True when any of STATEMENTS contains an assertion.

    `pytest.raises` and an `assert_*` helper both count: each asserts on the
    outcome exactly as an `assert` does, and each loses the same race."""
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.Assert):
                return True
            if isinstance(node, (ast.Name, ast.Attribute)):
                label = node.id if isinstance(node, ast.Name) else node.attr
                if label == "raises" or label.startswith("assert"):
                    return True
    return False


def _sleep_hits_in_block(
    statements: list[ast.stmt], constants: set[str]
) -> list[tuple[int, int]]:
    """(start, end) 1-based lines of every sleep statement in this block that an
    assertion follows."""
    hits = []
    for index, statement in enumerate(statements):
        if (
            isinstance(statement, ast.Expr)
            and _is_sleep_call(statement.value, constants)
            and _asserts(statements[index + 1 :])
        ):
            hits.append((statement.lineno, statement.end_lineno or statement.lineno))
    return hits


def violations(text: str) -> list[int]:
    """1-based lines of every unexempted sleep-before-assertion in one module."""
    tree = ast.parse(text)
    physical = py_lines(text)
    constants = _numeric_constants(tree)
    hits: dict[int, int] = {}

    for node in ast.walk(tree):
        # A loop body is the bounded poll, so its sleep is the mechanism
        # rather than the defect. Skipping the node's own blocks (not its
        # whole subtree) keeps a sleep in a nested non-loop block reportable.
        if isinstance(node, (ast.While, ast.For, ast.AsyncFor)):
            continue
        for field in _BLOCK_FIELDS:
            block = getattr(node, field, None)
            if isinstance(block, list):
                for start, end in _sleep_hits_in_block(block, constants):
                    hits[start] = end

    return sorted(
        start
        for start, end in hits.items()
        if not annotated_near(physical, start, OPT_OUT, span_end=end)
    )


def main(argv: list[str]) -> int:
    return run_line_checks(argv, violations, MESSAGE)


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
