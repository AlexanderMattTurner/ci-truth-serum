#!/usr/bin/env python3
"""Ban a TESTED pipeline that ends in a reader which answers before its input ends.

A pipe holds about 64 KiB. The writer blocks when it has more to send and nobody
takes it. Some readers answer without reading everything: ``grep -q`` stops at the
first matching line, ``head -5`` stops after five lines, ``sed 1q`` stops after
one. The reader exits, the writer's next write finds nobody at the other end, and
the kernel kills the writer with ``SIGPIPE``. ``set -o pipefail`` then makes the
pipeline's status 141, so every ``if``, ``&&`` and ``!`` around it reads 141 as
"the reader said no". A real MATCH becomes a NO-MATCH.

Nothing in the source text says how large the payload will be. The bug therefore
passes every small-input test and fires in production. It is silent and dangerous:
a teardown check that verifies a secret was removed
(``secret_store ls | grep -q "$name"``) reports a still-present credential as gone
the moment the listing outgrows the buffer.

WHAT IS FLAGGED — a pipeline where all of these hold:

* pipefail is in effect where the pipeline runs (see ``_pipefail_start``);
* the pipeline's exit status is READ (``_status_read``): the condition of an
  ``if``/``while``/``until``/``elif``, an operand of ``&&``/``||``, or under
  ``!``. A trailing ``|| true`` / ``|| :`` discards the status, so it is not read;
* the LAST stage is one of the readers in ``_EARLY_EXIT_READERS``;
* the stage feeding that reader is not a bounded builtin (see below).

THE FIX, in order of preference:

* feed the reader without a writer process — ``reader <<<"$payload"`` instead of
  ``printf '%s' "$payload" | reader``. There is then no process to signal, so the
  status is the reader's own;
* read the reader's own status: ``status=${PIPESTATUS[1]}``;
* annotate ``# pipefail-grep-ok: <reason>`` when the payload has a structural size
  bound well below the pipe buffer.

BOUNDED PRODUCERS. ``echo``/``printf``/``:`` with only LITERAL arguments write once
and cannot outrun the buffer, so they are exempt. An argument that expands
(``printf '%s' "$input"``) is not bounded: it writes as many bytes as the variable
holds. That distinction is the whole bug — a sandbox hook wrote an entire agent
tool result through ``printf '%s' "$input" | grep -qiE …`` and read the SIGPIPE as
"no network-failure signature".

HEREDOC SCRIPTS. A ``|`` inside a heredoc body is data to the enclosing shell, but
a body whose first line is a shell shebang is a SCRIPT the enclosing file writes
out, and that script later runs. Each such body is parsed as shell and scanned;
hits are reported at the enclosing file's line numbers, which is where a reader
goes to fix them. The descent is one level: a generator that writes a generator is
not a shape this pack has met.

The script is parsed with tree-sitter-bash (the shared ``_cts_bash_ast`` grammar), so
what the lint sees is what bash would run: a pipeline wrapped across physical lines
is ONE pipeline node, and a reader name inside a string or a comment is text.
Pipefail must be IN EFFECT where the pipeline runs: the first ``set`` command that
turns it on must precede the pipeline in source order, and that command is read off
the grammar (``_enables_pipefail``), so a printed string cannot arm the check. A
pipeline inside a function body is gated on pipefail being set anywhere in the file,
since the body runs at call time. A sourced bash library (no shebang, declaring
``# shellcheck shell=bash``) inherits its strict-mode callers' pipefail, so it is
treated as pipefail-scoped from its first byte.

ONE REGEX ANSWERS A GRAMMAR QUESTION, and it is a deliberate exception. A ``sed``
script is a language of its own, not shell, so the bash grammar cannot say where its
commands begin. No ``tree-sitter-sed`` package exists to ask instead, so ``_SED_QUIT``
matches the quit command by position in the script text. The script itself still
arrives as a word from the AST, so only its INSIDE is scanned by text.

LIMITATION: the reader set is the external programs this lint can name. A stage
that is a shell function, or an interpreter running a script that returns early
(``node dispatch.mjs``), reads as an ordinary command and is not flagged — whether
an arbitrary program drains its standard input is not a question the bash grammar
can answer. A command word that is an expansion (``"$GREP" -q``) is not matched
either: the alternative is to guess what a variable holds.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ARGUMENT_TYPES,
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

# A sourced bash library carries no shebang and declares `# shellcheck shell=bash`. By
# convention such a lib is sourced into strict-mode callers and must NOT re-set shell
# options — so pipefail is active at RUNTIME even though no `set -o pipefail` appears in
# the file. Treating it as pipefail-scoped is what catches the SIGPIPE trap in a sourced
# lib's teardown credential check, which the in-file-only heuristic would miss.
_SHELLCHECK_BASH = re.compile(r"#\s*shellcheck\s+shell=bash\b")

# A heredoc body is a shell script when its FIRST line is a shell shebang. The body's
# own content decides, not the delimiter word or the path it is written to: those are
# naming conventions a generator may not follow, while the shebang is what decides how
# the written file gets run.
_SHELL_SHEBANG = re.compile(r"^#!.*\b(?:ba)?sh\b")

# Builtins that write once. Exempt only when every argument is a literal — see
# `_producer_is_bounded`.
_BOUNDED_PRODUCERS = frozenset({"echo", "printf", ":"})

# Node types whose value the shell computes at run time. An argument containing one
# has no size the source text can bound.
_EXPANDING_TYPES = (
    "simple_expansion",
    "expansion",
    "command_substitution",
    "process_substitution",
    "arithmetic_expansion",
)

_ALLOW = "pipefail-grep-ok"

# The tokens separating pipeline stages in the grammar.
_PIPE_TOKENS = frozenset({"|", "|&"})

# Statements that read a pipeline's exit status as a boolean, and the children that end
# their CONDITION part. A `while` holds its body in a `do_group` node while an `if`
# holds its own after a bare `then` token, so both spellings are listed — a pipeline
# after either is the body, not the condition.
_CONDITION_PARENTS = frozenset(
    {"if_statement", "while_statement", "until_statement", "elif_clause"}
)
_CONDITION_END_TYPES = frozenset({"then", "do_group", "elif_clause", "else_clause"})

# The `||` right-hand sides that DISCARD the left side's status. Both always succeed
# and do nothing, so `cmd || true` runs the same thing next whatever `cmd` returned.
_STATUS_DISCARD_WORDS = frozenset({"true", ":"})

# The grep spellings this pack recognizes, and the options that make grep answer before
# end of input: `-q`/`--quiet`/`--silent` (answer at the first matching line), `-m N`/
# `--max-count` (answer at the Nth), `-l`/`--files-with-matches` (answer at the first).
# `-L` and `-c` must read everything to answer, so the short letters are matched
# case-sensitively on purpose.
_GREP_COMMANDS = frozenset({"grep", "egrep", "fgrep", "ggrep", "rg"})
_GREP_EARLY_LETTERS = frozenset("qlm")
_GREP_EARLY_LONG = frozenset(
    {"--quiet", "--silent", "--max-count", "--files-with-matches"}
)

# `head`'s count options. A NEGATIVE count (`head -n -5`) means "all but the last five",
# which head can only know at end of input, so it drains and is safe.
_HEAD_COUNT_OPTS = frozenset({"-n", "-c", "--lines", "--bytes"})

# One sed address: a line number, `$`, or a `/…/` regex whose delimiter a backslash can
# escape.
_SED_ADDRESS = r"(?:[0-9$]+|/(?:\\.|[^/])*/)"

# A `q`/`Q` sitting where a sed COMMAND goes: at the start of the script or after a
# `;`/`}`/newline, optionally behind an address, an address range or a negated address
# (`1q`, `$q`, `/marker/q`, `2,3q`, `$!q`) and optionally carrying an exit code (`q5`).
# Anchoring on the command position is what keeps `sed 's/query/x/'` — whose `q` is
# inside a regex — unflagged.
_SED_QUIT = re.compile(
    rf"""(?:^|[;{{}}\n])\s*                            # script start, or after a separator
        (?:{_SED_ADDRESS}(?:,{_SED_ADDRESS})?!?\s*)?  # optional address, range or negation
        [qQ](?:\s*[0-9]+)?\s*                         # the quit command, optional exit code
        (?:[;}}\n]|$)""",
    re.VERBOSE,
)


def _enables_pipefail(node) -> bool:
    """True when NODE is a `set` command that turns pipefail ON.

    Read off the grammar, not the command's text. bash gives `-o` its option name in
    the NEXT word, so this asks for a short-flag cluster ending in `o` (`-o`, `-euo`,
    `-Eeuo`) followed by the word `pipefail`. `set +o pipefail` DISABLES it and starts
    with `+`, so it never matches.

    The text scan this replaces read a `command` node's whole source, which meant an
    ARGUMENT could arm the check: `echo "set -euo pipefail"` prints a string and
    changes no shell option, and every pipeline after it was judged as though pipefail
    were on. Whether a token is a command word or a string a command prints is a
    structural question, and only the grammar answers it."""
    if _command_name(node) != "set":
        return False
    words = [unquote(w) for w in command_words(node)[1:]]
    return any(
        word.startswith("-")
        and not word.startswith("--")
        and word.endswith("o")
        and words[index + 1] == "pipefail"
        for index, word in enumerate(words[:-1])
    )


def _command_name(node) -> str | None:
    """The basename of NODE's command word (`/bin/grep` -> `grep`), or None when NODE
    is not a simple command or its name is computed.

    A `! cmd` pipeline stage parses as a `negated_command` wrapping the command — unwrap
    it so the negation cannot hide the command's identity. A name carrying a `$` is an
    expansion: what it runs is a run-time fact, so it names no program here."""
    while node.type == "negated_command" and node.children:
        node = node.children[-1]
    if node.type != "command":
        return None
    for child in node.children:
        if child.type == "command_name":
            raw = node_text(child)
            return None if "$" in raw else unquote(raw).rsplit("/", 1)[-1]
    return None


def _argument_words(node) -> list[str]:
    """NODE's argument text, one entry per argument, one layer of quotes removed.

    Only the `command`'s argument children are read, so a redirect (`2>/dev/null`) and
    an environment prefix (`LC_ALL=C`) never reach a caller looking for an option."""
    while node.type == "negated_command" and node.children:
        node = node.children[-1]
    return [
        unquote(node_text(child))
        for child in node.children
        if child.type in ARGUMENT_TYPES
    ]


def _short_cluster_letters(word: str) -> str:
    """The option letters of a short flag cluster (`-qiF` -> `qiF`), or `""` when WORD
    is not one. A long option, a bare `-`, and grep's `-5` context count all return
    `""`, so a caller testing for a letter never matches them."""
    if not word.startswith("-") or word.startswith("--") or len(word) < 2:
        return ""
    return "" if word[1].isdigit() else word[1:]


def _grep_exits_early(words: list[str]) -> bool:
    """True when this grep invocation answers before end of input."""
    for word in words:
        if word == "--":
            return False  # everything after is a pattern or a path, never an option
        if word.split("=", 1)[0] in _GREP_EARLY_LONG:
            return True
        if _GREP_EARLY_LETTERS & set(_short_cluster_letters(word)):
            return True
    return False


def _head_exits_early(words: list[str]) -> bool:
    """True unless this head invocation carries a NEGATIVE count, which makes it read
    to end of input before it can print anything."""
    for index, word in enumerate(words):
        if word in _HEAD_COUNT_OPTS:
            value = words[index + 1] if index + 1 < len(words) else ""
        elif word.split("=", 1)[0] in _HEAD_COUNT_OPTS and "=" in word:
            value = word.split("=", 1)[1]
        else:
            continue
        if value.startswith("-"):
            return False
    return True


def _sed_exits_early(words: list[str]) -> bool:
    """True when this sed invocation's script carries a `q`/`Q` quit command."""
    return any(_SED_QUIT.search(word) for word in words)


