#!/usr/bin/env python3
"""Flag a hand-kept list of repo paths that omits a path the code it ships names.

PROBLEM CLASS — a collection literal restates a fact that the rest of the code
already defines. A script keeps a list of the repository paths it copies to a
virtual machine, a container or an archive. The files on that list then run in a
place where only the listed paths exist. When one of those files names a repo
path that the list leaves out, the copy lacks the file, and the code fails at run
time with "No such file or directory". Nothing static sees it, because the file
exists in every checkout that a test or a linter reads.

A worked case. A bash array ships nine trees to a VM, among them `bin/lib` and
four files under `.claude/hooks`:

    PAYLOAD=(bin/lib config .claude/hooks/secret_env_vars.py …)

`bin/lib/launch/spawn.py` sits inside `bin/lib`, so it ships. It starts
`f"{root}/.claude/hooks/monitor.py"` under the VM's copy of the tree. The list
picks four files out of `.claude/hooks` by hand and never `monitor.py`, so the
VM had no monitor to start.

A PATH LIST is an assignment in production code whose value is a collection
literal of plain strings: a bash array (`X=(a b c)`, also under `local`,
`declare` or `readonly`), or a Python `list`, `tuple` or `set`, or a
`frozenset(…)`, `set(…)`, `tuple(…)` or `list(…)` call over one. A list in a
test file is not read, because a test's list names what the test scans, not
what ships. A list qualifies only when all of these hold:

  - it has at least three members;
  - every member names a tracked file or a tracked directory, which
    `git ls-files` decides, so a list of globs or absolute paths never
    qualifies;
  - at least one member is a directory, so the list ships a tree of code.

A literal append (`X+=(d e)`) adds its members to the first assignment.

A CONSUMER is a tracked shell or Python file that the list ships: a member, or a
file under a directory member. A finding needs a consumer that spells a repo
path P as a literal, where P meets all of these:

  - P names a tracked file or directory, and the list does not cover it;
  - the directory that holds P is a PICKED directory: the list names a FILE in
    it, and the list does not ship that directory whole.

The second rule carries the precision. A list that names single files out of a
directory shows that the author chose that directory's files one by one, so a
sibling that shipped code names is the omission this check exists to find. A
directory member does not make its parent a picked directory: `bin/lib` on a
list says "this tree", and `bin/other` beside it is a separate choice. One
omitted path is one finding, which lists every shipped line that names it.

The check reads the spelling of P from the grammar, never from the text:

  - Python: a whole string constant (`".claude/hooks/x.py"`); the tail of an
    f-string after one leading value (`f"{root}/.claude/hooks/x.py"`); a `/`
    chain (`root / ".claude" / "hooks" / "x.py"`); and `os.path.join` with one
    leading value and constant parts. A docstring is prose, so it is not read.
  - shell (tree-sitter-bash): a word that is all literal text, or one leading
    expansion and then literal text that starts with `/` (`"$root/.claude/x"`).
    A comment and a heredoc body are not code, and a message with a space in it
    never spells a path.

BLIND SPOT, and the direction it errs. The check reads only literals. A path
that the code computes, a path relative to its own file
(`Path(__file__).parent / "models.json"`), a list built in a loop, and an
omission in a directory that the list does not pick are all invisible, so each
one can make the check MISS an omission. It never invents one: every finding
names a tracked path, which the list does not cover, which a file that the list
ships spells, and whose directory the list picks from by hand. Two facts stay
outside what a static read can prove. The first is whether that line runs where
the list ships: a shipped directory can also hold code that only the host
runs. The second is a computed append (`X+=("$out")`), which can add P at run
time. For either case, annotate the consumer line, or the comment block
directly above it, with
`# path-list-closure-ok: <why the shipped copy does not need this path>`.
The reason is mandatory, and the finding goes away when no unannotated line is
left.

The files on argv are the LIST sites. The consumers come from the tracked tree
under `--repo-root` (default: `git rev-parse --show-toplevel`), because a
consumer that this commit does not touch still breaks at run time. A file that
the grammar cannot parse is refused loudly, on argv or in that sweep.
"""

