#!/usr/bin/env python3
"""Fail a shell script whose CLI flag parser consumes a value without first
proving the value exists.

The bug this guards: a ``case "$1" in`` arm labelled with a value-taking flag
reads ``$2`` / does ``shift 2`` while relying only on the loop's outer
``while [[ $# -gt 0 ]]``. That outer guard proves $1 exists, not $2 — so
``--branch`` passed as the FINAL argument makes ``$2`` unbound and, under
``set -u``, the parser dies with a raw ``$2: unbound variable`` instead of a
clean "--branch needs a value".

A value-consuming flag arm must carry its own arity guard BEFORE the read, and the
guard must actually BAIL when the value is missing::

    [[ $# -ge 2 ]] || die "--branch needs a value"   # or -gt 1 / (( $# >= 2 ))
    BRANCH="${2:?--branch needs a value}"            # self-guarding read
    need_val "$@"                                     # an allowlisted helper

Two failure modes this closes: an arity test whose result is DISCARDED
(``[[ $# -ge 2 ]]`` with no ``|| die`` / ``&& die`` / ``then die`` consequent does
not stop the read), and an arity guard that runs AFTER the read
(``X="$2"; [[ $# -ge 2 ]] || die`` still dereferences ``$2`` raw first). Both are
flagged; only a guard that bails and precedes the read passes.

The bail may span lines — a multi-line ``if [[ $# -lt 2 ]]; then … die … fi`` (the
common idiom) or a ``[[ $# -ge 2 ]] ||`` whose exiting command sits on the
continuation line — is recognized: tree-sitter parses each ``||``-list and
``if``-statement as one node regardless of where the newlines fall. A multi-line
``if`` whose body never exits (only warns) is NOT a guard, so the later read is
still flagged.

Scope is deliberately narrow to keep false positives at zero: only arms whose
LABEL is one or more ``-x`` / ``--xxx`` / ``--xxx=*`` options fire the check.
Subcommand dispatch (``read)``, ``write)``), catch-alls (``*)``), and value
reads inside ordinary function bodies (``local x="$1"; shift 2``) are never
flags, so they are excluded by construction.

The parse is done by tree-sitter-bash, so ``$#`` is never confused with a
comment, string contents are never mistaken for code, and arm/statement
boundaries come from the grammar rather than hand-rolled scanning.

Invoked by pre-commit with the changed shell files as arguments; ``--all`` walks
the whole tracked shell surface. Exits non-zero on any violation.
"""

import re
import subprocess
import sys
from pathlib import Path

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import parse  # noqa: E402,I001  # pylint: disable=wrong-import-position

# Helpers that themselves assert `[[ $# -ge 2 ]]` before returning — calling one
# at the top of an arm is an accepted guard. A small named allowlist, not a
# pattern, so a new helper is a deliberate one-line addition here.
ALLOWLISTED_HELPERS = ("need_val", "need_arg")

_OPTOUT_RE = re.compile(r"#\s*flag-arity-ok:(?P<reason>.*)$")

# A case-arm label alternative is a flag when it is a single `-x` / `--xxx`
# option, optionally a `--xxx=*` glob. `doctor)`, `*)`, `read)` and
# quoted/globbed data labels fail this and are skipped.
_FLAG_ALT_RE = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*(?:=\*)?$")

# Shell control transfers that ALWAYS abort the arm/loop/script — the consequent
# that makes an arity test an actual guard rather than a discarded boolean.
_BUILTIN_EXITS = frozenset({"exit", "return", "continue", "break"})

# Conventional names for sourced/external abort helpers. These are trusted by
# name ONLY when the file does not define them: a function DEFINED in the file
# is resolved against its actual body (see `exit_names`) — a local `fail() {
# echo "oops"; }` that merely narrates does not stop the read that follows, so
# its name must not clear the guard.
_CONVENTIONAL_EXITS = frozenset(
    {"die", "usage", "fatal", "abort", "bail", "fail", "error", "err"}
)

# `${2:?…}` / `${2:-…}` / `${2:=…}` / `${2:+…}`: a self-guarding read. tree-sitter
# names the operator token by its literal text.
_SELF_GUARD_OPS = frozenset({":?", ":-", ":=", ":+"})

# `$#`-vs-number comparison operators, `[[ ]]` (`-ge`) and `(( ))` (`>=`) spellings.
_ARITY_OPS = frozenset({"-ge", "-gt", "-eq", "-lt", "-le", ">=", ">", "==", "<", "<="})
_FLIP = {
    "-lt": "-gt",
    "-gt": "-lt",
    "-le": "-ge",
    "-ge": "-le",
    "<": ">",
    ">": "<",
    "<=": ">=",
    ">=": "<=",
}

