#!/usr/bin/env python3
"""Ban a bare remote `git` call, or a bare container `exec` in a loop condition.

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

EXEC RULE: a `<tool> exec` call (`docker`, `docker-compose`, `podman`,
`nerdctl`, `kubectl`, `sbx`, `lxc`, `incus`; extend with `--exec-tool NAME`,
repeatable) fires when it sits in the CONDITION of a `while` or `until` loop.
That covers a `!` negation, an `&&`/`||` list, a pipeline, a `$(…)` and a
redirect in the condition. One probe against a wedged runtime never returns, so
the loop never tests its condition again. Global options and one `compose` or
`container` group word before the verb are skipped (`kubectl -n ns exec`,
`docker compose exec`). The same bounding-wrapper and annotation rules apply. A
bound bounds each probe, not the loop: `until timeout 5 docker exec …` can
still poll forever against a runtime that answers "no" every time.

Every tool and wrapper word is compared by its basename, so `/usr/bin/timeout`
bounds a call and `/usr/bin/docker exec` fires.

BLIND SPOT: an `exec` call OUTSIDE a loop condition is out of scope, the loop
BODY included. So is an `exec` call hidden in a function the condition calls
(`until is_ready; do`). Whether such a call needs a bound depends on runtime
context this line-lint cannot see. A registered wrapper's OWN bound is trusted,
never verified: `--bounding-wrapper NAME` is a claim the consumer makes, not a
fact this check proves.

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
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    command_words,
    condition_commands,
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
    "remote `git`, or a `<tool> exec` in a `while`/`until` condition, runs with no "
    "timeout — a wedged endpoint or runtime would hang the tool forever (worst in "
    "a teardown window or poll loop). Put a bound in front (`timeout … git <cmd>`, "
    f"`timeout … docker exec …`, or a bounded helper), or annotate `# {OPT_OUT}: "
    "<reason>`."
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


# Container and VM runtimes whose `exec` verb runs a command inside a guest. A
# wedged runtime or guest never answers, so the call never returns.
_EXEC_TOOLS = frozenset(
    {
        "docker",
        "docker-compose",
        "podman",
        "nerdctl",
        "kubectl",
        "sbx",
        "lxc",
        "incus",
    }
)

# Command groups that sit between a runtime tool and its `exec` verb
# (`docker compose exec`, `podman container exec`).
_EXEC_GROUPS = frozenset({"compose", "container"})


def _name(word: str) -> str:
    """WORD's program name: unquoted, with any directory part removed, so
    `/usr/bin/timeout` reads as `timeout`."""
    return PurePosixPath(unquote(word)).name


def _bounded_before(
    words: tuple[str, ...], index: int, bounding_wrappers: frozenset[str]
) -> bool:
    """Whether a BOUNDING_WRAPPERS word sits anywhere before WORDS[INDEX]."""
    return any(_name(word) in bounding_wrappers for word in words[:index])


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
        if _name(word) != "git":
            continue
        if _bounded_before(words, index, bounding_wrappers):
            continue
        if _subcommand(words[index + 1 :]) in remote_subcommands:
            hits.append(index)
    return hits


def _runs_exec(words: tuple[str, ...]) -> bool:
    """Whether WORDS (the words after a runtime tool) run its `exec` verb.

    Leading options are skipped, and so is one value after an option with no
    `=`, so `kubectl -n ns exec` still counts. So is one `_EXEC_GROUPS` word,
    so `docker compose exec` counts. Any other word ends the search, so
    `docker run img exec` does not count."""
    after_option = False
    group_seen = False
    for word in map(unquote, words):
        if word == "exec":
            return True
        if word.startswith("-"):
            after_option = "=" not in word
            continue
        if after_option:
            after_option = False
            continue
        if group_seen or word not in _EXEC_GROUPS:
            return False
        group_seen = True
    return False


# Programs that name a command without running it (`command -v docker`).
_LOOKUPS = frozenset({"type", "which", "hash", "whereis"})


def _looked_up(words: tuple[str, ...], index: int) -> bool:
    """Whether a lookup before WORDS[INDEX] only names that word: a `_LOOKUPS`
    program, or `command` given `-v` or `-V`."""
    for position, word in enumerate(words[:index]):
        name = _name(word)
        if name in _LOOKUPS:
            return True
        if name == "command" and any(
            unquote(flag) in ("-v", "-V") for flag in words[position + 1 : index]
        ):
            return True
    return False


def _unbounded_exec(
    words: tuple[str, ...],
    bounding_wrappers: frozenset[str],
    exec_tools: frozenset[str],
) -> bool:
    """Whether WORDS hold an EXEC_TOOLS word that runs `exec`, with no
    BOUNDING_WRAPPERS word anywhere before it in the same command."""
    for index, word in enumerate(words):
        if _name(word) not in exec_tools:
            continue
        if _bounded_before(words, index, bounding_wrappers) or _looked_up(words, index):
            continue
        if _runs_exec(words[index + 1 :]):
            return True
    return False


# The grammar parses both `while` and `until` as a `while_statement`.
_LOOPS = frozenset({"while_statement"})


def violations(
    text: str,
    *,
    bounding_wrappers: frozenset[str] = _BOUNDING_WRAPPERS,
    remote_subcommands: frozenset[str] = _REMOTE_SUBCOMMANDS,
    exec_tools: frozenset[str] = _EXEC_TOOLS,
) -> list[int]:
    """1-based line numbers where `git` runs a literal remote subcommand, or a
    loop condition runs `<tool> exec`, with no bound in front, absent an
    `# allow-unbounded:` annotation."""
    physical = text.splitlines()
    hits: list[int] = []
    root = parse(text)
    in_loop_condition = {node.id for node in condition_commands(root, _LOOPS)}
    for command in iter_nodes(root, "command"):
        words = tuple(command_words(command))
        if not words or MESSAGE_PREFIX.match(words[0]):
            continue  # empty, or a command that only prints its arguments
        git_hit = bool(
            _unbounded_git_indices(words, bounding_wrappers, remote_subcommands)
        )
        exec_hit = command.id in in_loop_condition and _unbounded_exec(
            words, bounding_wrappers, exec_tools
        )
        if not (git_hit or exec_hit):
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
    parser.add_argument(
        "--exec-tool",
        action="append",
        default=[],
        dest="exec_tools",
        help="an extra runtime whose `exec` verb, in a `while`/`until` "
        "condition, needs a bound (repeatable)",
    )
    parser.add_argument("files", nargs="*")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    bounding_wrappers = _BOUNDING_WRAPPERS | set(args.bounding_wrappers)
    remote_subcommands = _REMOTE_SUBCOMMANDS | set(args.remote_subcommands)
    exec_tools = _EXEC_TOOLS | set(args.exec_tools)

    def find(text: str) -> list[int]:
        return violations(
            text,
            bounding_wrappers=bounding_wrappers,
            remote_subcommands=remote_subcommands,
            exec_tools=exec_tools,
        )

    return run_line_checks(args.files, find, MESSAGE)


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
