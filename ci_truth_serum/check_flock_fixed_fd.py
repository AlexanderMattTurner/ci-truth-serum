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

``--no-exec-exemption`` reports that pairing as well. An `exec 9>FILE` replaces
whatever fd 9 the caller passed in, such as a test harness's signal pipe. A
repo whose callers hand descriptors to its scripts can ban every literal number.

Only a LITERAL number is reported. A descriptor the shell computes
(`flock -x "$lock_fd"`) is exactly the remedy, and a PATH operand
(`flock /var/lock/x cmd`) is the other, self-contained form. Both pass.

The decision is a node shape (``_cts_bash_ast``), never a text match. The
operand must be an ARGUMENT of the command, so a `>&2` on the same line is a
redirection and never read as an operand. `flock` must be the program the
command runs, so `command -v flock` and a `flock 9` written inside a message a
command prints are both text this rule does not judge, and so is a heredoc body.

A PREFIX command runs the program after it, so `sudo flock 9` is still a call
to `flock`. The prefixes are `command`, `doas`, `env`, `exec`, `nice`, `nohup`,
`sudo` and `time`, each with its own options skipped. A repo's own wrapper
function is a prefix too when ``--wrapper NAME`` names it (repeatable). The
wrapper must run its first argument as the program, as `"$@"` does.

A `flock` word in any other position is somebody else's argument:
`helper --lock flock 9` names a tool.

`sudo` and `doas` close every descriptor above 2 by default before they run
the program.
Behind them, the literal descriptor is never open, so an `exec 9>FILE` in the
same file does not exempt the call.

A file that takes the descriptor from its caller on purpose, by a contract
written down somewhere, is a legitimate use of this form. Annotate with
``# allow-fixed-fd: <reason>`` on the flagged line or the comment block above
it. The reason is REQUIRED; a bare annotation does not suppress.

