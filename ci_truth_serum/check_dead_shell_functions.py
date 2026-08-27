#!/usr/bin/env python3
"""Flag a shell function this tree defines but nothing calls.

PROBLEM CLASS — a function no code calls rots: its assumptions drift from the
live code, and it tells a reader a code path exists that does not.

The files on argv are the DEFINITION sites this check reports on — the usual
content-lint contract. A PRODUCTION shell file (not a test) defines a function
with `name() {` or `function name { … }`. The REFERENCE scan is different: it
always sweeps every tracked, non-test, non-doc file in `--repo-root` (default:
`git rev-parse --show-toplevel`), because a caller can sit in a workflow
`run:` block or a Python shell-out this check never receives on argv. A
function is DEAD when its name is a word-boundary token nowhere in that sweep
except its own definition line(s).

A definition is read from the bash grammar's own `function_definition` node,
never a `name() {` line regex — a signature inside a heredoc body, a printed
message, or a string is data to bash and defines nothing, and the grammar is
what tells the difference. Comments are stripped with the same grammar
before the reference token scan, so a name mentioned only in prose does not
count as a call. A name built at runtime (`ck_${name//-/_}`) still counts as
a reference when its constructed-name PREFIX (`ck_${`) appears anywhere in
the sweep.

BLIND SPOT, and the direction it errs: a function defined identically in a
tracked file this check was NOT given on argv (a sibling copy, a vendored
script) has its own definition line read as an ordinary REFERENCE here, since
only the argv files are scanned for definitions. That undercounts dead
functions — it can miss one, never invent one — because the reference count
only ever goes UP from a line this check cannot identify as a definition.

`--always-live NAME` (repeatable) extends the built-in allowlist of names an
absent textual caller never means dead: shell entrypoints and dispatch hooks
invoked by the shell itself, not by name in the text. Carries no baseline —
every dead function fails the run.
"""

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    iter_nodes,
    node_text,
    parse,
    strip_comments,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    is_shell_source,
    is_test_path,
    run_file_cli,
)

# Names that are entrypoints or dynamically dispatched by the shell itself, so
# an absent textual caller never means dead.
ALWAYS_LIVE = frozenset(
    {
        "main",  # conventional top-level entrypoint, invoked as `main "$@"`
        "command_not_found_handle",  # bash's special not-found dispatch hook
        "command_not_found_handler",  # zsh's special not-found dispatch hook
    }
)

_DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt"})

_TOKEN_RE = re.compile(r"[\w.:-]+")


def extract_defs(text: str) -> list[tuple[str, int]]:
    """(name, 1-based lineno) for every `function_definition` node in TEXT —
    both `name() { … }` and `function name [()] { … }` are one grammar node,
    so a signature inside a heredoc body, a printed message, or a comment is
    data to bash and is never read as one."""
    defs: list[tuple[str, int]] = []
    for node in iter_nodes(parse(text), "function_definition"):
        name_node = next((c for c in node.children if c.type == "word"), None)
        if name_node is not None:
            defs.append((node_text(name_node), node.start_point[0] + 1))
    return defs


def _dispatch_markers(name: str) -> list[str]:
    """Constructed-name dispatch markers for NAME: each underscore-terminated
    namespace prefix glued to `${`. `ck_${name//-/_}` never writes the literal
    `ck_cli_help`, so the marker `ck_${` recovers it. The prefix must carry an
    alphanumeric, or a leading-underscore name would produce the bare `_${`
    that matches any `word_${var}` expansion, sparing every such function."""
    return [
        name[: i + 1] + "${"
        for i, ch in enumerate(name)
        if ch == "_" and i + 1 < len(name) and any(c.isalnum() for c in name[:i])
    ]


def _is_doc(rel: str) -> bool:
    """A documentation file, excluded from the reference scan: a function name
    in prose is a mention, not a call."""
    path = Path(rel)
    return path.suffix in _DOC_SUFFIXES or "docs" in path.parts


