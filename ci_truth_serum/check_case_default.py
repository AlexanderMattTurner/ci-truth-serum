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

The block is found with tree-sitter-bash (the shared ``_bash_ast`` grammar),
so a single-line ``case … esac``, a block wrapped in continuations, and a
nested ``case`` are all analyzed exactly as bash parses them, and a ``case``
quoted inside a string or comment is data, never a block.

Opt out with `# case-default-ok: <reason>` trailing the `case` line (or on
the line immediately above) when falling through really is the intent.

Invoked by pre-commit with the staged shell files as arguments.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import iter_nodes, parse  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import run_line_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "case-default-ok"

# Tokens in a case_item before the `)` that are not pattern alternatives.
_LABEL_NOISE = frozenset({"(", "|"})


def _item_has_bare_star(case_item) -> bool:
    """True when one of the arm's `|`-separated label alternatives is a bare `*`."""
    for child in case_item.children:
        if child.type == ")":
            return False
        if child.type in _LABEL_NOISE:
            continue
        if child.text.decode("utf-8", "replace").strip() == "*":
            return True
    return False


def _has_default_arm(case_node) -> bool:
    """True when the case statement carries an arm whose label includes a bare `*`."""
    return any(
        _item_has_bare_star(child)
        for child in case_node.children
        if child.type == "case_item"
    )


def violations(text: str) -> list[int]:
    """1-based line numbers of `case` statements with no bare `*)` default arm
    (and without an opt-out annotation)."""
    physical = text.splitlines()
    hits: list[int] = []
    for case_node in iter_nodes(parse(text), "case_statement"):
        if _has_default_arm(case_node):
            continue
        lineno = case_node.start_point[0] + 1
        raw = physical[lineno - 1] if lineno - 1 < len(physical) else ""
        if OPT_OUT in raw or (lineno >= 2 and OPT_OUT in physical[lineno - 2]):
            continue
        hits.append(lineno)
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
