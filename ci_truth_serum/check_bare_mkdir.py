#!/usr/bin/env python3
"""Flag a bare `mkdir -p` in shell code — its exit status alone proves nothing.

On macOS/BSD, `mkdir -p "$X"` exits 0 even when `$X` is an existing DANGLING
symlink, so trusting its exit status lets a later write into `$X` die
cryptically instead of failing where the real problem is. A caller that needs
`$X` to exist afterward must verify the post-condition (`[[ -d "$X" ]]`), not
just check `mkdir`'s exit code.

A VIOLATION is a `command` word `mkdir` carrying a `-p`-carrying flag word
among its arguments (`-p`, `-pm`, `-m 700 -p`, `--parents`). Plain `mkdir`
without `-p` is not flagged — only `-p`'s dangling-symlink lie needs the
post-condition check. `mkdir` need not be the command's own program name: a
wrapper (`sudo mkdir -p x`, `as_root mkdir -p x`) still counts, since bash
parses the wrapped call as one `command` node whose word list still carries
both `mkdir` and `-p`.

The command is found with tree-sitter-bash (the shared `_cts_bash_ast` grammar),
so `mkdir -p` written in a comment, or as part of a string a command prints
(`gb_warn "use mkdir -p here"`), never becomes a `command` node and is never
flagged.

A `mkdir -p` is not flagged when the SAME directory operand is checked with
`[[ -d "$X" ]]`, `[ -d "$X" ]`, or `test -d "$X"` in its own statement or the
next one — the post-condition the docstring above tells authors to write. The
window is exactly one statement past the create's own, because every real
instance in this repo puts the check on the very next line; a wider window
would risk crediting an unrelated later check of the same path (guarding a
DIFFERENT later use) as if it covered this create. A check on a different
path, or one further than one statement away, still leaves the create flagged.

Opt out with `# bare-mkdir-ok: <reason>` trailing the `mkdir` line (or on the
line above) for a create that is never verified and is not meant to be.

Known blind spot: a `mkdir -p` embedded in a string handed to a nested shell
(`bash -c "mkdir -p \"$x\""`, `sudo -n bash -c '...'`) is not parsed as a
second bash program, so it is not seen — this check reads one grammar's worth
of commands, not the text a command's argument happens to spell.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_words,
    iter_nodes,
    node_text,
    parse,
    unquote,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    run_line_checks,
)

OPT_OUT = "bare-mkdir-ok"

# A flag word carrying `p` (`-p`, `-pm`, `-mp`, `--parents`) on its own, as a
# whole argument word — never a substring of a longer word.
_P_FLAG_RE = re.compile(r"^(?:-[A-Za-z]*p[A-Za-z]*|--parents)$")

# `mkdir`'s only flag that consumes the next word as a value (`-m 700`,
# `--mode 700`), so that word is not misread as a second directory operand.
_MKDIR_VALUE_FLAGS = frozenset({"-m", "--mode"})

# Block-like nodes whose direct children are the statement sequence a
# "next statement" window counts over — a function body and a subshell each
# scope their own sequence, so a create inside one is windowed against its own
# siblings, not the top-level statements around the function/subshell itself.
_BLOCK_TYPES = frozenset({"program", "compound_statement", "subshell", "do_group"})

# Statements after the create's own to search for its post-condition test. See
# the module docstring for why this is exactly one.
_LOOKAHEAD = 1

MESSAGE = (
    "bare `mkdir -p` — on macOS/BSD its exit status is 0 even over a dangling "
    "symlink, so a later write dies cryptically. Verify the post-condition "
    f'(`[[ -d "$X" ]]`), or annotate `# {OPT_OUT}: <reason>`.'
)


def _is_bare_mkdir_p(command_node) -> bool:
    """True when COMMAND_NODE's word list carries `mkdir` followed later by a
    `-p`-carrying flag word. Command boundaries (`;`, `|`, `&&`) are already
    separate `command` nodes in the grammar, so no separator handling is
    needed here — a flag after the boundary is a different node entirely."""
    words = [unquote(word) for word in command_words(command_node)]
    try:
        start = words.index("mkdir")
    except ValueError:
        return False
    return any(_P_FLAG_RE.match(word) for word in words[start + 1 :])


def _mkdir_targets(command_node) -> list[str]:
    """The directory operands COMMAND_NODE's `mkdir` creates: every positional
    word after `mkdir` itself, with flags — and an `-m`/`--mode` flag's value —
    skipped."""
    words = [unquote(word) for word in command_words(command_node)]
    try:
        start = words.index("mkdir")
    except ValueError:
        return []
    targets: list[str] = []
    skip_value = False
    for word in words[start + 1 :]:
        if skip_value:
            skip_value = False
            continue
        if word in _MKDIR_VALUE_FLAGS:
            skip_value = True
            continue
        if word.startswith("-"):
            continue
        targets.append(word)
    return targets


def _dir_test_targets(node) -> list[str]:
    """The directory operand NODE checks with `-d` — a `[[ -d "$X" ]]` / `[ -d
    "$X" ]` `test_command`'s `unary_expression`, or a `test -d "$X"` `command`
    — else empty. A negated test (`[[ ! -d "$X" ]]`) does not match: its
    `unary_expression`'s first child is `!`, not `-d`."""
    if node.type == "test_command":
        for child in node.children:
            if (
                child.type == "unary_expression"
                and len(child.children) >= 2
                and node_text(child.children[0]) == "-d"
            ):
                return [unquote(node_text(child.children[1]))]
        return []
    if node.type == "command":
        words = [unquote(word) for word in command_words(node)]
        head = words[0].rsplit("/", 1)[-1] if words else ""
        if head == "test" and len(words) >= 3 and words[1] == "-d":
            return [words[2]]
    return []