def tracked_reference_files(repo_root: Path) -> list[str]:
    """Every tracked, non-test, non-doc path under REPO_ROOT — the reference
    scan's scope. `git ls-files` reports the index, so a path can be missing
    (a rename/delete race) or unreadable despite being tracked."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [
        rel for rel in tracked if rel and not is_test_path(rel) and not _is_doc(rel)
    ]


def reference_text(repo_root: Path, rel: str) -> str | None:
    """REL's text for the reference scan, comments stripped when it is shell,
    or None when it cannot be read (binary content, a race on the index)."""
    try:
        text = (repo_root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if is_shell_source(rel, text.split("\n", 1)[0]):
        return strip_comments(text)
    return text


def reference_counts(repo_root: Path) -> tuple[Counter[str], str]:
    """(token counts, whole scanned text) across every REPO_ROOT reference
    file. The joined text is reused for the dispatch-marker containment
    check, so that scan does not re-read the tree a second time."""
    total: Counter[str] = Counter()
    joined: list[str] = []
    for rel in tracked_reference_files(repo_root):
        text = reference_text(repo_root, rel)
        if text is None:
            continue
        total.update(_TOKEN_RE.findall(text))
        joined.append(text)
    return total, "\n".join(joined)


class DeadFn(NamedTuple):
    """A function with no caller found by the reference scan: its file, name,
    and definition line."""

    rel: str
    name: str
    lineno: int


def find_dead(
    paths: list[str],
    repo_root: Path,
    always_live: frozenset[str] = frozenset(),
) -> list[DeadFn]:
    """Every function DEFINED in a production shell file among PATHS with no
    reference in REPO_ROOT's whole tracked tree, excluding ALWAYS_LIVE and the
    built-in allowlist."""
    live = ALWAYS_LIVE | always_live
    total, corpus = reference_counts(repo_root)
    dead: list[DeadFn] = []
    for path in paths:
        if is_test_path(path):
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not is_shell_source(path, text.split("\n", 1)[0]):
            continue
        stripped = strip_comments(text)
        lines = stripped.splitlines()
        file_defs = extract_defs(stripped)
        own: Counter[str] = Counter()
        for name, lineno in file_defs:
            own[name] += _TOKEN_RE.findall(lines[lineno - 1]).count(name)
        for name, lineno in file_defs:
            if name in live:
                continue
            if total[name] - own[name] > 0:
                continue
            if any(marker in corpus for marker in _dispatch_markers(name)):
                continue
            dead.append(DeadFn(path, name, lineno))
    return sorted(dead)


def _default_repo_root() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            "check_dead_shell_functions: not inside a git repository — pass "
            "--repo-root explicitly."
        )
    return result.stdout.strip()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=None,
        metavar="DIR",
        help="root of the reference sweep (default: `git rev-parse --show-toplevel`)",
    )
    parser.add_argument(
        "--always-live",
        action="append",
        default=[],
        metavar="NAME",
        help="a function name that is never dead — extends the built-in "
        "entrypoint/dispatch-hook allowlist (repeatable)",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if not args.files:
        print(
            "check_dead_shell_functions: no files to scan. This check reads "
            "only the paths you give it, so an empty run would report a clean "
            "pass over nothing.",
            file=sys.stderr,
        )
        print(
            "  to scan the whole tree: git ls-files -z | xargs -0 python -m "
            "ci_truth_serum.check_dead_shell_functions",
            file=sys.stderr,
        )
        return 2
    repo_root = Path(args.repo_root) if args.repo_root else Path(_default_repo_root())
    dead = find_dead(args.files, repo_root, frozenset(args.always_live))
    if not dead:
        return 0
    for rel, name, lineno in dead:
        print(
            f"{rel}:{lineno}: `{name}` is referenced only from its own "
            "definition line — no production code calls it. Remove it, or "
            f"pass `--always-live {name}` when the shell itself dispatches it "
            "by a name the text never spells out.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
