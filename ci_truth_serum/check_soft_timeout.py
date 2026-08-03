#!/usr/bin/env python3
"""Ban a `timeout` bound the bounded command can ignore.

`timeout 60 cmd` is a request, not a limit. At the deadline `timeout` sends
SIGTERM, and a command may install a handler for that signal, or block it, or
sit in a state the handler never runs from. The command then keeps running, and
whatever waited on it keeps waiting — the exact failure the bound was written to
prevent. Two measured escapes: an `npm` install blocked on a dead registry
socket outlived a 180 s bound by 8 minutes 36 seconds, and a wedged sandbox held
an incident kill switch on its first target while every later target kept
running, under a report that said the halt was complete.

`--kill-after=N` (`-k N`) schedules a second signal, SIGKILL, which no process
can catch or block. That is what turns the request into a limit. A bound that
sends SIGKILL first (`-s KILL`) is a limit too, and passes.

The whole decision is a node shape (``_bash_ast``), never a text match. Three
executed positions count, and a text scan gets each of them wrong:

  * a `command` whose name is `timeout` — ``timeout 60 cmd``;
  * a word inside a `command` — ``sbx exec box -- timeout 60 cmd``, where the
    bound wraps a program another command launches;
  * the first element of an `array` — ``run=(timeout 600)``, expanded later as
    ``"${run[@]}" cmd``. The grammar reads that line as an assignment whose value
    is a list of words, so there is no command named `timeout` to find. Both
    escapes above were written this shape, and the sweep that searched for a
    command missed both.

A `timeout` the shell never runs is not an invocation: text inside a `string`
(``gb_warn "raise the timeout 60 seconds"``) and a `heredoc_body` hold no words,
and ``((timeout > 0))`` reads a variable of that name. None reaches this lint.

An invocation is recognised by `timeout`'s own grammar — its options, then a
duration (a literal such as `90` or `1.5m`, or a value the shell computes), then
the command. A word position needs that command word too, so `command -v
timeout` and a printed sentence naming a duration stay out.

Opt out with `# allow-soft-timeout: <reason>` on any physical line of the
command, or on the line above, when the soft limit is the point: a command that
must be asked to stop and never killed, because a SIGKILL would leave its state
half written. `dpkg` holds SIGTERM for that reason. The reason is REQUIRED.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ARGUMENT_TYPES,
    PathologicalInputError,
    iter_nodes,
    node_text,
    parse,
    unquote,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MESSAGE_PREFIX,
    annotated_near,
    run_line_checks,
)

OPT_OUT = "allow-soft-timeout"

MESSAGE = (
    "this `timeout` sends only SIGTERM, which the bounded command can catch, "
    "block, or never reach — so the command can outlive the bound, and the "
    "bound stops being a limit. Add `--kill-after=<grace>` (or `-s KILL` to go "
    f"straight to the signal nothing survives), or annotate `# {OPT_OUT}: "
    "<reason>` when the command must be asked to stop and never killed."
)

# The program names that ARE coreutils timeout. macOS ships GNU coreutils under a
# `g` prefix, and a script may spell either one as an absolute path.
_TIMEOUT_NAMES = frozenset({"timeout", "gtimeout"})

# timeout's duration argument: a number with an optional unit suffix (GNU takes
# s/m/h/d, and a bare number means seconds).
_DURATION = re.compile(r"^[0-9]+(?:\.[0-9]+)?[smhd]?$")

# timeout's long options that take their value in the NEXT word when written
# without `=`. Every other long option it accepts is a flag, and stepping over a
# flag needs no knowledge of which.
_VALUE_LONG_OPTIONS = frozenset({"--kill-after", "--signal"})


class _Bound(NamedTuple):
    """One `timeout` call read off the words that follow its own name."""

    escalates: bool  # a SIGKILL is scheduled (`-k`) or sent outright (`-s KILL`)
    duration: str | None  # the deadline word, or None when these words are no call
    consumed: int  # words the call itself spans, up to and including the duration


def _names_sigkill(value: str) -> bool:
    """True when VALUE names the signal a process cannot catch or block."""
    return unquote(value).upper().removeprefix("SIG") in {"KILL", "9"}


def _is_duration(word: str) -> bool:
    """True when WORD can be timeout's deadline: a literal, or a value the shell
    computes (`"$CAP"`, `"${CAP:-600}"`), whose text this lint cannot read and so
    must not reject."""
    return bool(_DURATION.match(unquote(word)) or "$" in word or "`" in word)


def _next_word(words: list[str], index: int) -> str:
    """The word after WORDS[INDEX], or empty when the list ends there."""
    return words[index + 1] if index + 1 < len(words) else ""


def _read_long_option(words: list[str], index: int) -> tuple[bool, int]:
    """Read WORDS[INDEX] as a `--long` option: (it schedules a SIGKILL, words used)."""
    name, joined, value = words[index].partition("=")
    signal = value if joined else _next_word(words, index)
    escalates = name == "--kill-after" or (
        name == "--signal" and _names_sigkill(signal)
    )
    takes_next = not joined and name in _VALUE_LONG_OPTIONS
    return escalates, 2 if takes_next else 1


def _read_short_options(words: list[str], index: int) -> tuple[bool, int]:
    """Read WORDS[INDEX] as a short-option cluster (`-k`, `-k10`, `-vk`):
    (it schedules a SIGKILL, words used).

    A cluster ends at the first letter taking a value, which swallows the rest of
    the cluster or the next word.
    """
    for position, letter in enumerate(words[index][1:], start=1):
        attached = words[index][position + 1 :]
        used = 1 if attached else 2
        if letter == "k":
            return True, used
        if letter == "s":
            return _names_sigkill(attached or _next_word(words, index)), used
    return False, 1


def _read_bound(words: list[str]) -> _Bound:
    """The `timeout` call WORDS spell, WORDS being the tokens after its name.

    Reading stops at the duration, because every word past it belongs to the
    bounded command: a `--kill-after` over there is the inner program's own flag,
    not this bound's.
    """
    escalates = False
    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if word.startswith("--"):
            found, used = _read_long_option(words, index)
        elif word.startswith("-") and len(word) > 1:
            found, used = _read_short_options(words, index)
        else:
            break
        escalates = escalates or found
        index += used
    if index >= len(words) or not _is_duration(words[index]):
        return _Bound(escalates, None, 0)
    return _Bound(escalates, words[index], index + 1)


def _word_nodes(node: Node) -> list[Node]:
    """The argument-carrying children of a `command` or an `array`, in order.

    A `command_name`'s own word is included, so the name position is read like
    any other. Redirects, heredoc plumbing and assignment prefixes are left out —
    that exclusion is what keeps a `>&2` from being read as a duration.
    """
    words: list[Node] = []
    for child in node.children:
        if child.type == "command_name":
            words.extend(child.children)
        elif child.type in ARGUMENT_TYPES:
            words.append(child)
    return words


def _program_name(word: str) -> str:
    """WORD as a program name: quotes removed, directories stripped."""
    return unquote(word).rsplit("/", 1)[-1]


def _soft_bounds(node: Node, needs_command: bool) -> list[Node]:
    """The `timeout` words in NODE whose call schedules no SIGKILL.

    NEEDS_COMMAND demands a word past the duration — the program being bounded —
    for a `timeout` that is not NODE's first word. That is what keeps
    `command -v timeout` and `sleep 5 # timeout 60` out of a `command`. An
    `array` sets it False: ``run=(timeout 600)`` is a complete bound whose
    command arrives at the call site.
    """
    words = _word_nodes(node)
    texts = [node_text(word) for word in words]
    hits = []
    for index, text in enumerate(texts):
        if _program_name(text) not in _TIMEOUT_NAMES:
            continue
        rest = texts[index + 1 :]
        bound = _read_bound(rest)
        if bound.duration is None:
            continue
        if needs_command and index > 0 and bound.consumed >= len(rest):
            continue
        if not bound.escalates:
            hits.append(words[index])
    return hits


def violations(text: str, root: Node | None = None) -> list[int]:
    """1-based line numbers in TEXT where a `timeout` bound sends no SIGKILL.

    The finding is anchored on the `timeout` word — the token a reader adds the
    flag to, and the line the annotation goes on.
    """
    root = parse(text) if root is None else root
    lines = text.split("\n")
    hits = set()
    for command in iter_nodes(root, "command"):
        names = [node_text(word) for word in _word_nodes(command)]
        # A command that only PRINTS has arguments that are text, so a sentence
        # naming a duration is not a bound. Skipping the whole command is safe:
        # its own name is what MESSAGE_PREFIX matched, and no printer is named
        # `timeout`, so no real call is dropped with it.
        if names and MESSAGE_PREFIX.match(_program_name(names[0])):
            continue
        hits.update(word.start_point[0] + 1 for word in _soft_bounds(command, True))
    for array in iter_nodes(root, "array"):
        hits.update(word.start_point[0] + 1 for word in _soft_bounds(array, False))
    return sorted(line for line in hits if not annotated_near(lines, line, OPT_OUT))


def main(argv: list[str]) -> int:
    """Run the detector over ARGV through the shared read/report loop, one path at
    a time so a file the grammar refuses to parse safely fails LOUDLY (naming the
    path, exit 1) instead of being silently skipped, while every remaining path is
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
    raise SystemExit(main(sys.argv[1:]))