_MSG_UNGUARDED = (
    "value flag consumes $2/shift without an arity guard — "
    "add '[[ $# -ge 2 ]] || die ...' or '${2:?...}'"
)
_MSG_EMPTY_OPTOUT = (
    "flag-arity-ok opt-out needs a non-empty reason (# flag-arity-ok: <why>)"
)


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def _walk(node: Node, prune: tuple[str, ...] = ()):
    """Pre-order traversal, skipping any subtree whose root type is in PRUNE."""
    yield node
    for child in node.children:
        if child.type in prune:
            continue
        yield from _walk(child, prune)


def _first_child(node: Node, type_: str) -> Node | None:
    for child in node.children:
        if child.type == type_:
            return child
    return None


def _bound(op: str, n: int) -> tuple[str, int] | None:
    """("pos", b): comparison success proves $# >= b. ("neg", b): the comparison
    is true exactly when fewer than b args remain, so BAILING on true proves
    $# >= b for the code after it. None: not an arity shape."""
    if op in ("-ge", ">=", "-eq", "=="):
        return ("pos", n)
    if op in ("-gt", ">"):
        return ("pos", n + 1)
    if op in ("-lt", "<"):
        return ("neg", n)
    if op in ("-le", "<="):
        return ("neg", n + 1)
    return None


def _has_hash(nodes: list[Node]) -> bool:
    return any(
        x.type == "special_variable_name" and _text(x) == "#"
        for n in nodes
        for x in _walk(n)
    )


def _first_number(nodes: list[Node]) -> int | None:
    for n in nodes:
        for x in _walk(n):
            if x.type == "number" and _text(x).isdigit():
                return int(_text(x))
    return None


def _binexpr_bound(binexpr: Node) -> tuple[str, int] | None:
    """Polarity and proven bound of a `$# <op> <number>` comparison (either
    operand order), or None if BINEXPR is not a `$#`-vs-literal arity test."""
    kids = binexpr.children
    op_idx = next((i for i, c in enumerate(kids) if _text(c) in _ARITY_OPS), None)
    if op_idx is None:
        return None
    op = _text(kids[op_idx])
    left, right = kids[:op_idx], kids[op_idx + 1 :]
    if _has_hash(left):
        n = _first_number(right)
    elif _has_hash(right):
        n, op = _first_number(left), _FLIP.get(op, op)
    else:
        return None
    return _bound(op, n) if n is not None else None


def _arity_bound(node: Node) -> tuple[str, int] | None:
    """Polarity/bound of the first `$#`-vs-number comparison under NODE, or None."""
    for n in _walk(node):
        if n.type == "binary_expression":
            found = _binexpr_bound(n)
            if found is not None:
                return found
    return None


def _function_bodies(root: Node) -> dict[str, Node]:
    """Map each function DEFINED in the file to its body node."""
    out: dict[str, Node] = {}
    for node in _walk(root):
        if node.type != "function_definition" or not node.children:
            continue
        name = _first_child(node, "word")
        if name is not None:
            out[_text(name)] = node.children[-1]
    return out


def exit_names(root: Node) -> frozenset[str]:
    """The command names that abort when run, resolved against the file itself.

    Builtin control transfers always count. A function defined in the file
    counts only when its body (transitively, to a fixed point) runs an aborting
    command — the name alone proves nothing. An UNdefined conventional helper
    name (`die`, `fatal`, …) is trusted: it is sourced from elsewhere and the
    naming convention is the only signal available."""
    bodies = _function_bodies(root)
    known = set(_BUILTIN_EXITS) | (_CONVENTIONAL_EXITS - bodies.keys())
    changed = True
    while changed:
        changed = False
        for name, body in bodies.items():
            if name in known:
                continue
            if any(c.type == "command_name" and _text(c) in known for c in _walk(body)):
                known.add(name)
                changed = True
    return frozenset(known)


def _has_exit(node: Node, exits: frozenset[str]) -> bool:
    """True if NODE runs a command that aborts — the bail that turns an arity
    test into a real guard. EXITS is the file's resolved `exit_names`."""
    return any(c.type == "command_name" and _text(c) in exits for c in _walk(node))


