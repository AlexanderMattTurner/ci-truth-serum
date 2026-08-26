#!/usr/bin/env python3
"""Require a retry on a file-writing ``curl`` download.

A single-shot ``curl … -o <file>`` has no resilience to a transient network
blip: on a flaky link or a rate-limited shared-cloud IP, it fails the whole
step for one dropped packet.

This flags a command that runs ``curl`` and writes to a file (``-o`` /
``--output``) with neither a ``--retry`` flag nor a call to a configured retry
wrapper. The bash grammar supplies the command's words, so a backslash-
continued download is one command with all of its flags, and a ``curl`` inside
a comment, a string a message command prints, or an inert heredoc body is
never read as an invocation.

A destination that holds no bytes owes no retry: ``-`` captures into a shell
variable and ``/dev/null`` discards, so neither leaves a partial file. A
var-capturing ``curl "$(…)"`` fetch (no ``-o``) is out of scope; it is a
separate, noisier class.

Configure the names of this repo's own retry helpers with a repeatable
``--retry-wrapper NAME`` (a call to any of them anywhere in the command's
words counts as a retry). With none configured, only curl's own ``--retry``
flag exempts a call. A site that must stay single-shot opts out with
``# curl-retry-ok`` (a reason is welcome but not required) on the command's
own lines or the comment block above them.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_words,
    iter_nodes,
    parse,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    run_source_checks,
)

OPT_OUT = "curl-retry-ok"

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


def _unretried_download(words: list[str], retry_wrappers: frozenset[str]) -> bool:
    """True when WORDS (a command's name followed by its arguments) run a
    file-writing ``curl`` with no ``--retry`` flag and no call to a configured
    retry wrapper. Read over the whole word list, not just the command word,
    because the bound or the retry may be a wrapper standing in front of curl:
    `timeout 30 curl -o f url` still owes a retry, while `gb_retry -- curl -o
    f url` already has one."""
    if not words:
        return False
    name, rest = words[0], words[1:]
    if _is_lookup(name, rest) or _is_message(name):
        return False
    if "curl" not in words:
        return False
    if not _writes_a_file(words):
        return False
    return not (retry_wrappers & set(words)) and not any(
        word.startswith("--retry") for word in words
    )


def violations(text: str, retry_wrappers: frozenset[str] = frozenset()) -> list[int]:
    """1-based line numbers of the commands in TEXT running a file-writing
    ``curl`` with no retry, absent a ``# curl-retry-ok`` annotation."""
    physical = text.splitlines()
    hits: list[int] = []
    for node in iter_nodes(parse(text), "command"):
        words = command_words(node)
        if not _unretried_download(words, retry_wrappers):
            continue
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        if annotated_near(physical, start, OPT_OUT, require_reason=False, span_end=end):
            continue
        hits.append(start)
    return list(dict.fromkeys(hits))


def _remedy(retry_wrappers: frozenset[str]) -> str:
    if retry_wrappers:
        names = " or ".join(f"`{name}`" for name in sorted(retry_wrappers))
        wrap = f"wrap it in {names}"
    else:
        wrap = "wrap it in your retry helper"
    return (
        f"add `--retry 3 --retry-delay 2`, {wrap}, or annotate `# {OPT_OUT}: <reason>`."
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retry-wrapper",
        action="append",
        default=[],
        metavar="NAME",
        help="a call to this retry helper anywhere in the command counts as a "
        "retry (repeatable)",
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
    retry_wrappers = frozenset(args.retry_wrapper)
    message = (
        "single-shot `curl … -o` download with no retry — a transient blip "
        f"fails the install. {_remedy(retry_wrappers)}"
    )
    # One path at a time, so a file the shell grammar refuses to parse (over
    # _MAX_PIPE_BYTES of piped bytes) fails LOUDLY, naming the path, instead of
    # taking the whole run down with an uncaught traceback.
    status = 0
    for path in args.files:
        try:
            status = max(
                status,
                run_source_checks(
                    [path],
                    lambda text, _path: violations(text, retry_wrappers),
                    message,
                ),
            )
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
