#!/usr/bin/env python3
"""Ban ``flock <number>`` — a lock taken on a hardcoded file descriptor.

``flock`` has two operand forms. Given a PATH it opens the file itself, and the
descriptor is its own business. Given a NUMBER it locks a descriptor the caller
already opened, and that number is now a name shared with every process in the
same descriptor table::

    exec 9>/var/lock/deploy
    flock -x 9                     # locks whatever fd 9 is, right now

SCOPE. The pair above is the DOCUMENTED util-linux idiom and this rule does not
judge it: a file that opens the descriptor itself, with an ``exec 9>``, owns
that number for the rest of the script, and separate processes still contend on
the same lock. What this rule reports is a `flock <number>` whose file never
opens that number::

    flock -x 200                   # who opened 200? not this script

Two things then go wrong, and neither one leaves a red check.

The first is an ABORT. When the descriptor is not open, ``flock`` exits
non-zero, and a ``set -e`` caller dies at a line that reads like a lock
acquisition rather than a missing redirect.

The second is a COLLISION. The number is a name shared with every process in
the same descriptor table, so the lock now depends on a caller, a test harness
or a CI wrapper having opened that exact number and nothing else having reused
it. When something else holds it, ``flock`` locks the wrong file: two runs both
proceed, and the job reports success while the lock guarded nothing.

The fix is to open the descriptor in the file that locks it, and to let the
shell allocate the number (bash 4.1 and later)::

    exec {lock_fd}>/var/lock/deploy
    flock -x "$lock_fd"

The shell picks a number no-one is using, so no caller can collide with it.
An `exec 9>FILE` in the same file passes too.

Only a LITERAL number is reported. A descriptor the shell computes
(`flock -x "$lock_fd"`) is exactly the remedy, and a PATH operand
(`flock /var/lock/x cmd`) is the other, self-contained form. Both pass.

The decision is a node shape (``_cts_bash_ast``), never a text match. The
operand must be an ARGUMENT of the command, so a `>&2` on the same line is a
redirection and never read as an operand. `flock` must be the command's own
NAME, so `command -v flock` and a `flock 9` written inside a message a command
prints are both text this rule does not judge, and so is a heredoc body.

That name position is also the whole scope. A `flock` word further along a
command line is somebody else's argument — `helper --lock flock 9` names a
tool, and no launcher usefully wraps this form, because the descriptor the
operand names belongs to the shell that opened it.

A file that takes the descriptor from its caller on purpose, by a contract
written down somewhere, is a legitimate use of this form. Annotate with
``# allow-fixed-fd: <reason>`` on the flagged line or the comment block above
it. The reason is REQUIRED; a bare annotation does not suppress.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ARGUMENT_TYPES,
    PathologicalInputError,
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

OPT_OUT = "allow-fixed-fd"

MESSAGE = (
    "this `flock` locks a hardcoded file descriptor that this file never opens. "
    "When nothing opened it, `flock` exits non-zero at a line that reads like a "
    "lock acquisition; when something else holds that number, the lock guards a "
    "different file and both runs still report success. Open it here — "
    '`exec {lock_fd}>FILE` then `flock -x "$lock_fd"` — '
    f"or annotate `# {OPT_OUT}: <reason>`"
)

# The program names that ARE util-linux flock. A script may spell either the bare
# name or an absolute path.
_FLOCK_NAMES = frozenset({"flock"})

# A file descriptor operand: a bare non-negative integer, quoted or not.
_FD = re.compile(r"^[0-9]+$")

# flock's long options that take their value in the NEXT word when written
# without `=`. Every other long option it accepts is a flag.
_VALUE_LONG_OPTIONS = frozenset(
    {"--wait", "--timeout", "--conflict-exit-code", "--command"}
)

# flock's short options that take a value: a timeout, an exit code, a command.
# The rest (`-s`, `-x`, `-u`, `-n`, `-o`, `-F`) are flags.
_VALUE_SHORT_OPTIONS = frozenset("wEc")


def _word_nodes(command: Node) -> list[Node]:
    """COMMAND's argument-carrying children, in order, its own name first.

    Redirects and assignment prefixes are left out. That exclusion is what keeps
    a `>&2` from being read as a descriptor operand.
    """
    words: list[Node] = []
    for child in command.children:
        if child.type == "command_name":
            words.extend(child.children)
        elif child.type in ARGUMENT_TYPES:
            words.append(child)
    return words


def _program_name(word: str) -> str:
    """WORD as a program name: quotes removed, directories stripped."""
    return unquote(word).rsplit("/", 1)[-1]


def _operand(words: list[str]) -> str | None:
    """The first non-option word in WORDS, WORDS being the tokens after `flock`.

    None when the call carries no operand at all. Option reading stops at `--`,
    which is where flock's own arguments end.
    """
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if word.startswith("--"):
            name, joined, _ = word.partition("=")
            index += 1 if joined or name not in _VALUE_LONG_OPTIONS else 2
            continue
        if word.startswith("-") and len(word) > 1:
            index += _short_cluster_width(word)
            continue
        break
    return words[index] if index < len(words) else None


def _short_cluster_width(word: str) -> int:
    """The number of words a short-option cluster spans, WORD included.

    A cluster ends at the first letter that takes a value. That letter swallows
    the rest of the cluster (`-w5`) or the next word (`-w 5`).
    """
    for position, letter in enumerate(word[1:], start=1):
        if letter in _VALUE_SHORT_OPTIONS:
            return 1 if word[position + 1 :] else 2
    return 1


def _exec_descriptors(root: Node) -> set[str]:
    """The literal descriptors an `exec` in this file binds.

    `exec 9>FILE` makes fd 9 this file's own for the rest of the run, which is
    the documented pairing `flock 9` completes. A redirect on any other command
    lasts only for that command, so it is not collected.
    """
    opened: set[str] = set()
    for statement in iter_nodes(root, "redirected_statement"):
        command = statement.child_by_field_name("body")
        if command is None or command.type != "command":
            continue
        words = _word_nodes(command)
        if not words or _program_name(node_text(words[0])) != "exec":
            continue
        for redirect in iter_nodes(statement, "file_redirect"):
            descriptor = redirect.child_by_field_name("descriptor")
            if descriptor is not None:
                opened.add(node_text(descriptor))
    return opened


def violations(text: str, root: Node | None = None) -> list[int]:
    """1-based line numbers in TEXT where `flock` locks a literal descriptor that
    TEXT never opens.

    The finding is anchored on the `flock` word — the token whose call has to
    change, and the line the annotation goes on.
    """
    root = parse(text) if root is None else root
    lines = text.split("\n")
    opened = _exec_descriptors(root)
    hits = set()
    for command in iter_nodes(root, "command"):
        words = _word_nodes(command)
        if not words or _program_name(node_text(words[0])) not in _FLOCK_NAMES:
            continue
        operand = _operand([node_text(word) for word in words[1:]])
        if operand is None:
            continue
        descriptor = unquote(operand)
        if not _FD.match(descriptor) or descriptor in opened:
            continue
        hits.add(words[0].start_point[0] + 1)
    return sorted(line for line in hits if not annotated_near(lines, line, OPT_OUT))


def main(argv: list[str]) -> int:
    """Run the detector over ARGV through the shared read/report loop, one path at
    a time so a file the grammar refuses to parse fails LOUDLY (naming the path,
    exit 1) instead of being silently skipped, while every remaining path is
    still checked."""
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