def _list_guards(node: Node, exits: frozenset[str]) -> int | None:
    """The bound proven by `[[ $# -ge N ]] || die` (positive test bailing on
    failure) or `[[ $# -lt N ]] && die` (negative test bailing when true), or
    None when NODE is no such guard."""
    found = _arity_bound(node)
    if found is None or not _has_exit(node, exits):
        return None
    pol, bound = found
    ops = {c.type for c in node.children}
    if (pol == "pos" and "||" in ops) or (pol == "neg" and "&&" in ops):
        return bound
    return None


def _then_body_has_exit(if_node: Node, exits: frozenset[str]) -> bool:
    in_then = False
    for c in if_node.children:
        if c.type == "then":
            in_then = True
            continue
        if c.type in ("else_clause", "elif_clause", "fi"):
            break
        if in_then and _has_exit(c, exits):
            return True
    return False


def _else_has_exit(if_node: Node, exits: frozenset[str]) -> bool:
    return any(
        c.type in ("else_clause", "elif_clause") and _has_exit(c, exits)
        for c in if_node.children
    )


def _if_guards(node: Node, exits: frozenset[str]) -> int | None:
    """The bound proven by `if [[ $# -lt N ]]; then die; fi` (negative test,
    then-body bails) or the positive mirror with the bail in the `else`; None
    when NODE is no such guard."""
    cond = next(
        (
            c
            for c in node.children
            if c.type in ("test_command", "command", "compound_statement", "list")
        ),
        None,
    )
    found = _arity_bound(cond) if cond is not None else None
    if found is None:
        return None
    pol, bound = found
    if pol == "neg" and _then_body_has_exit(node, exits):
        return bound
    if pol == "pos" and _else_has_exit(node, exits):
        return bound
    return None


def _self_guard_bound(node: Node) -> int:
    """The highest positional PROVEN to exist by a `${N:?…}` read under NODE
    (the script dies otherwise), or 0. A defaulting `${N:-…}`/`${N:=…}`/`${N:+…}`
    read is safe for ITSELF (it is never a raw read) but proves nothing about
    $#, so it earns no bound — a later raw `$N` still owes its own guard."""
    best = 0
    for n in _walk(node):
        if n.type != "expansion":
            continue
        var = _first_child(n, "variable_name")
        if var is None or not _text(var).isdigit():
            continue
        if any(c.type == ":?" for c in n.children):
            best = max(best, int(_text(var)))
    return best


def _is_helper(node: Node) -> bool:
    if node.type != "command":
        return False
    name = _first_child(node, "command_name")
    return name is not None and _text(name) in ALLOWLISTED_HELPERS


def _statement_guard_bound(stmt: Node, exits: frozenset[str]) -> int | None:
    """The arg count STMT proves remains (bailing otherwise) — a bailing
    `||`/`&&` list or a bailing `if` — or None when STMT is no such guard.
    (Self-guarding `${N:?}` reads and allowlisted helpers are credited by the
    caller, which must still scan the same statement for raw reads.)"""
    if stmt.type == "list":
        return _list_guards(stmt, exits)
    if stmt.type == "if_statement":
        return _if_guards(stmt, exits)
    return None


def _positional_reads(stmt: Node) -> list[tuple[Node, int]]:
    """(node, required $#) for every positional read in STMT, in source order —
    a bare `$N`, a plain `${N}`, or `shift N` (which errors when fewer than N
    args remain). A self-guarding `${N:?…}` / defaulting `${N:-…}` is not a raw
    read. Nested `case` statements are pruned so an inner arm's reads are never
    attributed to this arm."""
    reads: list[tuple[Node, int]] = []
    for n in _walk(stmt, prune=("case_statement",)):
        if n.type == "simple_expansion":
            var = _first_child(n, "variable_name")
            if var is not None and _text(var).isdigit():
                reads.append((n, int(_text(var))))
        elif n.type == "expansion":
            # A plain `${N}` is exactly `${`, the name, and `}` — anything else
            # (an operator, a substring, an array index) is not a bare read.
            if [c.type for c in n.children] == ["${", "variable_name", "}"]:
                var = _first_child(n, "variable_name")
                if _text(var).isdigit():
                    reads.append((n, int(_text(var))))
        elif n.type == "command":
            name = _first_child(n, "command_name")
            if name is not None and _text(name) == "shift":
                num = _first_child(n, "number")
                amount = (
                    int(_text(num)) if num is not None and _text(num).isdigit() else 1
                )
                reads.append((n, amount))
    reads.sort(key=lambda pair: pair[0].start_byte)
    return reads


