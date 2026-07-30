#!/usr/bin/env python3
"""Ban stderr suppression (``2>/dev/null``, ``&>/dev/null``, or the canonical
``>/dev/null 2>&1``) on container launch/build commands.

Discarding stderr on a command whose only other failure signal is its exit code
hides the diagnostic and leaves nothing to debug — the bug that motivated this
check was a container launch that swallowed stderr and reported only a bare
non-zero, so the actual cause was unrecoverable. Fires on:

  * ``devcontainer up`` / ``devcontainer build``
  * ``docker compose … up`` / ``docker compose … build`` (and ``docker-compose``)
  * ``docker build`` / ``docker buildx … build``
  * the same launchers invoked through an array variable, e.g.
    ``DC=(docker compose -p foo …)`` then ``"${DC[@]}" up`` — caught by a
    two-pass scan so the indirection can't smuggle a suppressed launch past us.

The script is parsed with the real bash grammar (``_bash_ast``), which answers
both halves of the question directly. A redirect is a ``file_redirect`` node
beside the command, so its file descriptor, operator and destination are read off
the tree instead of being reconstructed from co-occurring text — and their ORDER
is the tree's order, which is what makes ``2>&1 >/dev/null`` (stderr left on the
original stdout, nothing discarded) different from ``>/dev/null 2>&1``. The verb
is the command's first positional ``word``, so ``docker compose run --build`` is a
``run`` with a flag rather than a ``build`` subcommand, with no lookbehind needed
to tell a flag from a subcommand. A launch quoted inside a printed message holds
no command at all.

A launch that legitimately must discard stderr opts out with a
``# allow-stderr-suppress: <reason>`` on any line the command occupies — including
the continuation line its redirect sits on.

Invoked by pre-commit with the staged shell files as arguments.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    iter_nodes,
    parse,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
    run_line_checks,
)

OPT_OUT = "allow-stderr-suppress"

MESSAGE = (
    "stderr suppressed on a launch/build command — capture and surface it, or "
    f"annotate `# {OPT_OUT}: <reason>`"
)

_DEVNULL = "/dev/null"

# The launch/build subcommands. A verb here counts only in POSITIONAL position,
# which is what separates the `build` subcommand from the `--build` flag.
_VERBS = frozenset({"up", "build"})

# Prefixes that run the following word as the command, so the launcher behind one
# is still the launcher.
_WRAPPERS = frozenset({"sudo", "doas", "command", "env", "exec", "nice", "time"})

# Flags whose VALUE is the next token, so a `-f build.yml` cannot be read as the
# `build` subcommand. Only the launchers' own flags are listed; an unknown flag
# consumes nothing, which errs toward reading the real verb.
_VALUE_FLAGS = frozenset(
    {
        "-f",
        "--file",
        "-p",
        "--project-name",
        "--project-directory",
        "--profile",
        "--env-file",
        "--progress",
        "--ansi",
        "--parallel",
        "-c",
        "--context",
        "-H",
        "--host",
        "--config",
        "-l",
        "--log-level",
    }
)

# Child types of a `command` that carry an argument value.
_ARGUMENT_TYPES = frozenset(
    {"word", "string", "raw_string", "concatenation", "number", "simple_expansion"}
)

# The operator of a redirect that WRITES a stream to a file: `>` truncates, `>>`
# appends, `&>`/`&>>` send both stdout and stderr. `>&` is a descriptor DUP
# (`2>&1`), which points a stream at another stream rather than at a file.
_WRITE_OPERATORS = frozenset({">", ">>"})
_BOTH_OPERATORS = frozenset({"&>", "&>>"})
_DUP_OPERATOR = ">&"


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _unquote(raw: str) -> str:
    """A quoted token's literal text (`"/dev/null"` → `/dev/null`)."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _redirect_parts(redirect) -> tuple[str | None, str, str]:
    """(file descriptor, operator, destination) of a ``file_redirect``.

    The destination is the token right AFTER the operator, not the node's last
    child: the grammar folds a following argument into the same `file_redirect`
    (`foo >/dev/null "a;b"` parks the string there), so reading the last child
    would misread the destination on exactly the commands that also pass
    arguments."""
    children = redirect.children
    descriptor = next(
        (_text(c) for c in children if c.type == "file_descriptor"),
        None,
    )
    index = next(
        (
            i
            for i, child in enumerate(children)
            if child.type in _WRITE_OPERATORS
            or child.type in _BOTH_OPERATORS
            or child.type == _DUP_OPERATOR
        ),
        None,
    )
    if index is None or index + 1 >= len(children):
        return descriptor, "", ""
    return descriptor, children[index].type, _unquote(_text(children[index + 1]))