import argparse
import ast
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    iter_nodes,
    node_text,
    parse as parse_bash,
    unquote,
)
from _cts_comments import comment_lines  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    is_python_source,
    is_shell_source,
    is_test_path,
    source_lines,
    unreadable_reason,
)
from _cts_py_ast import LINE_ENDING, name_of, parse_whole, walk  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "path-list-closure-ok"

# A list with fewer members is not evidence of a hand-kept shipping manifest:
# two paths in a tuple are as often a pair of files a function compares.
MIN_MEMBERS = 3

# The Python calls that wrap a collection literal without changing its members.
_WRAPPERS = frozenset({"frozenset", "set", "tuple", "list"})
_COLLECTIONS = (ast.List, ast.Tuple, ast.Set)

# The shell node types that spell one argument word.
_WORD_TYPES = frozenset({"word", "string", "raw_string", "concatenation"})
_EXPANSIONS = frozenset({"simple_expansion", "expansion"})

# Characters that make a word a pattern or a computed value, not a path.
_NOT_A_PATH = frozenset("*?[]{}$`\\ \t\n'\"")


class PathList(NamedTuple):
    """One candidate list: where it is assigned, its name, and its members."""

    path: str
    line: int
    name: str
    members: tuple[str, ...]


class Reference(NamedTuple):
    """One literal spelling of a repo path in a consumer file."""

    line: int
    path: str


def normalize(raw: str) -> str | None:
    """RAW as a repo-relative POSIX path, or None when it cannot be one.

    A leading `./` and a trailing `/` are dropped. A `..` segment, an empty
    segment, or a character that makes RAW a pattern refuses it, because the
    check compares exact paths and a guess would invent a finding."""
    if not raw or any(char in _NOT_A_PATH for char in raw):
        return None
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.rstrip("/")
    parts = raw.split("/")
    if not raw or raw.startswith("/") or any(p in {"", ".", ".."} for p in parts):
        return None
    return raw


def parent(path: str) -> str:
    """PATH's directory, `""` for a top-level entry."""
    head = str(PurePosixPath(path).parent)
    return "" if head == "." else head


def covered(path: str, members: Iterable[str]) -> bool:
    """True when PATH is a member or sits under a member directory."""
    return any(path == m or path.startswith(m + "/") for m in members)


# ── Lists ────────────────────────────────────────────────────────────────────


def _shell_word(node) -> str | None:
    """The literal value of the shell word NODE, or None when any part of it is
    computed."""
    if any(True for _ in iter_nodes(node, *_EXPANSIONS, "command_substitution")):
        return None
    return unquote(node_text(node))


def shell_lists(text: str, path: str) -> list[PathList]:
    """Every array assignment in the shell TEXT whose members are all literal
    words, with literal appends merged into the first assignment of a name."""
    found: dict[str, PathList] = {}
    for assignment in iter_nodes(parse_bash(text), "variable_assignment"):
        value = assignment.child_by_field_name("value")
        name = assignment.child_by_field_name("name")
        if value is None or name is None or value.type != "array":
            continue
        appends = any(child.type == "+=" for child in assignment.children)
        words = [
            _shell_word(child)
            for child in value.named_children
            if child.type != "comment"
        ]
        literal = tuple(word for word in words if word is not None)
        key = node_text(name)
        if appends and key in found:
            first = found[key]
            found[key] = first._replace(members=first.members + literal)
            continue
        if len(literal) == len(words):
            line = assignment.start_point[0] + 1
            found.setdefault(key, PathList(path, line, key, literal))
    return list(found.values())


