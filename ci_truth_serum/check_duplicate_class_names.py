#!/usr/bin/env python3
"""Report a top-level class name a scanned file defines that another module in
scope also defines.

PROBLEM CLASS — two modules define the same class name, so neither is the
definition and a call site reads one header while getting the other's
behaviour.

The rule, kept literal on purpose:
  * A DEFINITION is a MODULE-LEVEL `ast.ClassDef`; a nested class never collides.
  * A definition COLLIDES when the same name is defined at module level by at
    least one OTHER file in scope.
  * SCOPE is every tracked `.py` file under `--repo-root` (default: `git
    rev-parse --show-toplevel`), restricted to `--scope DIR` (repeatable) when
    given, else the whole tracked tree. A test file never joins scope
    (`_cts_linecheck.is_test_path`) — a file given on argv is scanned for its own
    classes regardless, since judging it needs to know what it defines.
  * EXEMPT: `# allow-duplicate-class: <reason>` anywhere in the class HEADER —
    the `class` line, or the `):` line `ruff format` may split it onto. Per
    DEFINITION — every other file defining the same name still reports.

Only a file named on argv is REPORTED; the rest of scope supplies the other
half of the question. Exit 1 on any hit, 2 on an empty argv (`run_file_cli`).
"""

import argparse
import ast
import io
import subprocess
import sys
import tokenize
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotation_re,
    is_test_path,
    run_file_cli,
)

OPT_OUT = "allow-duplicate-class"


@dataclass(frozen=True, slots=True)
class ModuleClasses:
    """One module's top-level class names, and which of them opted out.

    DEFINED holds EVERY name, annotated ones included, because a name's presence
    is what makes another module's copy a collision. EXEMPT is the subset this
    module is not reported for. Keeping the two apart is what makes the
    annotation per-definition: dropping an annotated name from DEFINED would
    take a name shared by exactly two modules below the two-file threshold,
    silencing the OTHER module's report, which its author never opted out of.
    LINES maps each name to its first definition's line, for reporting only —
    it plays no part in collision detection.
    """

    defined: tuple[str, ...]
    exempt: frozenset[str]
    lines: dict[str, int] = field(default_factory=dict)


def _annotated_header_rows(source: str) -> set[int]:
    """The 1-based row of every `class` keyword whose HEADER carries the
    annotation.

    A header is the `class` keyword through the `:` that opens the body. It is
    read whole rather than as one line because `ruff format` splits a header
    the annotation pushed past the line limit: `class Row(NamedTuple):`
    becomes three lines and the comment lands on the closing `):`.

    Tokenized rather than matched over the text: the `:` that ends a header is
    not the `:` in an annotated base or a subscript, and bracket depth is what
    tells them apart.
    """
    marker = annotation_re(OPT_OUT)
    lines = source.splitlines()
    annotated: set[int] = set()
    start: int | None = None
    depth = 0
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if start is None:
            if token.type == tokenize.NAME and token.string == "class":
                start, depth = token.start[0], 0
            continue
        if token.type != tokenize.OP:
            continue
        if token.string in "([{":
            depth += 1
        elif token.string in ")]}":
            depth -= 1
        elif token.string == ":" and depth == 0:
            close = token.start[0]
            if any(marker.search(line) for line in lines[start - 1 : close]):
                annotated.add(start)
            start = None
    return annotated


def top_level_classes(source: str) -> ModuleClasses:
    """SOURCE's module-level class names, split by the `# allow-duplicate-class:`
    annotation anywhere in the class header."""
    annotated_rows = _annotated_header_rows(source)
    defined: list[str] = []
    exempt: set[str] = set()
    lines: dict[str, int] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        defined.append(node.name)
        lines.setdefault(node.name, node.lineno)
        if node.lineno in annotated_rows:
            exempt.add(node.name)
    return ModuleClasses(tuple(defined), frozenset(exempt), lines)