def _statement(node):
    """The direct-child statement of the nearest enclosing block NODE belongs
    to — a `command` glued into `a && b` climbs to the whole `list`, but stops
    climbing at a function body, subshell, or loop body (`_BLOCK_TYPES`), so a
    create's window is the statements sharing ITS scope, not the ones around
    the function/subshell that contains it."""
    while node.parent is not None and node.parent.type not in _BLOCK_TYPES:
        node = node.parent
    return node


def _window(create):
    """CREATE's own statement and the next `_LOOKAHEAD`, within the block that
    directly encloses it."""
    statement = _statement(create)
    block = statement.parent
    if block is None:
        return [statement]
    siblings = [child for child in block.children if child.is_named]
    index = next(
        (i for i, node in enumerate(siblings) if node.id == statement.id), None
    )
    return [] if index is None else siblings[index : index + 1 + _LOOKAHEAD]


def _verified_nearby(create, targets: set[str]) -> bool:
    """True when a `-d` test on one of CREATE's TARGETS follows CREATE within
    its window (see `_window`) — the post-condition check that makes this
    `mkdir -p` safe against the dangling-symlink lie."""
    return any(
        node.start_byte > create.start_byte and set(_dir_test_targets(node)) & targets
        for statement in _window(create)
        for node in iter_nodes(statement, "test_command", "command")
    )


def violations(text: str) -> list[int]:
    """1-based line numbers of unexempted bare `mkdir -p` invocations —
    a `mkdir -p` whose directory is verified nearby (`_verified_nearby`) is not
    one."""
    physical = text.splitlines()
    hits: set[int] = set()
    for command_node in iter_nodes(parse(text), "command"):
        if not _is_bare_mkdir_p(command_node):
            continue
        targets = set(_mkdir_targets(command_node))
        if targets and _verified_nearby(command_node, targets):
            continue
        lineno = command_node.start_point[0] + 1
        end_line = command_node.end_point[0] + 1
        if annotated_near(physical, lineno, OPT_OUT, span_end=end_line):
            continue
        hits.add(lineno)
    return sorted(hits)


def main(argv: list[str]) -> int:
    """Run the detector one path at a time, so a file the grammar refuses to
    parse fails LOUDLY (naming the path, exit 1) instead of silently vanishing
    from the scan, while every other path is still checked."""
    status = 0
    for path in argv:
        try:
            status = max(status, run_line_checks([path], violations, MESSAGE))
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