_EARLY_EXIT_READERS: dict[str, Callable[[list[str]], bool]] = {
    **dict.fromkeys(_GREP_COMMANDS, _grep_exits_early),
    "head": _head_exits_early,
    "sed": _sed_exits_early,
}


def _exits_before_draining(node) -> bool:
    """True when NODE runs a program that can answer before its input ends."""
    check = _EARLY_EXIT_READERS.get(_command_name(node) or "")
    return check is not None and check(_argument_words(node))


def _is_literal(node) -> bool:
    """True when NODE's text is fixed at parse time — no expansion, no substitution."""
    return not any(True for _ in iter_nodes(node, *_EXPANDING_TYPES))


def _producer_is_bounded(node) -> bool:
    """True when the stage feeding the reader writes a single bounded chunk.

    `echo hello` and `printf '%s\\n' done` qualify. `printf '%s' "$input"` does not: it
    writes as many bytes as `$input` holds, which is unbounded and is exactly how the
    real defect reached production."""
    while node.type == "negated_command" and node.children:
        node = node.children[-1]
    if _command_name(node) not in _BOUNDED_PRODUCERS:
        return False
    return all(
        _is_literal(child) for child in node.children if child.type in ARGUMENT_TYPES
    )


def _in_condition(node) -> bool:
    """True when NODE sits in the CONDITION part of its enclosing `if`/`while`."""
    parent = node.parent
    if parent is None or parent.type not in _CONDITION_PARENTS:
        return False
    for child in parent.children:
        if child.id == node.id:
            return True
        if child.type in _CONDITION_END_TYPES:
            return False
    return False