def _python_members(value: ast.expr) -> tuple[str, ...] | None:
    """The string members of the collection literal VALUE, or None when VALUE is
    not one, or holds a member that is not a plain string."""
    if isinstance(value, ast.Call) and name_of(value.func) in _WRAPPERS:
        if len(value.args) != 1 or value.keywords:
            return None
        value = value.args[0]
    if not isinstance(value, _COLLECTIONS):
        return None
    members = []
    for element in value.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            return None
        members.append(element.value)
    return tuple(members)


def python_lists(text: str, path: str) -> list[PathList]:
    """Every assignment in the Python TEXT whose value is a collection literal of
    plain strings."""
    out = []
    for node in walk(parse_whole(LINE_ENDING.sub("\n", text))):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        name = name_of(target)
        members = _python_members(value)
        if name is not None and members is not None:
            out.append(PathList(path, node.lineno, name, members))
    return out


def candidate_lists(text: str, path: str) -> list[PathList]:
    """The path-list candidates in TEXT, read with the grammar PATH names."""
    if is_python_source(path):
        return python_lists(text, path)
    if is_shell_source(path, text.split("\n", 1)[0]):
        return shell_lists(text, path)
    return []


# ── References ───────────────────────────────────────────────────────────────


def _shell_parts(node) -> list[str | None]:
    """NODE as a run of literal pieces, with None for each computed piece."""
    if node.type in {"word", "raw_string"}:
        return [unquote(node_text(node))]
    if node.type in _EXPANSIONS or node.type == "command_substitution":
        return [None]
    if node.type == "concatenation":
        return [piece for child in node.children for piece in _shell_parts(child)]
    if node.type == "string":
        pieces: list[str | None] = []
        for child in node.named_children:
            if child.type == "string_content":
                pieces.append(node_text(child))
            else:
                pieces.append(None)
        return pieces
    return [None]


def _tail(pieces: list[str | None]) -> str | None:
    """The literal path PIECES spell: all of them when all are literal, or the
    rest after one leading computed piece when the rest starts with `/`."""
    if pieces and all(piece is not None for piece in pieces):
        return "".join(pieces)  # type: ignore[arg-type]
    if len(pieces) < 2 or pieces[0] is not None or None in pieces[1:]:
        return None
    rest = "".join(pieces[1:])  # type: ignore[arg-type]
    return rest[1:] if rest.startswith("/") else None


def shell_references(text: str) -> Iterator[Reference]:
    """Every literal repo-path spelling in the shell TEXT. A word inside another
    word is read once, as part of the outer one."""
    stack = [parse_bash(text)]
    while stack:
        node = stack.pop()
        if node.type in {"comment", "heredoc_body", "heredoc_redirect"}:
            continue
        if node.type in _WORD_TYPES:
            spelled = _tail(_shell_parts(node))
            if spelled is not None:
                yield Reference(node.start_point[0] + 1, spelled)
            continue
        stack.extend(node.children)


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of every docstring constant in TREE."""
    out = set()
    for node in walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            out.add(id(node.value))
    return out


def _constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _joined(parts: list[ast.expr]) -> str | None:
    """The path that one leading value and then constant PARTS spell."""
    tail = [_constant(part) for part in parts[1:]]
    if len(parts) < 2 or _constant(parts[0]) is not None or None in tail:
        return None
    return "/".join(tail)  # type: ignore[arg-type]


def _division_operands(node: ast.expr) -> list[ast.expr]:
    """The operands of the left-nested `/` chain NODE, in source order."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [*_division_operands(node.left), node.right]
    return [node]


def python_references(text: str) -> Iterator[Reference]:
    """Every literal repo-path spelling in the Python TEXT."""
    tree = parse_whole(LINE_ENDING.sub("\n", text))
    skip = _docstrings(tree)
    for node in walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(value) for value in node.values)
            values = node.values
            first = _constant(values[-1]) if len(values) == 2 else None
            rooted = isinstance(values[0], ast.FormattedValue)
            if rooted and first is not None and first.startswith("/"):
                yield Reference(node.lineno, first[1:])
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            spelled = _joined(_division_operands(node))
            if spelled is not None:
                yield Reference(node.lineno, spelled)
        elif isinstance(node, ast.Call) and name_of(node.func) == "os.path.join":
            spelled = _joined(list(node.args))
            if spelled is not None:
                yield Reference(node.lineno, spelled)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip:
                yield Reference(node.lineno, node.value)


