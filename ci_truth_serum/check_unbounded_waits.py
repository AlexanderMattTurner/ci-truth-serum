#!/usr/bin/env python3
"""Ban a bare remote `git` call — one with no wall-clock bound — in shell.

A `git` call to a remote (`ls-remote`, `fetch`, `clone`, `push`, `pull`) carries
no time bound of its own: a wedged or unresponsive endpoint hangs the call
FOREVER, worst inside a poll loop or a teardown window. Swallowing the error
path (`|| true`, `check=False`) does not bound it — only a wall-clock bound does.

RULE: fires on any `command` node whose word list holds a literal `git` token
immediately followed by a literal remote subcommand (past a value-taking
global option: `-C`, `-c`, `--git-dir`, `--work-tree`, `--namespace`,
`--exec-path`, so the subcommand is still found past `git -C dir fetch`),
UNLESS an earlier word in that same command is a BOUNDING WRAPPER — built in:
`timeout`; extend it with `--bounding-wrapper NAME`, repeatable, for a
project's own bounded helper (`sudo timeout 30 git fetch` is bounded because
`timeout` sits before `git`; `sudo git fetch` is not, because nothing before
`git` bounds it). A dynamic subcommand (`git "$@"`) is not a literal verb, so
it is exempt: this check cannot know what it will run. A quoted or
message-command word (`echo "run git fetch manually"`) never reaches this rule
at all — a double-quoted string is ONE argument node in the bash grammar, not
separate `git`/`fetch` tokens — and an UNQUOTED word list under a
print-only command (`echo`, `printf`, `die`, …) is skipped outright, since the
grammar cannot rule out that its words are prose rather than a call.

BLIND SPOT: `sbx exec`/`docker exec` and similar are out of scope — whether one
needs a bound depends on runtime context (a poll loop, a teardown) this
line-lint cannot see. A registered wrapper's OWN bound is trusted, never
verified: `--bounding-wrapper NAME` is a claim the consumer makes, not a fact
this check proves.

The remote-verb set is built in (`ls-remote`, `fetch`, `clone`, `push`, `pull`);
extend it with `--remote-subcommand NAME`, repeatable, for a project verb this
pack does not know about.

Opt a `git` call that genuinely must block (a clone from a LOCAL path) out with
an `# allow-unbounded: <reason>` on the command's own line span, or the line
above it.

This check reads whatever shell files its caller passes on argv — scope it with
a `files:` regex in the consumer's `.pre-commit-config.yaml`.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    command_words,
    iter_nodes,
    parse,
    unquote,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MESSAGE_PREFIX,
    annotated_near,
    run_file_cli,
    run_line_checks,
)

OPT_OUT = "allow-unbounded"

MESSAGE = (
    "remote `git` runs with no timeout — a wedged or unresponsive endpoint would "
    "hang the tool forever (worst in a teardown window or poll loop). Put a bound "
    "in front (`timeout … git <cmd>`, or a bounded helper), or annotate "
    f"`# {OPT_OUT}: <reason>`."
)

# Command words that, appearing anywhere before a `git` token in the same
# command, already bound it — so that occurrence is never inspected further.
_BOUNDING_WRAPPERS = frozenset({"timeout"})

# `git` subcommands that talk to a remote — the ones that hang on an
# unresponsive endpoint. Local subcommands (`rev-parse`, `log`, `status`) never
# wedge and are absent on purpose: an ALLOWLIST, since most of git's dozens of
# subcommands are local and the remote handful is the exception.
_REMOTE_SUBCOMMANDS = frozenset({"ls-remote", "fetch", "clone", "push", "pull"})

# `git` global options that sit BEFORE the subcommand and consume the following
# token as their value, so the subcommand is still found past them
# (`git -C dir fetch`, `git -c k=v push`).
_VALUE_OPTS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)


def _subcommand(words: tuple[str, ...]) -> str | None:
    """The first non-option word of WORDS — `git`'s subcommand — skipping
    flags and the values `_VALUE_OPTS` consumes. ``None`` when WORDS is all
    options (or empty), which a bare `git` call with no verb reads as."""
    index = 0
    while index < len(words):
        word = words[index]
        if word in _VALUE_OPTS:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        return unquote(word)
    return None


def _unbounded_git_indices(
    words: tuple[str, ...],
    bounding_wrappers: frozenset[str],
    remote_subcommands: frozenset[str],
) -> list[int]:
    """The indices of WORDS holding a `git` token that runs a literal remote
    subcommand, with no BOUNDING_WRAPPERS word anywhere before it in the same
    command."""
    hits = []
    for index, word in enumerate(words):
        if word != "git":
            continue
        if any(w in bounding_wrappers for w in words[:index]):
            continue
        if _subcommand(words[index + 1 :]) in remote_subcommands:
            hits.append(index)
    return hits


def violations(
    text: str,
    *,
    bounding_wrappers: frozenset[str] = _BOUNDING_WRAPPERS,
    remote_subcommands: frozenset[str] = _REMOTE_SUBCOMMANDS,
) -> list[int]:
    """1-based line numbers where `git` runs a literal remote subcommand with
    no bound in front, absent an `# allow-unbounded:` annotation."""
    physical = text.splitlines()
    hits: list[int] = []
    for command in iter_nodes(parse(text), "command"):
        words = tuple(command_words(command))
        if not words or MESSAGE_PREFIX.match(words[0]):
            continue  # empty, or a command that only prints its arguments
        if not _unbounded_git_indices(words, bounding_wrappers, remote_subcommands):
            continue
        lineno = command.start_point[0] + 1
        end_line = command.end_point[0] + 1
        if annotated_near(physical, lineno, OPT_OUT, span_end=end_line):
            continue
        hits.append(lineno)
    return sorted(set(hits))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--bounding-wrapper",
        action="append",
        default=[],
        dest="bounding_wrappers",
        help="a command that, appearing anywhere before `git`, already bounds "
        "it (repeatable; extends the built-in `timeout`)",
    )
    parser.add_argument(
        "--remote-subcommand",
        action="append",
        default=[],
        dest="remote_subcommands",
        help="an extra `git` subcommand that talks to a remote (repeatable)",
    )
    parser.add_argument("files", nargs="*")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    bounding_wrappers = _BOUNDING_WRAPPERS | set(args.bounding_wrappers)
    remote_subcommands = _REMOTE_SUBCOMMANDS | set(args.remote_subcommands)

    def find(text: str) -> list[int]:
        return violations(
            text,
            bounding_wrappers=bounding_wrappers,
            remote_subcommands=remote_subcommands,
        )

    return run_line_checks(args.files, find, MESSAGE)


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
