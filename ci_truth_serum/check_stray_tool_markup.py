#!/usr/bin/env python3
"""Flag stray agent tool-call markup accidentally committed into a file.

When an agent authors a file through a tool call, the transport wraps the file
body in markup — an opening ``invoke`` / ``parameter`` tag, the body, then the
matching closing tags. A truncation or copy-paste slip can leak that scaffolding
INTO the committed file: the classic incident is a Markdown doc that ends with a
bare closing ``content`` tag and a bare closing ``invoke`` tag on their own
lines, which render as literal garbage and are caught only by a human reviewer —
no lint fired. Every consumer of this package is agent-driven, so this corruption
class is universal; this check catches it mechanically.

Detection is deliberately HIGH-PRECISION: a line is flagged only when, after
stripping surrounding whitespace, it is ENTIRELY a single tool-call tag —
``</invoke>``, an opening ``invoke``/``parameter`` tag (with or without
attributes), ``</function_calls>``, ``<function_calls>``, a bare closing
``content`` tag, and the ``antml:``-prefixed variants of each. An inline mention
inside prose or an inline-code span is never a whole-line tag, so it is left
alone; syntax shown as an example inside a fenced code block (``` / ~~~) is
skipped outright.

Only whole-line bare tags are flagged, so the false-positive surface is tiny. The
one deliberately-omitted form is a bare OPENING ``content`` tag: unlike the
tool-call-specific ``invoke``/``parameter``/``function_calls`` tags, an opener
collides with the deprecated HTML/JSX ``content`` element, so only its closing
form (the shape that actually leaks) is flagged.

Escape hatch: a file that genuinely must carry such a line (extraordinarily rare)
suppresses it with an ``allow-stray-markup: <reason>`` annotation on the line
directly above — either as a ``<!-- allow-stray-markup: … -->`` HTML comment
(Markdown) or a ``# allow-stray-markup: …`` comment (code). Prefer a fenced code
block for genuine documentation of the syntax.

Invoked by pre-commit with the staged prose/code files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
    run_line_checks,
)

# The tool-call-specific tags: both their opening and closing whole-line forms are
# stray scaffolding (these names have no legitimate whole-line use in source).
_OPEN_CLOSE_NAME = r"(?:antml:)?(?:invoke|parameter|function_calls)"
# `content` leaks only as a bare closing tag; a bare `<content>` opener collides
# with the deprecated HTML/JSX `content` element, so it is intentionally not flagged.
_STRAY_RE = re.compile(
    rf"</{_OPEN_CLOSE_NAME}>"  # closing invoke/parameter/function_calls
    rf"|<{_OPEN_CLOSE_NAME}(?:\s[^<>]*)?>"  # opening, with optional attributes
    rf"|</(?:antml:)?content>"  # bare closing content tag only
)

_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_ALLOW = "allow-stray-markup"


def violations(text: str) -> list[int]:
    """1-based line numbers that are entirely a stray tool-call tag, outside any
    fenced code block and without an ``allow-stray-markup`` annotation on the line
    directly above."""
    lines = text.splitlines()
    hits: list[int] = []
    in_fence = False
    for lineno, raw in enumerate(lines, 1):
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence or not _STRAY_RE.fullmatch(raw.strip()):
            continue
        # A bare tag fills the whole line, so any suppression sits on the line
        # above (an inline annotation would stop the line from being a bare tag).
        if lineno >= 2 and annotated(lines[lineno - 2], _ALLOW):
            continue
        hits.append(lineno)
    return hits


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "stray agent tool-call markup committed into the file — delete the leaked "
        "tag, fence it if documenting the syntax, or annotate the line above with "
        "`allow-stray-markup: <reason>`.",
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
