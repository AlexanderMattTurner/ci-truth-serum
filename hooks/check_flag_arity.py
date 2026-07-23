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

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_bash.language())
_PARSER = Parser(_LANGUAGE)

# Helpers that themselves assert `[[ $# -ge 2 ]]` before returning — calling one
# at the top of an arm is an accepted guard. A small named allowlist, not a
# pattern, so a new helper is a deliberate one-line addition here.
ALLOWLISTED_HELPERS = ("need_val", "need_arg")

_OPTOUT_RE = re.compile(r"#\s*flag-arity-ok:(?P<reason>.*)$")

# A case-arm label alternative is a flag when it is a single `-x` / `--xxx`
# option, optionally a `--xxx=*` glob. `doctor)`, `*)`, `read)` and
# quoted/globbed data labels fail this and are skipped.
_FLAG_ALT_RE = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*(?:=\*)?$")

# A command name that ABORTS the arm/loop/script before the read is reached — the
# consequent that makes an arity test an actual guard rather than a discarded
# boolean.
_EXIT_NAMES = frozenset(
    {
        "die",
        "exit",
        "return",
        "usage",
        "fatal",
        "abort",
        "bail",
        "fail",
        "error",
        "err",
        "continue",
        "break",
    }
)

# Shell BUILTIN aborts — always trusted to stop execution, never shadowed by a
# repo's own function. The remaining `_EXIT_NAMES` are CONVENTIONAL bail names that
# a script may define itself; a locally-defined one is trusted only if its body
# actually aborts (see `_trusted_aborts`), else a pure-`printf` `error()` returning
# 0 would falsely read as a guard.
_BUILTIN_ABORTS = frozenset({"exit", "return", "continue", "break"})

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

_MSG_EMPTY_OPTOUT = (
    "flag-arity-ok opt-out needs a non-empty reason (# flag-arity-ok: <why>)"
)


def _msg_unguarded(required: int) -> str:
    """The finding message for an arm that reads ``$required`` / ``shift required``
    without an arity guard proving at least that many args remain. The required
    index is spelled out so a guard proving a SMALLER arity (``$# -ge 2`` before a
    ``$3`` read) is clearly insufficient."""
    return (
        "value flag reads $%d/shift %d without an arity guard proving $# >= %d — "
        "add '[[ $# -ge %d ]] || die ...' or '${%d:?...}'"
    ) % (required, required, required, required, required)


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


def _true_lower_bound(op: str, n: int) -> int | None:
    """The lower bound on ``$#`` guaranteed when ``$# <op> n`` is TRUE, or None if
    the true branch yields only an upper bound (`-lt`/`-le`)."""
    if op in ("-ge", ">=", "-eq", "=="):
        return n
    if op in ("-gt", ">"):
        return n + 1
    return None


def _false_lower_bound(op: str, n: int) -> int | None:
    """The lower bound on ``$#`` guaranteed when ``$# <op> n`` is FALSE (the
    proceed path of a `then`-bailing guard), or None."""
    if op in ("-lt", "<"):
        return n
    if op in ("-le", "<="):
        return n + 1
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


def _binexpr_bound(binexpr: Node, when_true: bool) -> int | None:
    """The lower bound on ``$#`` a `$# <op> <number>` comparison proves on its
    WHEN_TRUE / when-false branch (either operand order), or None if BINEXPR is not
    a `$#`-vs-literal arity test."""
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
    if n is None:
        return None
    return _true_lower_bound(op, n) if when_true else _false_lower_bound(op, n)


def _arity_bound(node: Node, when_true: bool) -> int | None:
    """The bound of the first `$#`-vs-number comparison anywhere under NODE on its
    WHEN_TRUE / when-false branch, or None."""
    for n in _walk(node):
        if n.type == "binary_expression":
            bound = _binexpr_bound(n, when_true)
            if bound is not None:
                return bound
    return None


def _has_exit(node: Node, trusted: frozenset[str]) -> bool:
    """True if NODE runs a command that aborts — a name in TRUSTED (the abort
    commands that genuinely stop execution in this script). This is the bail that
    turns an arity test into a real guard."""
    return any(c.type == "command_name" and _text(c) in trusted for c in _walk(node))


