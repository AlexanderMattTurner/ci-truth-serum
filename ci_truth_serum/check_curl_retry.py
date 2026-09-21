#!/usr/bin/env python3
"""Require curl's own widened retry on a file-writing ``curl`` download.

A single-shot ``curl … -o <file>`` has no resilience to a transient network
blip: on a flaky link or a rate-limited shared-cloud IP, it fails the whole
step for one dropped packet.

The retry must be curl's own ``--retry``, not a caller-side wrapper. A wrapper
restarts the whole command on a sleep schedule somebody picked by hand. curl's
``--retry`` knows the transfer's own state, so it waits, backs off and gives up
on what actually happened. Where BOTH are present the two windows multiply,
which is a defect of its own: one consumer measured a wrapped six-attempt curl
that stalled a startup hook for tens of minutes.

Plain ``--retry`` is still too narrow. It covers only curl's default transient
set: a timeout, a 408, a 429 and the 5xx replies. A refused or aborted
connection is not in that set. An HTTP proxy that kills the CONNECT ends the
transfer with curl exit 56, and curl tries again zero times.
``--retry-all-errors`` adds every error to the set, exit 56 included.
``--retry-connrefused`` adds the refused connection alone. This check accepts
either flag, and the message names ``--retry-all-errors`` first.

This flags two shapes, with a separate message for each:

  * a file-writing ``curl`` with no ``--retry`` at all;
  * a file-writing ``curl`` with ``--retry`` but no widening flag.

The bash grammar supplies the command's words, so a backslash-continued
download is one command with all of its flags, and a ``curl`` inside a comment,
a string a message command prints, or an inert heredoc body is never read as an
invocation.

A destination that holds no bytes owes no retry: ``-`` captures into a shell
variable and ``/dev/null`` discards, so neither leaves a partial file. A
var-capturing ``curl "$(…)"`` fetch (no ``-o``) is out of scope; it is a
separate, noisier class.

``--retry-wrapper NAME`` is retired. A wrapper no longer exempts a download.
``main()`` still accepts the flag and ignores it, so an existing consumer
config keeps running. A site that must stay single-shot opts out with
``# curl-retry-ok`` (a reason is welcome but not required) on the command's own
lines or the comment block above them. The same marker covers both shapes.
"""

import argparse
import sys
from pathlib import Path

from tree_sitter import Node

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
    run_source_checks,
)

OPT_OUT = "curl-retry-ok"

# The two shapes this check reports, each with its own message below.
ARM_MISSING = "missing"
ARM_NARROW = "narrow"

# Destinations that cannot hold a partial download: stdout (a capture into a
# shell variable) and the null device (a discard, so the transfer is a
# measurement of latency or throughput rather than a download).
_NO_FILE_DESTINATIONS = frozenset({"-", "/dev/null"})

# The lookup builtins, and the flags that make one a query rather than a run:
# `command -v curl` and `type -P curl` ask WHERE curl is, they do not run it.
_LOOKUP_BUILTINS = frozenset({"command", "type", "hash", "which"})
_LOOKUP_FLAGS = frozenset({"-v", "-V", "-p", "-P", "-t"})

# Command words whose whole job is to PRINT their arguments — a word list
# carries no quotes, so a policed word among them is prose, not a download.
_MESSAGE_COMMANDS = frozenset({"echo", "printf", "warn", "status", "die", "log", ":"})

# The flags that widen curl's retry set past its default transient replies, so
# that a refused or aborted connection (exit 56) is retried too.
_WIDENING_FLAGS = frozenset({"--retry-all-errors", "--retry-connrefused"})


def _is_message(name: str) -> bool:
    return name in _MESSAGE_COMMANDS


def _is_lookup(name: str, rest: list[str]) -> bool:
    return name in _LOOKUP_BUILTINS and any(word in _LOOKUP_FLAGS for word in rest)


