#!/usr/bin/env python3
"""
Ban a `#`-leading line inside a YAML FOLDED (`>`/`>-`) block scalar whose value is
an argument string.

A folded scalar has NO comment syntax. Every indented line under the header is
content, and folding replaces the newlines with spaces — so a line a human wrote
as a comment lands in the middle of the value. When that value is an argument
string something later shell-splits, `#` starts a shell comment and the split
DISCARDS every argument after it, so flags a reader can plainly see in the file
never reach the program:

    tool_args: >-
      --setting-sources user
      # Path-scoped, never bare: explaining the grant below
      --allowedTools "Read(./**),Edit(//tmp/out.json)"

parses to `--setting-sources user # Path-scoped, … --allowedTools "…"`, and the
tool runs with NO permission rules at all. That is the worst shape this mistake
can take: the file documents a security boundary that is not in force, and the
grant reads tighter than it is to everyone who reviews it. This has shipped for
real — four separate grant sites in one commit, where `--allowedTools` and
`--add-dir` never reached the CLI while the diff read as though the scoping were
present.

The remedy is to move the comment ABOVE the key, where YAML comment syntax
applies:

    # Path-scoped, never bare: explaining the grant below
    tool_args: >-
      --setting-sources user
      --allowedTools "Read(./**),Edit(//tmp/out.json)"

Two deliberate limits keep this to the shape that actually bites. A LITERAL
(`|`/`|-`) scalar is not covered: it keeps newlines, a `#` line stays on its own
line, and prompts and markdown bodies legitimately begin lines with `#`. And a
folded block is judged only when it is an ARGUMENT STRING — some other content
line in it begins with `-`, the way an option does — because a folded
`description:` of prose that happens to open a line with `#` is mangled
cosmetically, not silently truncated.

Opt out with `# allow-folded-scalar-comment: <reason>` on the flagged line itself
(or on the block content line above it), for the rare folded value whose own prose
really does begin a line with a literal `#`. It cannot go on the line above the
KEY, because that line is outside the block and would be an ordinary YAML comment.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotation_re,
    workflow_files as _workflow_files,
)
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

ALLOW = "allow-folded-scalar-comment"
_ALLOW_RE = annotation_re(ALLOW)

# A mapping key whose value is a folded block scalar. The header may carry an
# indentation indicator, a chomping indicator, and a real trailing comment — the
# one place a `#` IS a comment in this construct.
_FOLDED_HEADER = re.compile(
    r"^(?P<indent>\s*)(?:-\s+)?[^\s#][^:]*:\s*>[0-9]*[-+]?\s*(?:#.*)?$"
)

# A sequence entry that is itself a folded scalar (`- >-`).
_FOLDED_ITEM = re.compile(r"^(?P<indent>\s*)-\s*>[0-9]*[-+]?\s*(?:#.*)?$")

_COMMENT_LINE = re.compile(r"^\s*#")

# What marks a folded value as an argument string rather than prose: a content line
# that begins the way an option does. Only such a value gets shell-split, and only
# there does a folded `#` silently truncate the arguments.
_OPTION_LINE = re.compile(r"^\s*-")

MESSAGE = (
    "this line reads as a comment but sits inside a YAML folded (`>`/`>-`) block "
    "scalar, which has no comment syntax — it is folded into the value. When that "
    "value is an argument string, shell-splitting treats the `#` as a comment and "
    "DISCARDS every argument after it, so the flags below it never reach the "
    "program. Move the comment above the key (where YAML comment syntax applies), "
    f"or annotate '# {ALLOW}: <reason>'."
)


def blocks(physical: list[str]) -> list[list[int]]:
    """The 1-based content line numbers of each folded block scalar in PHYSICAL."""
    found: list[list[int]] = []
    current: list[int] | None = None
    header_indent = 0
    for lineno, raw in enumerate(physical, 1):
        if current is not None:
            if not raw.strip():
                continue  # a blank line carries no indent, so it is no dedent
            if len(raw) - len(raw.lstrip()) > header_indent:
                current.append(lineno)
                continue
            current = None  # the block ended; fall through and re-test this line
        match = _FOLDED_HEADER.match(raw) or _FOLDED_ITEM.match(raw)
        if match:
            header_indent = len(match.group("indent"))
            current = []
            found.append(current)
    return found


def _opted_out(lineno: int, physical: list[str]) -> bool:
    """True when the flagged line, or the block content line above it, carries a
    reason-bearing opt-out. The line above the KEY is outside the block, so it is
    an ordinary YAML comment and is not consulted."""
    return any(
        _ALLOW_RE.search(physical[n - 1]) for n in (lineno, lineno - 1) if n >= 1
    )


def violations(text: str) -> list[int]:
    """1-based line numbers of a comment-looking line that is really content of a
    folded argument-string scalar, and carries no opt-out annotation."""
    physical = text.splitlines()
    hits: list[int] = []
    for block in blocks(physical):
        comments = [n for n in block if _COMMENT_LINE.match(physical[n - 1])]
        options = [n for n in block if _OPTION_LINE.match(physical[n - 1])]
        if not comments or not options:
            continue
        hits += [n for n in comments if not _opted_out(n, physical)]
    return sorted(hits)


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """(line, message) for every violation in PATH.

    An unparseable workflow is reported as a violation rather than passed clean:
    this file IS the artifact under test, so "no findings" on it would be a false
    green."""
    text = path.read_text(encoding="utf-8")
    try:
        safe_load(text)  # a validity probe: the detector below is line-based
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify that no "
                "folded block scalar folds a comment into an argument string — fix "
                "the syntax (or run actionlint) and re-check.",
            )
        ]
    return [(lineno, MESSAGE) for lineno in violations(text)]


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