def _status_discarded(node) -> bool:
    """True when NODE is the left operand of a `|| true` / `|| :` — the shell's way of
    saying the status does not matter, so nothing can misread it."""
    parent = node.parent
    if parent is None or parent.type != "list":
        return False
    children = parent.children
    index = next(i for i, child in enumerate(children) if child.id == node.id)
    if index + 2 >= len(children) or children[index + 1].type != "||":
        return False
    return _command_name(children[index + 2]) in _STATUS_DISCARD_WORDS


def _status_read(pipeline, negated: bool) -> bool:
    """True when PIPELINE's exit status changes what runs next.

    A status that only reaches `set -e` is not counted: whether `-e` is in force there
    is a run-time fact (an `if` around a caller, a `set +e`, a sourcing script), so the
    text cannot say. That gate is the precision lever for the wider reader set — it cut
    a naive trigger from 43 hits to 14 over the consumer tree, losing no true positive.
    """
    if _status_discarded(pipeline):
        return False
    parent = pipeline.parent
    return (
        negated
        or _in_condition(pipeline)
        or (parent is not None and parent.type == "list")
    )


def _pipefail_start(text: str, root) -> int | None:
    """The byte offset from which pipefail is in effect, or None when it never is.

    A sourced bash library (no shebang + `# shellcheck shell=bash`) inherits strict
    mode from its callers, so it is scoped from byte 0. Otherwise the first `set`
    command that enables pipefail marks the start."""
    physical = text.splitlines()
    no_shebang = not (physical and physical[0].startswith("#!"))
    if no_shebang and any(_SHELLCHECK_BASH.search(raw) for raw in physical[:5]):
        return 0
    starts = [
        node.start_byte
        for node in iter_nodes(root, "command")
        if _enables_pipefail(node)
    ]
    return min(starts) if starts else None


