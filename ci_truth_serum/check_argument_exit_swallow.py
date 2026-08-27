#!/usr/bin/env python3
"""Ban SC2155's sibling: a command substitution passed as an ARGUMENT to a
locally defined function, in a file that runs under ``set -e``.

Shellcheck's SC2155 flags ``local x="$(cmd)"``: the assignment reports the
status of ``local``, so ``cmd``'s failure is discarded. One syntactic step over
sits the same defect with no shellcheck rule at all::

    my_helper "$(risky_listing)"     # risky_listing exits 3 → my_helper still runs

The shell expands the substitution, throws its exit status away, and calls
``my_helper`` with whatever the failed command printed — usually nothing. Under
``set -e`` the author expects a failure to stop the script; it does not stop
here, because ``set -e`` acts on the status of the OUTER command, and the outer
command succeeded on empty input. A guard built this way passes an empty
allowlist to its helper and reports success.

Correct pattern — assign on its own line first, which DOES propagate (a plain
assignment reports the substitution's status; only ``local``/``declare``/
``export`` in front of it hide the status, which is what SC2155 is about)::

    listing="$(risky_listing)"
    my_helper "$listing"

SCOPE — why only a locally defined function. ``echo "$(date)"`` and
``grep -q "$(marker)" file`` are the bulk of every match and are idiomatic: the
author of a print or a match did not mean the inner failure to stop the script,
and flagging them buries the real finding. A call to a function this tree
DEFINES is the shape where the author owns both sides and meant the failure to
propagate. The callee set is therefore every function defined in the scanned
file plus every function defined in a file it sources, directly or through
another sourced file. A source path the shell computes (``"${DIR}/lib.sh"``) is
resolved by its trailing literal name — first as a sibling of the sourcing file,
then against the tracked shell files — so the common
``source "${SCRIPT_DIR}/lib.sh"`` idiom still contributes its functions.

The whole decision is a node shape (``_cts_bash_ast``), never a text match:

  * The substitution must be an ARGUMENT. The walk from the
    ``command_substitution`` up to its ``command`` passes only value wrappers
    (``string``, ``concatenation``, ``expansion``), so a substitution in a
    ``variable_assignment`` (``x="$(cmd)"``, and SC2155's own
    ``local x="$(cmd)"``) never reaches a ``command`` and is not this rule, and
    one inside ``command_name`` (``"$(get_tool)" --flag``) stops there too.
  * The callee name must be a literal ``word``. A computed name (``"$tool" a``)
    matches no definition, so it cannot be read as a local function.
  * A substitution written inside a printed message is ``string`` content that
    holds no command, and one in a heredoc body is data — neither is reachable.

GATE — the file must enable ``errexit``: a ``set`` command with a short-flag
cluster carrying ``e`` (``set -e``, ``set -euo pipefail``), a ``set -o
errexit``, or a shebang whose flags carry ``e``. Without errexit the script
never claimed a failure would stop it, so there is no broken promise to report.
The shebang is read as text, because a shebang is a comment to the bash grammar
and carries no command node to inspect. A later ``set +e`` does not close the
gate: it turns errexit off for a span this lint does not track, and the
conservative direction for a fail-open detector is to still report.

Opt out on the flagged line, or the line above it, with
``# allow-argument-exit: <reason>``. The reason is REQUIRED; a bare annotation
does not suppress.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_name,
    command_words,
    iter_nodes,
    node_text,
    parse,
    unquote,
)
from _cts_linecheck import (  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    tracked_shell_files,
)

OPT_OUT = "allow-argument-exit"

MESSAGE = (
    "this substitution runs as an argument, so the shell discards its exit "
    "status and `set -e` never sees the failure — the callee runs on empty "
    'output. Assign it on its own line first (`out="$(cmd)"`, which does '
    'propagate) and pass `"$out"`, or annotate '
    f"`# {OPT_OUT}: <reason>`"
)

# The node types that may sit between a `command` and a substitution inside one
# of its arguments. `command_name` is deliberately absent: stopping there is what
# keeps `"$(get_tool)" --flag` — a computed PROGRAM, not an argument — out.
_VALUE_WRAPPERS = frozenset({"string", "concatenation", "expansion"})

# The commands that read another file into the current shell.
_SOURCE_COMMANDS = frozenset({"source", "."})

# A short-flag cluster that turns errexit on: `-e`, `-eu`, `-euo`. A `+e` cluster
# turns it off and is not matched.
_ERREXIT_CLUSTER = re.compile(r"^-[a-zA-Z]*e")


def _enables_errexit(words: list[str]) -> bool:
    """True when WORDS (a `set` command's arguments, or a shebang's) turn errexit
    on: a short-flag cluster carrying `e`, or `-o errexit`."""
    for index, word in enumerate(words):
        if _ERREXIT_CLUSTER.match(word):
            return True
        if word == "-o" and index + 1 < len(words) and words[index + 1] == "errexit":
            return True
    return False


def sets_errexit(text: str, root: Node | None = None) -> bool:
    """True when TEXT runs under ``set -e``.

    Both sources count: a `set` command anywhere in the file (the grammar answers
    where a command is), and the shebang's own flags (`#!/bin/bash -e`), which
    are text — a shebang is a comment to bash, so there is no node to read.
    """
    first_line = text.split("\n", 1)[0]
    if first_line.startswith("#!") and _enables_errexit(first_line.split()[1:]):
        return True
    root = parse(text) if root is None else root
    for command in iter_nodes(root, "command"):
        words = command_words(command)
        if (
            words
            and words[0].rsplit("/", 1)[-1] == "set"
            and _enables_errexit(words[1:])
        ):
            return True
    return False


def defined_functions(text: str, root: Node | None = None) -> set[str]:
    """Every function name TEXT defines, in either the `name() { … }` or the
    `function name { … }` form — the name is the definition's first bare word
    either way."""
    root = parse(text) if root is None else root
    names = set()
    for node in iter_nodes(root, "function_definition"):
        name = next((child for child in node.children if child.type == "word"), None)
        if name is not None:
            names.add(node_text(name))
    return names


def source_targets(text: str, root: Node | None = None) -> list[str]:
    """The path argument of every `source`/`.` command in TEXT, as written.

    A `source`/`.` carrying no argument yields nothing. What a written path
    points at is resolved by the caller, in ``resolve_source``.
    """
    root = parse(text) if root is None else root
    targets = []
    for command in iter_nodes(root, "command"):
        words = command_words(command)
        if len(words) >= 2 and words[0] in _SOURCE_COMMANDS:
            targets.append(unquote(words[1]))
    return targets


def resolve_source(target: str, origin: str, tracked: list[str]) -> str | None:
    """The tracked shell file TARGET names when sourced from ORIGIN, or None.

    A literal TARGET is tried relative to ORIGIN's directory, then relative to
    the working directory (a repo-root-relative path, which is what a script run
    from the root writes).

    A literal TARGET that matches neither is None: the name fallback below would
    widen the callee set to a file this script does not source.

    A computed TARGET (`source "${SCRIPT_DIR}/lib.sh"`) has no literal path to
    try, so its trailing literal NAME is used instead: first as a sibling of
    ORIGIN, which is what the common idiom means, then against TRACKED's
    basenames. A trailing name that is itself computed resolves to None, and so
    does a basename matching more than one tracked file — two candidates cannot
    be told apart here, and picking one would attribute functions the script
    never sourced.
    """
    if "$" not in target and "`" not in target:
        for candidate in (Path(origin).parent / target, Path(target)):
            resolved = str(candidate)
            if resolved in tracked or candidate.is_file():
                return resolved
        return None
    name = target.rsplit("/", 1)[-1]
    if "$" in name or "`" in name or not name:
        return None
    sibling = Path(origin).parent / name
    if sibling.is_file():
        return str(sibling)
    matches = [path for path in tracked if path.rsplit("/", 1)[-1] == name]
    return matches[0] if len(matches) == 1 else None


def _read(path: str) -> str | None:
    """PATH's text, or None when it cannot be read — a path the index still lists
    after a rename, or a binary file with a shell-ish name."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _parse_sourced(text: str, resolved: str) -> Node:
    """TEXT parsed as bash, with a refusal re-raised under the SOURCED file's own
    name, so the report sends a reader to the file that has to change.

    The refusal is never swallowed: dropping a sourced file's functions would
    shrink the callee set silently, and a smaller callee set loses findings —
    the fail-open direction this pack exists to catch.
    """
    try:
        return parse(text)
    except PathologicalInputError as err:
        raise PathologicalInputError(f"sourced file {resolved}: {err}") from err