def _list_bound(node: Node, trusted: frozenset[str]) -> int | None:
    """The lower bound on ``$#`` proven by a bailing `||`/`&&` arity list, or None.
    `[[ $# -ge 2 ]] || die` proves the test's TRUE-branch bound (proceed on success);
    `[[ $# -lt 2 ]] && die` proves its FALSE-branch bound (proceed when not-less)."""
    if not _has_exit(node, trusted):
        return None
    ops = {c.type for c in node.children}
    if "||" in ops:
        return _arity_bound(node, when_true=True)
    if "&&" in ops:
        return _arity_bound(node, when_true=False)
    return None


def _then_body_has_exit(if_node: Node, trusted: frozenset[str]) -> bool:
    in_then = False
    for c in if_node.children:
        if c.type == "then":
            in_then = True
            continue
        if c.type in ("else_clause", "elif_clause", "fi"):
            break
        if in_then and _has_exit(c, trusted):
            return True
    return False


def _else_has_exit(if_node: Node, trusted: frozenset[str]) -> bool:
    return any(
        c.type in ("else_clause", "elif_clause") and _has_exit(c, trusted)
        for c in if_node.children
    )


def _if_bound(node: Node, trusted: frozenset[str]) -> int | None:
    """The lower bound proven after an `if [[ $# … ]]; then die; fi` (then-body
    bails, proceed on the test being FALSE) or its `else`-bailing mirror (proceed on
    TRUE), or None if NODE is not such a guard."""
    cond = next(
        (
            c
            for c in node.children
            if c.type in ("test_command", "command", "compound_statement", "list")
        ),
        None,
    )
    if cond is None:
        return None
    if _then_body_has_exit(node, trusted):
        bound = _arity_bound(cond, when_true=False)
        if bound is not None:
            return bound
    if _else_has_exit(node, trusted):
        return _arity_bound(cond, when_true=True)
    return None


def _self_guard_bound(node: Node) -> int | None:
    """The highest index N of a `${N:?…}` / `${N:-…}` / `${N:=…}` / `${N:+…}`
    self-guarding read (N>=2) under NODE — the highest positional the author has
    explicitly DEFENDED (an abort-if-unset, a default, or an alternate) — or None.

    A defended index N clears reads up to N (the author handled arity that far), but
    a LATER read at a higher index is still flagged: `C="${2:-x}"; D="$3"` defends
    only index 2, so the raw `$3` is caught."""
    bounds = [
        int(_text(var))
        for n in _walk(node)
        if n.type == "expansion"
        and any(c.type in _SELF_GUARD_OPS for c in n.children)
        and (var := _first_child(n, "variable_name")) is not None
        and _text(var).isdigit()
        and int(_text(var)) >= 2
    ]
    return max(bounds) if bounds else None


def _is_helper(node: Node) -> bool:
    if node.type != "command":
        return False
    name = _first_child(node, "command_name")
    return name is not None and _text(name) in ALLOWLISTED_HELPERS


def _statement_bound(stmt: Node, trusted: frozenset[str]) -> int | None:
    """The lower bound on ``$#`` that STMT proves (and bails otherwise) for the code
    that follows it — a bailing `||`/`&&` list, a bailing `if`, a `${N:?}`
    self-guard, or an allowlisted helper (assumed to assert `$# >= 2`). None when
    STMT establishes no arity, in which case its own reads are scanned."""
    if stmt.type == "list":
        return _list_bound(stmt, trusted)
    if stmt.type == "if_statement":
        return _if_bound(stmt, trusted)
    bound = _self_guard_bound(stmt)
    if bound is not None:
        return bound
    return 2 if _is_helper(stmt) else None


def _reads(stmt: Node):
    """Yield ``(node, required)`` for every UNGUARDED positional read past $1 in
    STMT — a bare `$2`..`$9`, a plain `${N}` with N>=2, or `shift N` with N>=2 —
    where REQUIRED is the number of args the read needs present. A self-guarding
    `${N:?…}`/`${N:-…}` is not a raw read. Nested `case` statements are pruned so an
    inner arm's reads are never attributed to this arm."""
    for n in _walk(stmt, prune=("case_statement",)):
        if n.type == "simple_expansion":
            var = _first_child(n, "variable_name")
            if var is not None and _text(var).isdigit() and int(_text(var)) >= 2:
                yield n, int(_text(var))
        elif n.type == "expansion":
            # A plain `${N}` is exactly `${`, the name, and `}` — anything else
            # (an operator, a substring, an array index) is not a bare read.
            if [c.type for c in n.children] == ["${", "variable_name", "}"]:
                var = _first_child(n, "variable_name")
                if _text(var).isdigit() and int(_text(var)) >= 2:
                    yield n, int(_text(var))
        elif n.type == "command":
            name = _first_child(n, "command_name")
            if name is not None and _text(name) == "shift":
                num = _first_child(n, "number")
                if num is not None and _text(num).isdigit() and int(_text(num)) >= 2:
                    yield n, int(_text(num))