def _suppresses_stderr(redirects: list) -> bool:
    """True when REDIRECTS leave file descriptor 2 pointing at /dev/null.

    Bash applies redirects LEFT TO RIGHT, each one re-pointing a descriptor, so
    this replays them in the tree's order and reads off where stderr ended up.
    That order is the whole reason to do it this way rather than looking for
    co-occurring tokens: `>/dev/null 2>&1` sends stdout to the bit-bucket and
    THEN dups stderr onto it (suppressed), while `2>&1 >/dev/null` dups stderr
    onto the still-live stdout first and only then moves stdout (stderr
    survives). The two carry the same three tokens."""
    streams = {"1": "", "2": ""}  # "" — still attached to the caller's stream
    for redirect in sorted(redirects, key=lambda node: node.start_byte):
        descriptor, operator, destination = _redirect_parts(redirect)
        if operator in _BOTH_OPERATORS:
            streams["1"] = streams["2"] = destination
        elif operator in _WRITE_OPERATORS:
            streams[descriptor or "1"] = destination
        elif operator == _DUP_OPERATOR and descriptor:
            streams[descriptor] = streams.get(destination, "")
    return streams["2"] == _DEVNULL


def _redirects(command) -> list:
    """Every redirect that applies to COMMAND.

    A redirect is parked on the enclosing ``redirected_statement``, not under the
    command itself — and that statement may wrap a whole group, so
    ``{ docker build .; } >/dev/null 2>&1`` suppresses the build's stderr from an
    ancestor. Hence the walk climbs every ancestor and takes the redirects of
    each ``redirected_statement`` on the way up; a redirect belonging to some
    OTHER command is never on that path, because the grammar puts it under that
    command's own ``redirected_statement`` instead."""
    found: list = []
    node = command
    while node is not None:
        if node is command or node.type == "redirected_statement":
            for child in node.children:
                if child.type == "file_redirect":
                    found.append(child)
                elif child.type == "heredoc_redirect":
                    found += [c for c in child.children if c.type == "file_redirect"]
        node = node.parent
    return found


def _positionals(words: list[str]) -> list[str]:
    """WORDS with wrapper prefixes, environment assignments, flags and flag values
    removed — what is left is the program and its subcommands, in order."""
    while words and (words[0] in _WRAPPERS or "=" in words[0]):
        words = words[1:]
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word.startswith("-"):
            skip = word in _VALUE_FLAGS
            continue
        out.append(word)
    return out


def _is_launch(positionals: list[str]) -> bool:
    """True when POSITIONALS name a container launch or image build."""
    if not positionals:
        return False
    head, rest = positionals[0].rsplit("/", 1)[-1], positionals[1:]
    if head in ("devcontainer", "docker-compose"):
        return bool(rest) and rest[0] in _VERBS
    if head != "docker" or not rest:
        return False
    if rest[0] in ("compose", "buildx"):
        return len(rest) > 1 and rest[1] in _VERBS
    return rest[0] == "build"


def _words(command) -> list[str]:
    """COMMAND's name followed by its argument words, as written."""
    return [
        _text(child)
        for child in command.children
        if child.type == "command_name" or child.type in _ARGUMENT_TYPES
    ]


def _array_values(root) -> dict[str, list[str]]:
    """Every array variable in ROOT mapped to its elements, so a command invoked
    through one — ``DC=(docker compose -f x.yml)`` then ``"${DC[@]}" up`` — is
    read as the command bash actually runs. The verb may come from either half,
    which is why the elements are substituted rather than merely recognized."""
    arrays: dict[str, list[str]] = {}
    for assignment in iter_nodes(root, "variable_assignment"):
        name = next(
            (_text(c) for c in assignment.children if c.type == "variable_name"), None
        )
        array = next((c for c in assignment.children if c.type == "array"), None)
        if name is not None and array is not None:
            arrays[name] = [
                _text(c) for c in array.children if c.type in _ARGUMENT_TYPES
            ]
    return arrays


def _command_words(command, arrays: dict[str, list[str]]) -> list[str]:
    """COMMAND's words, with an array's elements expanded in place of the
    ``"${DC[@]}"`` that names it."""
    words = _words(command)
    if not words:
        return words
    expanded = next(
        (elements for name, elements in arrays.items() if f"${{{name}[" in words[0]),
        None,
    )
    return words if expanded is None else expanded + words[1:]


def violations(text: str) -> list[int]:
    """1-based line numbers in TEXT that suppress stderr on a launch/build."""
    lines = text.split("\n")
    root = parse(text)
    arrays = _array_values(root)  # collected file-wide (two-pass)
    hits = set()
    for command in iter_nodes(root, "command"):
        if not _is_launch(_positionals(_command_words(command, arrays))):
            continue
        redirects = _redirects(command)
        if not _suppresses_stderr(redirects):
            continue
        lineno = command.start_point[0] + 1
        # The opt-out is accepted on any line the flagged command occupies,
        # INCLUDING its redirects' — a continuation-wrapped launch puts the
        # suppression (and so the reason for it) on a later line than the verb.
        last = max(node.end_point[0] + 1 for node in (command, *redirects))
        if not any(annotated(line, OPT_OUT) for line in lines[lineno - 1 : last]):
            hits.add(lineno)
    return sorted(hits)


def main(argv: list[str]) -> int:
    """Run the detector over ARGV through the shared read/report loop, one path at a
    time so a file the grammar refuses to parse safely fails LOUDLY (naming the
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