def sourced_functions(
    text: str, path: str, tracked: list[str], root: Node | None = None
) -> set[str]:
    """Every function name reachable from PATH through `source`, transitively.

    A library that sources a second library contributes both sets, so a call to a
    function two files away is still recognised. Each file is read and parsed
    once, so a source cycle terminates.
    """
    names: set[str] = set()
    seen = {path}
    pending = [(text, path, parse(text) if root is None else root)]
    while pending:
        current_text, current_path, current_root = pending.pop()
        if current_path != path:
            names |= defined_functions(current_text, current_root)
        for target in source_targets(current_text, current_root):
            resolved = resolve_source(target, current_path, tracked)
            if resolved is None or resolved in seen:
                continue
            seen.add(resolved)
            sourced_text = _read(resolved)
            if sourced_text is None:
                continue
            pending.append(
                (sourced_text, resolved, _parse_sourced(sourced_text, resolved))
            )
    return names


def _callee(substitution: Node) -> Node | None:
    """The `command` NODE is an argument of, or None when it is not an argument.

    The walk climbs only value wrappers, so it stops — returning None — at a
    `variable_assignment` (`x="$(cmd)"`, `local x="$(cmd)"`), at a
    `command_name` (`"$(get_tool)" --flag`), and at every other construct that is
    not a command taking arguments.
    """
    node, parent = substitution, substitution.parent
    while parent is not None and parent.type in _VALUE_WRAPPERS:
        node, parent = parent, parent.parent
    if parent is None or parent.type != "command" or node.type == "command_name":
        return None
    return parent