def _in_function(node) -> bool:
    """True when NODE sits inside a function body — executed at call time, not at
    its source position."""
    current = node.parent
    while current is not None:
        if current.type == "function_definition":
            return True
        current = current.parent
    return False


def _scan(text: str, root) -> list[int]:
    """1-based violating lines in ONE shell source, ignoring any heredoc scripts.

    ROOT is TEXT's already-parsed tree — the caller holds it because it also walks it
    for heredoc bodies, and one parse per source is the cost this pack budgets."""
    start = _pipefail_start(text, root)
    if start is None:
        return []
    physical = text.splitlines()
    hits: set[int] = set()
    for pipeline in iter_nodes(root, "pipeline"):
        # A pipeline that runs before pipefail is enabled returns the reader's own
        # status — no SIGPIPE false-negative is possible there. A function body is the
        # exception: it executes at call time, after a later `set -o pipefail`.
        if pipeline.start_byte < start and not _in_function(pipeline):
            continue
        stages = [c for c in pipeline.children if c.type not in _PIPE_TOKENS]
        if len(stages) < 2:
            continue
        negated = stages[0].type == "negated_command"
        if not _status_read(pipeline, negated):
            continue
        # Only the LAST stage decides the pipeline's answer, so only its early exit can
        # be misread as that answer.
        reader = stages[-1]
        if not _exits_before_draining(reader) or _producer_is_bounded(stages[-2]):
            continue
        if annotated_near(
            physical,
            pipeline.start_point[0] + 1,
            _ALLOW,
            span_end=pipeline.end_point[0] + 1,
        ):
            continue
        hits.add(reader.start_point[0] + 1)
    return sorted(hits)


def heredoc_scripts(root) -> list[tuple[int, str]]:
    """Every heredoc body under ROOT that is itself a shell script, as
    `(1-based line of the body's first line, body source)`.

    The enclosing shell treats such a body as inert text, and correctly so. The file it
    writes is not inert: it is a hook that later runs. Four SIGPIPE-misreading hooks
    reached a sandbox image that way, invisible to every shell lint."""
    found = []
    for body in iter_nodes(root, "heredoc_body"):
        source = node_text(body)
        first = source.splitlines()[:1]
        if first and _SHELL_SHEBANG.match(first[0]):
            found.append((body.start_point[0] + 1, source))
    return found


def violations(text: str) -> list[int]:
    """1-based line numbers of TEXT's tested pipelines that end in an early-exit reader
    under pipefail, without a ``# pipefail-grep-ok:`` annotation.

    Heredoc bodies that hold a shell script are scanned too, and their hits are shifted
    to the enclosing file's line numbers. Each such body is its own file for the purpose
    of the pipefail gate and the annotation, because it becomes its own file."""
    root = parse(text)
    hits = _scan(text, root)
    for body_line, source in heredoc_scripts(root):
        hits += [body_line + inner - 1 for inner in _scan(source, parse(source))]
    return sorted(set(hits))


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "tested pipeline ending in a reader that stops early under `set -o pipefail`: "
        "the reader's early exit SIGPIPEs the still-writing producer, and pipefail "
        "surfaces exit 141 so a MATCH reads as NO-MATCH. Feed the reader a here-string "
        '(`reader PAT <<<"$out"`) so there is no writer to signal, read '
        "`${PIPESTATUS[N]}` for the reader's own status, or annotate "
        "`# pipefail-grep-ok: <reason>` when the input has a size bound below the "
        "64 KiB pipe buffer.",
    )


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