def _first_unsatisfied_read(stmt: Node, established: int) -> tuple[Node, int] | None:
    """The earliest (by source position) raw read in STMT whose required arity
    exceeds ESTABLISHED — the read a prior guard has NOT proven safe. None when
    every read is already covered."""
    unsatisfied = [(n, req) for n, req in _reads(stmt) if req > established]
    return min(unsatisfied, key=lambda nr: nr[0].start_byte) if unsatisfied else None


def _local_funcdefs(root: Node) -> dict[str, Node]:
    """Map each locally-defined function name to its body node (first definition
    wins), so a conventional bail name that a script defines itself can be checked
    for whether it actually aborts."""
    defs: dict[str, Node] = {}
    for n in _walk(root):
        if n.type != "function_definition":
            continue
        name = _first_child(n, "word")
        body = _first_child(n, "compound_statement")
        if name is not None and body is not None:
            defs.setdefault(_text(name), body)
    return defs


def _body_aborts(body: Node) -> bool:
    """True if BODY contains an abort command (a builtin or a conventional bail
    name) — the test of whether a locally-defined `error()`/`fail()` actually stops
    execution rather than merely warning and returning 0."""
    return any(
        c.type == "command_name" and _text(c) in _EXIT_NAMES for c in _walk(body)
    )


def _trusted_aborts(root: Node) -> frozenset[str]:
    """The abort-command names that genuinely stop the arm/loop/script in THIS
    script. Builtins and unknown/external conventional names (`die`, `fatal`, …) are
    trusted; a conventional name that is a LOCALLY-defined function whose body never
    aborts (a pure `printf`/`echo` helper returning 0) is NOT — trusting it would
    treat `[[ $# -ge 2 ]] || warn_only` as a guard and miss the later read (C11)."""
    defs = _local_funcdefs(root)
    trusted = set(_BUILTIN_ABORTS)
    for name in _EXIT_NAMES - _BUILTIN_ABORTS:
        body = defs.get(name)
        if body is None or _body_aborts(body):
            trusted.add(name)
    return frozenset(trusted)


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
    found: list[tuple[int, str]],
    trusted: frozenset[str],
) -> None:
    """Record at most one violation for a flag-labelled arm: the first positional
    read whose required arity a preceding guard has not proven.

    Walks the arm's statements in order, tracking ESTABLISHED — the greatest lower
    bound on ``$#`` proven so far. A guard statement raises that bound (and its own
    conditionally-guarded reads are not scanned); a non-guard statement's reads are
    each checked against it, so `[[ $# -ge 2 ]] || die` clears a later `$2` but not
    a later `$3` (C9)."""
    if not _label_is_flag(case_item):
        return
    established = 0
    for stmt in _body_statements(case_item):
        if stmt.type == "case_statement":
            continue  # a nested case is scanned as its own arms
        bound = _statement_bound(stmt, trusted)
        if bound is not None:
            established = max(established, bound)
            continue
        read = _first_unsatisfied_read(stmt, established)
        if read is None:
            continue
        node, required = read
        lineno = node.start_point[0] + 1
        marker = _optout(lines, lineno)
        if marker is not None:
            if not marker.group("reason").strip():
                found.append((lineno, _MSG_EMPTY_OPTOUT))
            return  # a marker resolves the arm either way
        found.append((lineno, _msg_unguarded(required)))
        return


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for every value-taking flag arm in TEXT that reads a
    positional past $1 without a guard proving that many args remain. One report
    per arm."""
    tree = _PARSER.parse(text.encode("utf-8"))
    root = tree.root_node
    trusted = _trusted_aborts(root)
    lines = text.split("\n")
    found: list[tuple[int, str]] = []
    for node in _walk(root):
        if node.type == "case_item":
            _scan_arm(node, lines, found, trusted)
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