def references(text: str, path: str) -> list[Reference]:
    """The literal repo-path spellings in TEXT, read with the grammar PATH
    names. Each spelling is normalized; one that cannot be a path is dropped."""
    if is_python_source(path):
        raw: Iterable[Reference] = python_references(text)
    elif is_shell_source(path, text.split("\n", 1)[0]):
        raw = shell_references(text)
    else:
        return []
    out = []
    for ref in raw:
        spelled = normalize(ref.path)
        if spelled is not None:
            out.append(Reference(ref.line, spelled))
    return out


# ── Judgement ────────────────────────────────────────────────────────────────


class Tree(NamedTuple):
    """The tracked paths of one repository."""

    files: frozenset[str]
    dirs: frozenset[str]

    @classmethod
    def of(cls, files: Iterable[str]) -> "Tree":
        tracked = frozenset(files)
        dirs = {
            str(p) for f in tracked for p in PurePosixPath(f).parents if str(p) != "."
        }
        return cls(tracked, frozenset(dirs))

    def holds(self, path: str) -> bool:
        return path in self.files or path in self.dirs


def qualifying(candidate: PathList, tree: Tree) -> tuple[str, ...] | None:
    """CANDIDATE's normalized members when it is a path list, else None."""
    members = tuple(normalize(m) for m in candidate.members)
    if len(members) < MIN_MEMBERS or None in members:
        return None
    if not all(tree.holds(m) for m in members):  # type: ignore[arg-type]
        return None
    if not any(m in tree.dirs for m in members):
        return None
    return members  # type: ignore[return-value]


def picked_directories(members: tuple[str, ...], tree: Tree) -> set[str]:
    """The directories that MEMBERS pick FILES out of one by one: the parent of
    each file member, when the list does not ship that parent whole.

    A directory member does not count. `bin/lib` on a list says "this whole
    tree", and the trees beside it under `bin` are a separate decision, not a
    sibling the author forgot. Measured on the motivating repository, counting
    directory members as picks raised the findings from 3 to 12, and every
    added one named a host-only file."""
    return {
        parent(m)
        for m in members
        if m in tree.files and parent(m) and not covered(parent(m), members)
    }


class Site(NamedTuple):
    """One consumer line that names an omitted path."""

    consumer: str
    line: int


class Omission(NamedTuple):
    """One path that the list omits, with every shipped line that names it."""

    listing: PathList
    path: str
    sites: tuple[Site, ...]


class Consumer(NamedTuple):
    """What the check reads from one shipped file."""

    lines: list[str]
    references: list[Reference]
    comments: dict[int, str]


def judge(
    listing: PathList,
    members: tuple[str, ...],
    tree: Tree,
    read: dict[str, Consumer],
) -> list[Omission]:
    """The omissions of LISTING, whose normalized members are MEMBERS. READ maps
    each shipped consumer to what the check read from it.

    One omission is one finding, however many shipped files name the path. A
    line that carries the opt-out drops out of the finding, and the finding goes
    away when no line is left: the copy needs the path as soon as one line that
    runs there names it."""
    picked = picked_directories(members, tree)
    sites: dict[str, list[Site]] = {}
    for consumer in sorted(f for f in tree.files if covered(f, members)):
        if consumer not in read:
            continue
        lines, refs, comments = read[consumer]
        for ref in refs:
            if parent(ref.path) not in picked or ref.path in picked:
                continue
            if not tree.holds(ref.path) or covered(ref.path, members):
                continue
            if annotated_near(lines, ref.line, OPT_OUT, comments=comments):
                continue
            here = sites.setdefault(ref.path, [])
            if Site(consumer, ref.line) not in here:
                here.append(Site(consumer, ref.line))
    return [Omission(listing, path, tuple(at)) for path, at in sorted(sites.items())]