def find_collisions(classes_by_file: dict[str, ModuleClasses]) -> dict[str, list[str]]:
    """{rel: [colliding class names]} — a name is colliding when at least one
    OTHER file in the mapping defines it too, and this file did not exempt it.
    Zero-hit files are kept."""
    files_by_name: dict[str, set[str]] = defaultdict(set)
    for rel, classes in classes_by_file.items():
        for name in classes.defined:
            files_by_name[name].add(rel)
    return {
        rel: sorted(
            {
                name
                for name in classes.defined
                if len(files_by_name[name]) > 1 and name not in classes.exempt
            }
        )
        for rel, classes in classes_by_file.items()
    }


def _default_repo_root() -> Path:
    """`git rev-parse --show-toplevel` — the tree argv/`--scope` paths resolve
    against by default. Depth-based parent-walking breaks when a caller's cwd
    moves; asking git is this pack's usual answer."""
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(out)


def _tracked_python_files(repo_root: Path, scopes: list[str]) -> list[str]:
    """Every tracked, non-test `.py` file under REPO_ROOT, restricted to SCOPES
    (each resolved relative to REPO_ROOT) when given, else the whole tree."""
    cmd = ["git", "-C", str(repo_root), "ls-files", "-z"]
    if scopes:
        cmd += ["--", *scopes]
    listing = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return sorted(
        rel
        for rel in listing.split("\0")
        if rel.endswith(".py") and (repo_root / rel).is_file() and not is_test_path(rel)
    )


def _relative(path: str, repo_root: Path) -> str:
    """PATH as a POSIX string relative to REPO_ROOT, however it was spelled on
    argv (absolute, or relative to the current directory)."""
    return Path(path).resolve().relative_to(repo_root).as_posix()


def scan_repo(
    argv_paths: list[str], repo_root: Path, scopes: list[str]
) -> tuple[dict[str, list[str]], dict[str, ModuleClasses]]:
    """({argv-relative-path: [colliding class names]}, {rel: ModuleClasses})
    for every scanned argv path — the rest of scope only supplies the other
    half of the question. An argv path outside every `--scope` directory is
    still compared: judging it needs to know what it itself defines."""
    argv_rel = [_relative(p, repo_root) for p in argv_paths if p.endswith(".py")]
    reportable = [rel for rel in argv_rel if not is_test_path(rel)]
    all_files = sorted({*_tracked_python_files(repo_root, scopes), *reportable})
    classes_by_file: dict[str, ModuleClasses] = {}
    for rel in all_files:
        try:
            text = (repo_root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        classes_by_file[rel] = top_level_classes(text)
    collisions = find_collisions(classes_by_file)
    hits = {rel: collisions[rel] for rel in reportable if collisions.get(rel)}
    return hits, classes_by_file


_WHY = (
    "the same top-level class name in two modules leaves neither as the "
    "definition, so a call site reads one header and gets the other's behaviour"
)
_REMEDY = (
    "make one module the definition and import it, or rename the one that "
    "means something else; annotate a deliberate repeat with "
    f"`# {OPT_OUT}: <reason>` anywhere in the CLASS HEADER"
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        metavar="DIR",
        help="restrict the comparison scope to DIR (repeatable; default: the "
        "whole tracked tree)",
    )
    parser.add_argument(
        "--repo-root",
        metavar="DIR",
        help="the tree argv and --scope paths resolve against (default: "
        "`git rev-parse --show-toplevel`)",
    )
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    if not args.paths:
        print(
            "check_duplicate_class_names: no files to scan. This check reads "
            "only the paths you give it, so an empty run would report a "
            "clean pass over nothing.",
            file=sys.stderr,
        )
        return 2

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else _default_repo_root()
    )
    hits, classes_by_file = scan_repo(args.paths, repo_root, args.scope)
    if not hits:
        return 0
    for rel in sorted(hits):
        for name in hits[rel]:
            lineno = classes_by_file[rel].lines.get(name, 1)
            print(
                f"{rel}:{lineno}: duplicated class name `{name}` — {_WHY}. {_REMEDY}.",
                file=sys.stderr,
            )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
