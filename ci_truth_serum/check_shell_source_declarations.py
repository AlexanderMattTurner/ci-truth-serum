#!/usr/bin/env python3
"""Demand that every `source`/`.` statement shellcheck can follow, or says why not.

shellcheck follows a `source`/`.` line only when it can tell which file it
reads. A computed target (`source "$DIR/lib.sh"`) needs a `# shellcheck
source=<path>` comment on the line above; without one, shellcheck silently
skips the file and never checks it. This bans two silent failures:
UNDECLARED (a computed target, no directive, no `disable=SC1090`/`SC1091`)
and UNRESOLVABLE (a directive, or a literal target, naming a path this tree
lacks). Either way shellcheck exits 0 having never read the library.

A directive is read from the bash `comment` nodes directly above the
statement — climbing through a `list`/`pipeline`/`negated_command`/
`redirected_statement` parent the statement opens, so `[[ -f "$X" ]] &&` on
one line and `source "$X"` on the next still see the directive above the
first line, exactly as shellcheck does. A target is resolved, in order,
against the sourcing file's own directory, `--repo-root` (default: `git
rev-parse --show-toplevel`, since this package ships no fixed repo layout to
hard-code), then each `--search-path DIR` (repeatable, relative to
`--repo-root` unless already absolute — the consumer's own shellcheck `-P`
list, which this package cannot guess).

An ABSOLUTE target (`/dev/null` is shellcheck's do-not-follow marker) is
never a finding. A LITERAL target (no `$`/backtick) needs no directive —
shellcheck resolves it on its own — but is still checked for resolving
somewhere in the tree; a directive always takes priority over a statement's
own literal text when both are present.

Blind spots: a directive inside a heredoc body resolves where the generated
script lands, not where the heredoc sits; a `$(dirname
"${BASH_SOURCE[0]}")`-style self-directory idiom with no directive reads as
UNDECLARED, even though shellcheck's own SCRIPTDIR default would resolve it.
No opt-out token of this package's own — shellcheck's `disable=SC1090`/
`SC1091` already states "this target cannot be known ahead of time".

Invoked by pre-commit with the staged shell files as arguments.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_words,
    iter_nodes,
    parse,
    unquote,
)
from _comments import shell_comments  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    run_file_cli,
    unparseable_shell_reason,
)

# The commands that read another file into the current shell.
_SOURCE_COMMANDS = frozenset({"source", "."})

# `# shellcheck source=<path>`, possibly preceded by other `key=value` flags on
# the same directive comment (`# shellcheck disable=SC2034 source=lib.sh`).
_DIRECTIVE = re.compile(r"^#\s*shellcheck\s+(?:[\w-]+=\S+\s+)*source=(?P<target>\S+)")
# `# shellcheck disable=SC1090,SC1091` — shellcheck's own "cannot be known
# ahead of time" marker, checked as a comma-separated code list so a
# multi-code disable line still counts.
_DISABLE = re.compile(r"^#\s*shellcheck\s+(?:[\w-]+=\S+\s+)*disable=(?P<codes>\S+)")

# Parent node types a directive above the whole construct still governs: bash
# reads `A && source X` and `A | source X` as one logical line, so a directive
# above `A` governs the `source` wherever it sits in the chain.
_ONE_LINE_PARENTS = frozenset(
    {"list", "pipeline", "negated_command", "redirected_statement"}
)

_REMEDY = (
    "give it a `# shellcheck source=<path>` comment naming a path that "
    "resolves, or `# shellcheck disable=SC1090` when the target genuinely "
    "cannot be known ahead of time."
)


def _disables_source_lookup(codes: str) -> bool:
    """True when CODES (a `disable=` value) names SC1090 or SC1091."""
    return bool({"SC1090", "SC1091"} & set(codes.split(",")))


def _directive_line(node: Node) -> int:
    """The 1-based line a directive comment must sit above to govern NODE — the
    top of the chain of one-line parents NODE opens, so a directive above `A &&`
    still governs a `source` on the next physical line."""
    line = node.start_point[0] + 1
    current = node
    while True:
        parent = current.parent
        if parent is None or parent.type == "program":
            return line
        opens_parent = bool(parent.named_children) and parent.named_children[0].id == (
            current.id
        )
        if parent.type not in _ONE_LINE_PARENTS and not opens_parent:
            return line
        current = parent
        line = min(line, current.start_point[0] + 1)


def _comment_block_above(comments: dict[int, str], lineno: int) -> list[str]:
    """The unbroken run of comment lines directly above 1-based LINENO, nearest
    line first. Stops at the first non-comment line, so it never reaches a
    prior statement's own code line."""
    block = []
    line = lineno - 1
    while line in comments:
        block.append(comments[line])
        line -= 1
    return block