def _output_flag(word: str) -> bool:
    """True when WORD is curl's ``-o``/``--output`` flag, including a bundled
    short-flag tail (`-fsSLo` == `-f -s -S -L -o`) a bare `-o` check would
    miss. `--connect-timeout` is not it: the `o` must end the flag cluster."""
    if word == "--output":
        return True
    return (
        word.startswith("-")
        and not word.startswith("--")
        and word[1:].isalpha()
        and word.endswith("o")
    )


def _writes_a_file(words: list[str]) -> bool:
    """True when WORDS carry an ``-o``/``--output`` whose destination is a
    file — read from the flag's VALUE, since `-o -` and `-o /dev/null` name no
    file at all."""
    for index, word in enumerate(words):
        if word.startswith("--output="):
            if word.removeprefix("--output=") not in _NO_FILE_DESTINATIONS:
                return True
        elif _output_flag(word):
            destination = words[index + 1] if index + 1 < len(words) else ""
            if destination not in _NO_FILE_DESTINATIONS:
                return True
    return False


def _is_retry_flag(word: str) -> bool:
    """True for curl's ``--retry N`` or ``--retry=N``. Only that flag asks curl
    to try again. ``--retry-delay`` and ``--retry-max-time`` shape a ladder that
    ``--retry`` starts; on their own they retry nothing, so a prefix match on
    ``--retry`` would read a dead knob as resilience."""
    return word == "--retry" or word.startswith("--retry=")


def _literal_tokens(value: Node) -> set[str]:
    """The literal words under VALUE, an assignment's right-hand side. The
    grammar names each literal piece — a bare `word`, a `raw_string`, the
    `string_content` inside a double-quoted string — so an array's parentheses
    and an expansion's own text never arrive as tokens. One piece can still
    hold several flags (`f="--retry 3 --retry-all-errors"`), and the shell
    word-splits it, so this does too."""
    tokens: set[str] = set()
    for node in iter_nodes(value, "word", "raw_string", "string_content"):
        tokens.update(unquote(token) for token in node_text(node).split())
    return tokens


def _flag_carrying_names(root: Node) -> tuple[frozenset[str], frozenset[str]]:
    """The variable names this script assigns a curl retry flag to: the ones
    that carry ``--retry``, and the ones that carry a widening flag.

    A script often computes the flag rather than writing it at the call site:
    ``retry_widen="--retry-connrefused"`` on one line, then ``curl …
    "$retry_widen" -o f`` on the next. The grammar finds the assignment and its
    VALUE node. Inside that value the tokens are read as literal text, because
    an unquoted expansion is exactly what the shell word-splits back into
    flags.
    """
    retry: set[str] = set()
    widening: set[str] = set()
    for node in iter_nodes(root, "variable_assignment"):
        name = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        if name is None or value is None:
            continue
        tokens = _literal_tokens(value)
        if any(_is_retry_flag(token) for token in tokens):
            retry.add(node_text(name))
        if tokens & _WIDENING_FLAGS:
            widening.add(node_text(name))
    return frozenset(retry), frozenset(widening)


def _expanded_names(command: Node) -> set[str]:
    """Every variable name COMMAND reads. ``$x``, ``${x}`` and ``"${x[@]}"``
    all carry a ``variable_name`` node, so one walk covers each spelling."""
    return {node_text(n) for n in iter_nodes(command, "variable_name")}


def _download_arm(
    words: list[str], read_names: set[str], carriers: tuple[frozenset[str], ...]
) -> str | None:
    """The shape WORDS (a command's name followed by its arguments) are in, or
    ``None`` when they are clean. Read over the whole word list, not just the
    command word, because a wrapper can stand in front of curl: `timeout 30
    curl -o f url` is still the download this check judges. CARRIERS holds the
    variable names that carry a retry flag and a widening flag, so a flag the
    script computes into a variable counts as present."""
    if not words:
        return None
    name, rest = words[0], words[1:]
    if _is_lookup(name, rest) or _is_message(name):
        return None
    if "curl" not in words or not _writes_a_file(words):
        return None
    bare = {unquote(word) for word in words}
    retry_names, widening_names = carriers
    if not any(_is_retry_flag(word) for word in bare) and not (
        read_names & retry_names
    ):
        return ARM_MISSING
    if bare & _WIDENING_FLAGS or read_names & widening_names:
        return None
    return ARM_NARROW


