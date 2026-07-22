#!/usr/bin/env python3
"""Require a bare `*)` default arm on every shell `case … esac` block.

A `case` with no `*)` arm silently ignores any value it doesn't enumerate:
the block falls through, nothing runs, and whatever the arms were supposed to
set stays unset. Real incident: an unexpected bump-type value matched no arm,
left `NEW_VERSION` unset, and the release script continued with an empty
version.

Only a BARE `*)` (or `(*)` / `* )`) arm counts as a default: `*.txt)`,
`--*)`, and `foo|*bar)` are globs that match a subset, not everything —
though a multi-pattern arm containing a bare `*` alternative (`x|*)`) does
count. The default may reject the value (`*) die "unknown: $1" ;;`) — the
point is that SOMETHING runs.

Opt out with `# case-default-ok: <reason>` trailing the `case` line (or on
the line immediately above) when falling through really is the intent.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import run_line_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "case-default-ok"

# A `case … in` opener / `esac` closer, as statement words.
_CASE_OPEN = re.compile(r"(?:^|[\s;(])case\s+\S.*?\s+in(?:\s|;|$)")
_ESAC = re.compile(r"(?:^|[\s;])esac(?:\s|;|\)|$)")
# An arm label: pattern list ending in `)` at the start of a line (after `;;`
# resets, arms begin at line starts in practice). The label may be
# paren-wrapped: `(pattern)`.
_ARM = re.compile(r"^\s*\(?\s*(?P<patterns>[^)]*?)\s*\)")
# Words that begin compound statements an arm body may open with — a line
# starting with one of these is body, not a label.
_BODY_STARTERS = re.compile(
    r"^\s*(?:if|then|else|elif|fi|for|while|until|do|done|case|esac|function)\b"
)


def _strip_comment(line: str) -> str:
    """LINE up to the first unquoted `#` (quote-aware, single-line scope)."""
    in_s = in_d = False
    for idx, ch in enumerate(line):
        if in_s:
            in_s = ch != "'"
        elif in_d:
            in_d = ch != '"'
        elif ch == "'":
            in_s = True
        elif ch == '"':
            in_d = True
        elif ch == "#":
            return line[:idx]
    return line


def _has_default_alternative(patterns: str) -> bool:
    """True when the arm's `|`-separated pattern list contains a bare `*`."""
    return any(alt.strip() == "*" for alt in patterns.split("|"))


def violations(text: str) -> list[int]:
    """1-based line numbers of `case` openers whose block reaches `esac`
    without a bare `*)` arm (and without an opt-out annotation)."""
    physical = text.splitlines()
    hits: list[int] = []
    # Stack of open case frames: (opener line, has_default, expecting_label).
    stack: list[dict] = []
    for lineno, raw in enumerate(physical, 1):
        line = _strip_comment(raw)
        stripped = line.strip()
        if not stripped and not stack:
            continue

        opened_here = _CASE_OPEN.search(line)
        if stack and not opened_here:
            frame = stack[-1]
            # Fail-open catch-all: a bare `*)` anywhere on the line (e.g. the
            # compact `a) x ;; *) y ;;` form) counts as the default — over-
            # accepting here only ever suppresses a finding, never adds one.
            if re.search(r"(?:^|[;\s(|])\*\s*\)", line):
                frame["has_default"] = True
            # A label is only read where an arm may begin: right after `case
            # … in` or after a `;;`-family terminator.
            if frame["expecting"] and stripped and not _BODY_STARTERS.match(line):
                arm = _ARM.match(line)
                if arm:
                    frame["expecting"] = False
                    if _has_default_alternative(arm.group("patterns")):
                        frame["has_default"] = True
            if re.search(r";;&?|;&", line):
                frame["expecting"] = True

        if opened_here:
            opted = OPT_OUT in raw or (lineno >= 2 and OPT_OUT in physical[lineno - 2])
            stack.append(
                {
                    "line": lineno,
                    "has_default": False,
                    "expecting": True,
                    "opted": opted,
                }
            )
            continue

        if stack and _ESAC.search(line):
            frame = stack.pop()
            if not frame["has_default"] and not frame["opted"]:
                hits.append(frame["line"])
    return sorted(hits)


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "`case` block has no bare `*)` default arm — an unexpected value matches "
        "nothing, the block silently no-ops, and whatever the arms set stays "
        "unset (an unknown bump type left NEW_VERSION empty this way). Add a "
        f"`*)` arm (even just a `die`), or annotate `# {OPT_OUT}: <reason>` "
        "on the case line.",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
