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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import iter_nodes, parse  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import run_line_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "case-default-ok"


def _arm_is_default(case_item) -> bool:
    """True when a `case_item`'s pattern list contains a bare `*` alternative —
    the catch-all default.

    The grammar yields each alternative before the `)` as a `word` /
    `extglob_pattern` node (separated by `|`, optionally paren-wrapped), so a bare
    `*` is exactly a pattern node whose text is `*`. A subset glob (`*.txt`, `--*`)
    has other text and is not a default; a quoted `"*"` is a `string` node (a
    literal asterisk), so it is correctly NOT a default. A multi-pattern arm with a
    bare `*` alternative (`x|*)`) does count."""
    for child in case_item.children:
        if child.type == ")":
            break
        if child.type in ("word", "extglob_pattern") and child.text.decode() == "*":
            return True
    return False


def violations(text: str) -> list[int]:
    """1-based line numbers of `case … esac` statements with no bare `*)` default
    arm (and without an opt-out annotation).

    Driven off the bash AST's `case_statement` nodes, so a single-line
    `case … esac`, a compact `a) x ;; *) y ;;`, a nested case, comments, and quoted
    patterns are all parsed by the grammar rather than a hand-rolled line scanner.
    The finding is anchored on the `case` keyword's line; the opt-out is read from
    that line or the one immediately above."""
    physical = text.splitlines()
    hits: list[int] = []
    for case_node in iter_nodes(parse(text), "case_statement"):
        arms = [c for c in case_node.children if c.type == "case_item"]
        if any(_arm_is_default(arm) for arm in arms):
            continue
        line = case_node.start_point[0] + 1
        raw = physical[line - 1] if 0 <= line - 1 < len(physical) else ""
        prev = physical[line - 2] if line - 2 >= 0 else ""
        if OPT_OUT in raw or OPT_OUT in prev:
            continue
        hits.append(line)
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
