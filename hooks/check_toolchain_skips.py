#!/usr/bin/env python3
"""Flag pytest skips gated on binary discovery with no CI environment guard.

`pytest.mark.skipif(shutil.which("node") is None, …)` reads as a harmless
local-dev convenience — but on a CI runner missing the tool it silently zeroes
the coverage of everything the test guards, and the suite stays green. The
skip must FAIL (not skip) in CI, e.g.::

    shutil.which("node") is None and not os.environ.get("CI")

Deliberately conservative (precision over recall): only conditions that
reference binary discovery (`shutil.which`, a bare `which(…)` call, or
`find_executable`) are examined, and any reference to a CI env guard
(`os.environ` / `os.getenv` / a `CI` name) in the same condition passes.
Applies to `pytest.mark.skipif(…)` and `pytest.importorskip(…)` call texts in
Python test files (`test_*.py` / `*_test.py` / files under a `tests/` dir);
non-test Python files are never scanned.

Opt out with `# toolchain-skip-ok: <reason>` on the call's first line or the
line above. Invoked by pre-commit with the staged Python files as arguments.
"""

import re
import sys
from pathlib import Path

OPT_OUT = "toolchain-skip-ok"

_CALL = re.compile(r"\bpytest\.(?:mark\.skipif|importorskip)\s*\(")
_WHICH = re.compile(r"shutil\.which|(?<![\w.])which\s*\(|find_executable")
_CI_GUARD = re.compile(r"os\.environ|os\.getenv|(?<![\w.])CI(?![\w])")

MESSAGE = (
    "skipif/importorskip gated on binary discovery with no CI guard — on a "
    "runner missing the tool this silently zeroes the guarded coverage while "
    "the suite stays green. Make it fail in CI: `shutil.which(...) is None "
    'and not os.environ.get("CI")`, or annotate '
    f"`# {OPT_OUT}: <reason>`."
)


def is_test_path(path: str) -> bool:
    """True for the Python files pytest collects as tests (name convention or a
    tests/ directory component)."""
    p = Path(path)
    name = p.name
    return (
        name.startswith("test_")
        and name.endswith(".py")
        or name.endswith("_test.py")
        or "tests" in p.parts
    )


def _call_span(text: str, open_paren: int) -> str:
    """The argument text of the call whose `(` sits at OPEN_PAREN, up to its
    balanced close (string-quote aware; an unbalanced call yields the rest of
    the text — fail open into the CI-guard scan, never a crash)."""
    depth = 0
    i = open_paren
    quote: str | None = None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1 : i]
        i += 1
    return text[open_paren + 1 :]


def violations(text: str) -> list[int]:
    """1-based line numbers of skipif/importorskip calls whose condition does
    binary discovery without a CI guard."""
    lines = text.splitlines()
    hits: list[int] = []
    for m in _CALL.finditer(text):
        span = _call_span(text, m.end() - 1)
        if not _WHICH.search(span) or _CI_GUARD.search(span):
            continue
        lineno = text.count("\n", 0, m.start()) + 1
        first_line = lines[lineno - 1] if lineno <= len(lines) else ""
        above = lines[lineno - 2] if lineno >= 2 else ""
        if OPT_OUT in first_line or OPT_OUT in above:
            continue
        hits.append(lineno)
    return hits


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