class SourceStatement(NamedTuple):
    """One `source`/`.` command: its own line, the target as written, and the
    line a directive above it must sit above."""

    line: int
    directive_line: int
    target: str


def source_statements(root: Node) -> list[SourceStatement]:
    """Every `source`/`.` command in ROOT, per the bash grammar — never a text
    match, so a `.` inside an embedded jq program or a `source` printed by
    `echo` is never read as a statement."""
    out = []
    for node in iter_nodes(root, "command"):
        words = command_words(node)
        if len(words) < 2 or words[0] not in _SOURCE_COMMANDS:
            continue
        out.append(
            SourceStatement(
                line=node.start_point[0] + 1,
                directive_line=_directive_line(node),
                target=unquote(words[1]),
            )
        )
    return out


def resolve_target(
    directory: Path, target: str, repo_root: Path, search_paths: list[str]
) -> Path | None:
    """The file TARGET names when sourced from a file in DIRECTORY, tried in
    the order shellcheck itself resolves a directive: the sourcing file's own
    directory, the repo root, then each SEARCH_PATHS root (repo-root-relative
    unless already absolute)."""
    candidates = [directory / target, repo_root / target]
    candidates += [
        (Path(root) if Path(root).is_absolute() else repo_root / root) / target
        for root in search_paths
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def violations(
    path: str,
    text: str,
    repo_root: Path,
    search_paths: list[str],
    root: Node | None = None,
) -> list[tuple[int, str]]:
    """(1-based line, message) for every `source`/`.` statement in TEXT that
    shellcheck cannot follow."""
    root = parse(text) if root is None else root
    comments = shell_comments(text)
    directory = Path(path).parent
    hits: list[tuple[int, str]] = []
    for statement in source_statements(root):
        block = _comment_block_above(comments, statement.directive_line)
        directive = next((m for line in block if (m := _DIRECTIVE.match(line))), None)
        if directive is not None:
            target = directive.group("target")
            declaration = f"`# shellcheck source={target}`"
        elif "$" not in statement.target and "`" not in statement.target:
            target = statement.target
            declaration = f"`source {target}`"
        elif any(
            (m := _DISABLE.match(line)) and _disables_source_lookup(m.group("codes"))
            for line in block
        ):
            continue
        else:
            hits.append(
                (
                    statement.line,
                    "`source` target is an expansion with no `# shellcheck "
                    f"source=<path>` directive above it — {_REMEDY}",
                )
            )
            continue
        if target.startswith("/"):
            continue  # shellcheck's own do-not-follow marker (e.g. /dev/null)
        if resolve_target(directory, target, repo_root, search_paths) is None:
            hits.append(
                (
                    statement.line,
                    f"{declaration} names no file in the tree, so shellcheck "
                    f"follows nothing — {_REMEDY}",
                )
            )
    return hits


def _default_repo_root() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            "check_shell_source_declarations: not inside a git repository — "
            "pass --repo-root explicitly."
        )
    return result.stdout.strip()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=None,
        metavar="DIR",
        help="resolve a target here too (default: `git rev-parse --show-toplevel`)",
    )
    parser.add_argument(
        "--search-path",
        action="append",
        default=[],
        metavar="DIR",
        help="an extra root to resolve a target against, mirroring shellcheck's "
        "`-P` (repeatable; relative to --repo-root unless already absolute)",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if not args.files:
        print(
            "check_shell_source_declarations: no files to scan. This check "
            "reads only the paths you give it, so an empty run would report a "
            "clean pass over nothing.",
            file=sys.stderr,
        )
        print(
            "  to scan the whole tree: git ls-files -z | xargs -0 python -m "
            "ci_truth_serum.check_shell_source_declarations",
            file=sys.stderr,
        )
        return 2
    repo_root = Path(args.repo_root) if args.repo_root else Path(_default_repo_root())

    status = 0
    for path in args.files:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            reason = unparseable_shell_reason(path, text)
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
            continue
        if reason is not None:
            print(f"{path}: {reason}", file=sys.stderr)
            status = 1
            continue
        found = violations(path, text, repo_root, args.search_path)
        for lineno, message in found:
            print(f"{path}:{lineno}: {message}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