def _literal_name(command: Node) -> str | None:
    """COMMAND's program name when it is written as one literal word, else None.

    A name the shell assembles (`"$tool"`, `${bin}/x`) has children other than a
    single `word`, and no computed name can match a function this tree defines.
    """
    name_node = next(
        (child for child in command.children if child.type == "command_name"), None
    )
    if name_node is None or len(name_node.children) != 1:
        return None
    if name_node.children[0].type != "word":
        return None
    return command_name(command)


def violations(
    text: str,
    functions: frozenset[str] | set[str] = frozenset(),
    root: Node | None = None,
) -> list[int]:
    """1-based line numbers in TEXT where a command substitution is passed as an
    argument to a locally defined function under ``set -e``.

    FUNCTIONS names the functions reachable through `source`; the functions TEXT
    itself defines are always added. Omitting it scans TEXT alone, which is what
    a caller without a path can do.

    The finding is anchored on the SUBSTITUTION — the command whose status is
    lost, and the line a reader writes the annotation on.
    """
    root = parse(text) if root is None else root
    if not sets_errexit(text, root):
        return []
    known = set(functions) | defined_functions(text, root)
    if not known:
        return []
    lines = text.split("\n")
    hits = set()
    for substitution in iter_nodes(root, "command_substitution"):
        callee = _callee(substitution)
        if callee is None or _literal_name(callee) not in known:
            continue
        lineno = substitution.start_point[0] + 1
        if not annotated_near(lines, lineno, OPT_OUT):
            hits.add(lineno)
    return sorted(hits)


def main(argv: list[str]) -> int:
    """Run the detector over ARGV.

    This lint owns its read loop rather than calling ``run_line_checks``, because
    the callee set depends on the PATH (what the file sources), and because a
    file the grammar refuses to parse must fail LOUDLY — naming the path, exit 1
    — instead of being silently skipped, while every remaining path is still
    checked.
    """
    tracked = tracked_shell_files()
    status = 0
    for path in argv:
        text = _read(path)
        if text is None:
            continue
        try:
            root = parse(text)
            found = violations(text, sourced_functions(text, path, tracked, root), root)
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
            continue
        for lineno in found:
            print(f"{path}:{lineno}: {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
