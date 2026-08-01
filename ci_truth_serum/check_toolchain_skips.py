#!/usr/bin/env python3
"""Flag pytest skips gated on binary discovery with no CI environment guard.

`pytest.mark.skipif(shutil.which("node") is None, …)` reads as a harmless
local-dev convenience — but on a CI runner missing the tool it silently zeroes
the coverage of everything the test guards, and the suite stays green. The
skip must FAIL (not skip) in CI, e.g.::

    shutil.which("node") is None and not os.environ.get("CI")

The source is read through Python's own grammar (``_py_ast``): a skip is an
``ast.Call``, so its argument list ends where the grammar says it ends rather
than where a balanced-paren walk with its own quote state guesses, and the
condition examined is the ``skipif`` CONDITION rather than the whole call text —
so a ``reason="install node with shutil.which"`` string cannot read as binary
discovery, and a ``reason="…CI…"`` cannot pass as the guard that is missing.

Deliberately conservative (precision over recall): only conditions that
reference binary discovery (``shutil.which``, a bare ``which(…)`` call, or
``find_executable``) are examined, and any reference to a CI env guard
(``os.environ`` / ``os.getenv`` / a ``CI`` name) in the same condition passes.
Applies to ``pytest.mark.skipif(…)`` and ``pytest.importorskip(…)`` in Python
test files (`test_*.py` / `*_test.py` / files under a `tests/` dir); non-test
Python files are never scanned.

Opt out with `# toolchain-skip-ok: <reason>` on the call's first line or the
line above. Invoked by pre-commit with the staged Python files as arguments.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    is_test_path,
)
from _py_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    lines,
    name_of,
    trees,
)

OPT_OUT = "toolchain-skip-ok"

MESSAGE = (
    "skipif/importorskip gated on binary discovery with no CI guard — on a "
    "runner missing the tool this silently zeroes the guarded coverage while "
    "the suite stays green. Make it fail in CI: `shutil.which(...) is None "
    'and not os.environ.get("CI")`, or annotate '
    f"`# {OPT_OUT}: <reason>`."
)

# The final component of a call that discovers a binary on PATH. Matching the
# last component rather than the whole dotted name is what accepts every import
# spelling — `shutil.which`, a bare `which` from `from shutil import which`, and
# `distutils.spawn.find_executable`.
_DISCOVERY = frozenset({"which", "find_executable"})
# The final component of a name that reads the environment, where a CI guard
# lives. A bare `CI` name (a module-level `CI = os.environ.get("CI")`) and the
# literal key counts too.
_ENV_READERS = frozenset({"environ", "getenv"})
_CI = "CI"


def _is_skip_call(node: ast.AST) -> bool:
    """True when NODE is a ``pytest.mark.skipif`` / ``pytest.importorskip`` call.

    The last one or two components decide it, so the decorator, the bare
    ``mark.skipif`` spelling, and a module-level ``pytestmark = …`` assignment
    are all the same node shape."""
    if not isinstance(node, ast.Call):
        return False
    parts = (name_of(node.func) or "").split(".")
    return parts[-1] == "importorskip" or parts[-2:] == ["mark", "skipif"]


def _condition(call: ast.Call) -> list[ast.AST]:
    """The expressions a skip's verdict is computed from: ``skipif``'s condition
    (positional or ``condition=``), or every argument of an ``importorskip``.

    Scoping to these — never the whole call — is what keeps a ``reason=`` string
    from supplying either half of this lint's verdict."""
    if (name_of(call.func) or "").split(".")[-1] == "importorskip":
        return [*call.args, *(kw.value for kw in call.keywords)]
    condition = next(
        (kw.value for kw in call.keywords if kw.arg == "condition"),
        call.args[0] if call.args else None,
    )
    return [condition] if condition is not None else []


def _names(expressions: list[ast.AST]) -> tuple[set[str], set[object]]:
    """(final name components, literal constants) reachable in EXPRESSIONS."""
    components: set[str] = set()
    constants: set[object] = set()
    for expression in expressions:
        for node in ast.walk(expression):
            if isinstance(node, (ast.Name, ast.Attribute)):
                components.add((name_of(node) or "").split(".")[-1])
            elif isinstance(node, ast.Constant):
                constants.add(node.value)
    return components, constants


def _is_unguarded(call: ast.Call) -> bool:
    """True when CALL's verdict turns on binary discovery with no CI guard."""
    components, constants = _names(_condition(call))
    if not components & _DISCOVERY:
        return False
    return not (components & _ENV_READERS or _CI in components or _CI in constants)


def violations(text: str) -> list[int]:
    """1-based line numbers of skipif/importorskip calls whose condition does
    binary discovery without a CI guard."""
    physical = lines(text)
    # Each flagged call's own last line: a reason written beside the condition it
    # excuses lives inside this span, which is where authors put it.
    _ends = {
        node.lineno: (node.end_lineno or node.lineno)
        for tree in trees(text)
        for node in ast.walk(tree)
        if _is_skip_call(node) and _is_unguarded(node)
    }
    hits = set(_ends)
    return sorted(
        lineno
        for lineno in hits
        if lineno <= len(physical)
        and not annotated_near(physical, lineno, OPT_OUT, span_end=_ends.get(lineno))
    )


def main(argv: list[str]) -> int:
    status = 0
    for path in argv:
        if not is_test_path(path):
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        for lineno in violations(text):
            print(f"{path}:{lineno}: {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