def _shift_amount(stmt: Node) -> int:
    """Total positions the statement shifts away (`shift` = 1, `shift N` = N),
    so the proven bound can be decremented after the statement runs."""
    total = 0
    for n in _walk(stmt, prune=("case_statement",)):
        if n.type == "command":
            name = _first_child(n, "command_name")
            if name is not None and _text(name) == "shift":
                num = _first_child(n, "number")
                total += (
                    int(_text(num)) if num is not None and _text(num).isdigit() else 1
                )
    return total


def _label_is_flag(case_item: Node) -> bool:
    """True if EVERY label alternative before the `)` is a `-x`/`--xxx`/`--xxx=*`
    option. `doctor)`, `*)`, and quoted data labels fail and are skipped."""
    alts: list[str] = []
    for child in case_item.children:
        if child.type == ")":
            break
        if child.type == "|":
            continue
        alts.append(_text(child).strip())
    return bool(alts) and all(_FLAG_ALT_RE.match(a) for a in alts)


def _body_statements(case_item: Node):
    """The arm's body statement nodes, in source order — the children after the
    `)` label terminator, skipping the arm terminator and comments."""
    seen_paren = False
    skip = {")", ";;", ";&", ";;&", "comment"}
    for child in case_item.children:
        if not seen_paren:
            seen_paren = child.type == ")"
            continue
        if child.type in skip:
            continue
        yield child


def _optout(lines: list[str], lineno: int) -> "re.Match[str] | None":
    """A `# flag-arity-ok:` marker on the read's own line or the line above it."""
    cur = lines[lineno - 1] if 0 <= lineno - 1 < len(lines) else ""
    prev = lines[lineno - 2] if lineno - 2 >= 0 else ""
    return _OPTOUT_RE.search(cur) or _OPTOUT_RE.search(prev)


def _scan_arm(
    case_item: Node,
    lines: list[str],
    exits: frozenset[str],
    found: list[tuple[int, str]],
) -> None:
    """Record at most one violation for a flag-labelled arm: the first positional
    read whose requirement exceeds what the guards so far have PROVEN.

    The proven bound starts at 1 (the parse loop's `while [[ $# -gt 0 ]]`) and
    rises with each bailing arity guard / `${N:?}` read / allowlisted helper —
    tracked as a NUMBER, so a `[[ $# -ge 2 ]] || die` guard does not clear an
    arm that goes on to read `$3` (the guard proves two args, the read needs
    three). A `shift N` lowers the bound by N: positions past the shift point
    are unproven again."""
    if not _label_is_flag(case_item):
        return
    proven = 1
    for stmt in _body_statements(case_item):
        if stmt.type == "case_statement":
            continue  # a nested case is scanned as its own arms
        proven = max(proven, _self_guard_bound(stmt))
        if _is_helper(stmt):
            proven = max(proven, 2)
            continue
        guard = _statement_guard_bound(stmt, exits)
        if guard is not None:
            proven = max(proven, guard)
            continue
        for read, required in _positional_reads(stmt):
            if required <= proven:
                continue
            lineno = read.start_point[0] + 1
            marker = _optout(lines, lineno)
            if marker is not None:
                if not marker.group("reason").strip():
                    found.append((lineno, _MSG_EMPTY_OPTOUT))
                return  # a marker resolves the arm either way
            found.append((lineno, _MSG_UNGUARDED))
            return
        proven = max(1, proven - _shift_amount(stmt))


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for every value-taking flag arm in TEXT that
    reads a positional (``$N`` / ``shift N``) beyond what its arity guards
    prove exists. One report per arm."""
    root = parse(text)
    lines = text.split("\n")
    exits = exit_names(root)
    found: list[tuple[int, str]] = []
    for node in _walk(root):
        if node.type == "case_item":
            _scan_arm(node, lines, exits, found)
    found.sort()
    return found


def _shell_files() -> list[str]:
    """Tracked *.sh / *.bash files, plus tracked extensionless files whose
    shebang names bash/sh (git hooks under .hooks/, bin scripts)."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    out: list[str] = []
    for f in tracked:
        if not f:
            continue
        if re.search(r"\.(?:sh|bash)$", f):
            out.append(f)
            continue
        base = f.rsplit("/", 1)[-1]
        if "." in base:
            continue
        try:
            first = Path(f).read_text(encoding="utf-8").split("\n", 1)[0]
        except (OSError, UnicodeDecodeError):
            continue
        if re.match(r"^#!.*\b(?:bash|sh)\b", first):
            out.append(f)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    files = (
        _shell_files()
        if "--all" in argv
        else [a for a in argv if not a.startswith("--")]
    )

    rc = 0
    for path in files:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        for line_no, message in violations(text):
            print(f"{path}:{line_no}: {message}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