Invoked by pre-commit with the staged shell files as arguments.
"""

import argparse
import re
import sys
from pathlib import Path

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ARGUMENT_TYPES,
    PathologicalInputError,
    is_lookup,
    iter_nodes,
    node_text,
    parse,
    program_name,
    unquote,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    run_line_checks,
)

OPT_OUT = "allow-fixed-fd"

MESSAGE = (
    "this `flock` locks a hardcoded file descriptor, and nothing in this file "
    "makes that number safe to lock. Behind `sudo` or `doas`, an `exec 9>FILE` "
    "here cannot make it safe, because both close the descriptor first. "
    "When nothing opened it, `flock` exits non-zero at a line that reads like a "
    "lock acquisition; when something else holds that number, the lock guards a "
    "different file and both runs still report success. Open it here — "
    '`exec {lock_fd}>FILE` then `flock -x "$lock_fd"` — '
    f"or annotate `# {OPT_OUT}: <reason>`"
)

# The program names that ARE util-linux flock. A script may spell either the bare
# name or an absolute path.
_FLOCK_NAMES = frozenset({"flock"})

# Commands that run the program named after them, each mapped to its own options
# that take their value in the NEXT word. Every other option is a flag.
_PREFIXES: dict[str, frozenset[str]] = {
    "command": frozenset(),
    "doas": frozenset({"-C", "-u"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "exec": frozenset({"-a"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "nohup": frozenset(),
    "sudo": frozenset(
        {
            "-C",
            "--close-from",
            "-D",
            "--chdir",
            "-g",
            "--group",
            "-h",
            "--host",
            "-p",
            "--prompt",
            "-R",
            "--chroot",
            "-r",
            "--role",
            "-T",
            "--command-timeout",
            "-t",
            "--type",
            "-U",
            "--other-user",
            "-u",
            "--user",
        }
    ),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
}

# The prefixes that close every descriptor above 2 before they run the program.
_CLOSING_PREFIXES = frozenset({"sudo", "doas"})

# An `env` or `sudo` operand that sets a variable rather than naming the program.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

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


def _operand(words: list[str]) -> str | None:
    """The first non-option word in WORDS, WORDS being the tokens after `flock`.

    None when the call carries no operand at all. Option reading stops at `--`,
    which is where flock's own arguments end.
    """
    resume = 0
    for index, word in enumerate(words):
        if index < resume:
            continue
        if word == "--":
            return words[index + 1] if index + 1 < len(words) else None
        if word.startswith("--"):
            name, joined, _ = word.partition("=")
            resume = index + (1 if joined or name not in _VALUE_LONG_OPTIONS else 2)
        elif word.startswith("-") and len(word) > 1:
            resume = index + _short_cluster_width(word)
        else:
            return word
    return None


def _short_cluster_width(word: str) -> int:
    """The number of words a short-option cluster spans, WORD included.

    A cluster ends at the first letter that takes a value. That letter swallows
    the rest of the cluster (`-w5`) or the next word (`-w 5`).
    """
    for position, letter in enumerate(word[1:], start=1):
        if letter in _VALUE_SHORT_OPTIONS:
            return 1 if word[position + 1 :] else 2
    return 1


# The `sudo` options that run no program: the words after them are the subject of a
# listing, a validation or an edit, never a command `sudo` starts.
_NON_RUNNING_SUDO_OPTIONS = frozenset(
    {"-e", "--edit", "-K", "--remove-timestamp", "-l", "--list", "-v", "--validate"}
)

# The prefixes whose `NAME=VALUE` operands set a variable for the program.
_ASSIGNING_PREFIXES = frozenset({"env", "sudo"})


def _skip_prefix_options(words: list[str], index: int, prefix: str) -> int:
    """The index of the first word at or after INDEX that is not an option of PREFIX.

    An option in `_PREFIXES[PREFIX]` takes the next word too. A `NAME=VALUE`
    operand of `env` or `sudo` sets a variable, so it is skipped as well. `--`
    ends the options. A `sudo` option that runs no program returns `len(words)`,
    so nothing after it counts as the program.
    """
    value_options = _PREFIXES[prefix]
    skip_next = False
    for position in range(index, len(words)):
        word = unquote(words[position])
        if skip_next:
            skip_next = False
        elif word == "--":
            return position + 1
        elif prefix == "sudo" and word in _NON_RUNNING_SUDO_OPTIONS:
            return len(words)
        elif prefix in _ASSIGNING_PREFIXES and _ASSIGNMENT.match(word):
            continue
        elif word.startswith("-") and len(word) > 1:
            skip_next = word in value_options
        else:
            return position
    return len(words)


def _flock_position(words: list[str], wrappers: frozenset[str]) -> tuple[int, bool]:
    """The index of the `flock` word that WORDS run, and whether a prefix closed
    the inherited descriptors first.

    The index is -1 when WORDS run some other program. A lookup such as
    `command -v flock` names the program but does not run it.
    """
    closed = False
    resume = 0
    for index, word in enumerate(words):
        if index < resume:
            continue
        name = program_name(word)
        if name in _FLOCK_NAMES:
            return index, closed
        if name in wrappers:
            continue
        if name not in _PREFIXES or is_lookup(words[index:]):
            break
        closed = closed or name in _CLOSING_PREFIXES
        resume = _skip_prefix_options(words, index + 1, name)
    return -1, closed


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
        if not words or program_name(node_text(words[0])) != "exec":
            continue
        for redirect in iter_nodes(statement, "file_redirect"):
            descriptor = redirect.child_by_field_name("descriptor")
            if descriptor is not None:
                opened.add(node_text(descriptor))
    return opened


def violations(
    text: str,
    root: Node | None = None,
    wrappers: frozenset[str] = frozenset(),
    exec_exempts: bool = True,
) -> list[int]:
    """1-based line numbers in TEXT where `flock` locks a literal descriptor that
    TEXT never opens.

    WRAPPERS names the repo's own wrapper functions. EXEC_EXEMPTS False also
    reports a descriptor that an `exec` in TEXT opens. The finding is anchored on
    the `flock` word, which is the line the annotation goes on.
    """
    root = parse(text) if root is None else root
    lines = text.split("\n")
    opened = _exec_descriptors(root) if exec_exempts else set()
    hits = set()
    for command in iter_nodes(root, "command"):
        words = _word_nodes(command)
        texts = [node_text(word) for word in words]
        position, closed = _flock_position(texts, wrappers)
        if position < 0:
            continue
        operand = _operand(texts[position + 1 :])
        if operand is None:
            continue
        descriptor = unquote(operand)
        if not _FD.match(descriptor) or (descriptor in opened and not closed):
            continue
        hits.add(words[position].start_point[0] + 1)
    return sorted(line for line in hits if not annotated_near(lines, line, OPT_OUT))


def main(argv: list[str]) -> int:
    """Run the detector over the files in ARGV.

    One path runs at a time, so a file the grammar refuses fails LOUDLY. The
    run names the path and exits 1, and every other path is still checked. An
    empty file list exits 2, because a pass over nothing is not a clean pass.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wrapper",
        action="append",
        default=[],
        metavar="NAME",
        help="a function that runs its first argument as the program, as "
        "`sudo` does (repeatable)",
    )
    parser.add_argument(
        "--no-exec-exemption",
        action="store_true",
        help="also report a descriptor that an `exec N>FILE` in the file opens",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if not args.files:
        parser.error(
            "no files to scan. This check reads only the paths you give it, so "
            "an empty run would report a clean pass over nothing."
        )
    wrappers = frozenset(args.wrapper)

    def find(text: str) -> list[int]:
        return violations(
            text, wrappers=wrappers, exec_exempts=not args.no_exec_exemption
        )

    status = 0
    for path in args.files:
        try:
            status = max(status, run_line_checks([path], find, MESSAGE))
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