def _git_files(repo_root: Path) -> list[str]:
    done = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in done.stdout.split("\0") if path]


def _default_repo_root() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _read(path: Path) -> str | None:
    """PATH's text. An unreadable path is skipped: `git ls-files` reports the
    index, so a path can be gone from the disk or be binary."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _consumer(path: str, text: str) -> Consumer | None:
    """What the check reads from the shipped file PATH, or None when PATH is not
    shell or Python."""
    if not (is_python_source(path) or is_shell_source(path, text.split("\n", 1)[0])):
        return None
    return Consumer(
        source_lines(text, path), references(text, path), comment_lines(text, path)
    )


def _refused(path: str, text: str) -> bool:
    """True, with the refusal printed, when the grammar cannot parse TEXT. The
    verdict without that file is not a pass."""
    reason = unreadable_reason(path, text)
    if reason is not None:
        print(f"{path}: {reason}", file=sys.stderr)
    return reason is not None


def find(list_files: Iterable[str], repo_root: Path) -> tuple[list[Omission], bool]:
    """Every omission of every path list in LIST_FILES, and whether a list file
    or a shipped consumer was refused because its grammar could not parse it."""
    tree = Tree.of(_git_files(repo_root))
    root = repo_root.resolve()
    lists = []
    refused: set[str] = set()
    for list_file in list_files:
        text = _read(Path(list_file))
        if text is None:
            continue
        rel = Path(list_file).resolve().relative_to(root).as_posix()
        if is_test_path(rel):
            continue
        if _refused(list_file, text):
            refused.add(rel)
            continue
        for candidate in candidate_lists(text, rel):
            members = qualifying(candidate, tree)
            if members is not None:
                lists.append((candidate, members))
    read: dict[str, Consumer] = {}
    findings = []
    for candidate, members in lists:
        for path in sorted(f for f in tree.files if covered(f, members)):
            if path in read or path in refused:
                continue
            text = _read(repo_root / path)
            if text is None:
                continue
            if _refused(path, text):
                refused.add(path)
                continue
            consumer = _consumer(path, text)
            if consumer is not None:
                read[path] = consumer
        findings.extend(judge(candidate, members, tree, read))
    return findings, bool(refused)


def message(finding: Omission) -> str:
    """The line that reports FINDING, at the list it belongs to."""
    first = finding.sites[0]
    more = len(finding.sites) - 1
    others = f" and {more} more shipped line(s)" if more else ""
    return (
        f"{finding.listing.path}:{finding.listing.line}: `{finding.listing.name}` "
        f"omits `{finding.path}`, which `{first.consumer}:{first.line}`{others} "
        "name. The list ships those files, and it picks other files out of "
        f"`{parent(finding.path)}` by hand, so the shipped copy lacks this one. "
        "Add the path to the list, or derive the list from the code that reads "
        "it. When a line never runs where the list ships, annotate that line "
        f"`# {OPT_OUT}: <reason>`."
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=None,
        metavar="DIR",
        help="the repository whose tracked tree decides what a path names "
        "(default: `git rev-parse --show-toplevel`)",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if not args.files:
        print(
            "check_path_list_closure: no files to scan. This check reads only "
            "the paths you give it, so an empty run would report a clean pass "
            "over nothing.",
            file=sys.stderr,
        )
        print(
            "  to scan the whole tree: git ls-files -z | xargs -0 python -m "
            "ci_truth_serum.check_path_list_closure",
            file=sys.stderr,
        )
        return 2
    repo_root = Path(args.repo_root or _default_repo_root())
    findings, refused = find(args.files, repo_root)
    for finding in findings:
        print(message(finding), file=sys.stderr)
        for site in finding.sites:
            print(f"  named at {site.consumer}:{site.line}", file=sys.stderr)
    return 1 if findings or refused else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