def findings(text: str) -> list[tuple[int, str]]:
    """The (1-based line, arm) pairs for the file-writing ``curl`` commands in
    TEXT that no ``# curl-retry-ok`` annotation exempts. One line reports once,
    under the first arm that claims it."""
    physical = text.splitlines()
    root = parse(text)
    carriers = _flag_carrying_names(root)
    hits: dict[int, str] = {}
    for node in iter_nodes(root, "command"):
        arm = _download_arm(command_words(node), _expanded_names(node), carriers)
        if arm is None:
            continue
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        if annotated_near(physical, start, OPT_OUT, require_reason=False, span_end=end):
            continue
        hits.setdefault(start, arm)
    return list(hits.items())


def violations(text: str, arm: str | None = None) -> list[int]:
    """1-based line numbers of the reported downloads in TEXT. ARM selects one
    shape; the default reports both."""
    return [line for line, found in findings(text) if arm in (None, found)]


def _remedy(arm: str) -> str:
    """The one sentence that says how to make ARM green."""
    if arm == ARM_MISSING:
        return (
            "Add `--retry 3 --retry-all-errors --retry-delay 2` to the curl "
            f"call itself, or annotate `# {OPT_OUT}: <reason>`."
        )
    return (
        "Add `--retry-all-errors`, or `--retry-connrefused` for the narrower "
        f"case, or annotate `# {OPT_OUT}: <reason>`."
    )


MESSAGES = {
    ARM_MISSING: (
        "single-shot `curl … -o` download with no `--retry` — one dropped "
        "packet fails the install. A caller-side retry wrapper does not count. "
        "It restarts the whole command on a sleep ladder somebody picked by "
        "hand, and the two windows multiply. " + _remedy(ARM_MISSING)
    ),
    ARM_NARROW: (
        "`curl … -o` download whose `--retry` is too narrow — it covers only "
        "curl's default transient replies. A refused or aborted connection is "
        "curl exit 56, and plain `--retry` does not retry it. " + _remedy(ARM_NARROW)
    ),
}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retry-wrapper",
        action="append",
        default=[],
        metavar="NAME",
        help="retired and ignored: a caller-side wrapper no longer exempts a "
        "download. Accepted so an existing config keeps running (repeatable)",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if not args.files:
        print(
            "check_curl_retry: no files to scan. This check reads only the "
            "paths you give it, so an empty run would report a clean pass "
            "over nothing.",
            file=sys.stderr,
        )
        print(
            "  to scan the whole tree: git ls-files -z | xargs -0 python -m "
            "ci_truth_serum.check_curl_retry",
            file=sys.stderr,
        )
        return 2
    if args.retry_wrapper:
        names = ", ".join(sorted(set(args.retry_wrapper)))
        print(
            f"check_curl_retry: --retry-wrapper is retired and ignored ({names}). "
            "curl must carry its own `--retry`. Drop the flag from your config.",
            file=sys.stderr,
        )
    # One path at a time, so a file the shell grammar refuses to parse (over
    # _MAX_PIPE_BYTES of piped bytes) fails LOUDLY, naming the path, instead of
    # taking the whole run down with an uncaught traceback. Each arm makes its
    # own pass so that every hit carries the message for its own shape; `parse`
    # caches the tree, so the second pass re-walks it rather than re-reading it.
    status = 0
    for path in args.files:
        try:
            for arm, message in MESSAGES.items():
                status = max(
                    status,
                    run_source_checks(
                        [path],
                        lambda text, _path, selected=arm: violations(text, selected),
                        message,
                    ),
                )
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
