#!/usr/bin/env python3
"""Ban an environment-sourced variable inside bash `$(( ))` arithmetic.

A variable read directly inside `$(( ))` (`$((SECONDS + ${TIMEOUT:-90}))`)
trusts its value to be an integer. It routinely is not: a typo or an empty
export makes the expansion an arithmetic SYNTAX ERROR that aborts a `set -e`
caller mid-run, and some garbage values coerce to 0, silently disabling the
limit the arithmetic implements. Remedy: bind the value through a validated
variable FIRST (`[[ "$v" =~ ^[0-9]+$ ]] || v=<default>`), then use that
variable in the arithmetic.

ENVIRONMENT-SOURCED here means an ALL-CAPS name (a common convention for an
externally-set variable) that the script itself does not ASSIGN on an earlier
line. A name the script assigns first — a counter, a `read` target, a loop
variable — holds whatever that assignment put there, so it carries none of
the "might not be an integer" risk this check exists for. Bash's own integer
builtins (`SECONDS`, `RANDOM`, `LINENO`, `BASHPID`, `PPID`, `UID`, `EUID`) are
exempt outright — they are never a caller's env.

Both questions the check asks are structural, so both are put to the real
bash grammar (`_cts_bash_ast`) rather than to a regex: whether a span is a
`$(( ))` arithmetic expansion at all, and whether a name is bound earlier by
a `variable_assignment`, a `for` header, a `declare`/`export`/`local`
variable, or a `read`/`mapfile`/`readarray`/`printf -v` target. A name spelled
out in a comment, inside a string a command prints, or in a heredoc body
carries no `variable_name` node there at all, so none of those can ever
trip this check.

Per-line opt-out: a trailing `# env-arith-ok: <reason>` (a reason is
required), honoured anywhere in a multi-line `$(( ))` expansion's own lines.

Known blind spots:
  * a bare `(( … ))` arithmetic COMMAND (no `$`) carries the same risk and is
    not scanned — only a `$(( ))` expansion is;
  * which argument of `read`/`mapfile`/`readarray`/`printf -v` is a bound
    NAME rather than a flag's value is matched by option text, since the
    grammar parses any command generically and does not know one builtin's
    flags from another's — the value-taking option letters are shared
    across all three, so `read`'s `-t TIMEOUT` and `mapfile`'s boolean
    (no-value) `-t` are not told apart, and `mapfile -t NAME` misreads
    `NAME` as `-t`'s value;
  * "assigned earlier" is by source line, so a function that reads a name
    the script only assigns further down (e.g. in a later function it never
    calls first) still flags;
  * `(( NAME = value ))` (a bare-arithmetic assignment) does not register as
    binding `NAME`, since the grammar spells it as a `binary_expression`
    rather than a `variable_assignment`.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_name,
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

OPT_OUT = "env-arith-ok"

# Bash's own arithmetic-safe builtins — always an integer by construction,
# never a caller's env.
_BUILTINS = frozenset({"SECONDS", "RANDOM", "LINENO", "BASHPID", "PPID", "UID", "EUID"})

# An ALL-CAPS identifier of at least two characters — the convention this
# check reads as "externally set" absent an earlier assignment.
_ALLCAPS_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")

# `read`/`mapfile`/`readarray` commands whose plain-word arguments (after
# skipping a value-taking option and the word right after it) are the names
# they bind.
_READ_LIKE = frozenset({"read", "mapfile", "readarray"})
_READ_VALUE_OPTS = frozenset({"-d", "-i", "-n", "-N", "-p", "-t", "-u", "-c", "-C"})
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

MESSAGE = (
    "an ALL-CAPS variable this script never assigns, inside `$(( ))` — a "
    "non-integer value is an arithmetic syntax error that aborts a `set -e` "
    "caller, and garbage coerced to 0 silently disables the limit. Validate "
    f"the value into a variable first, or annotate `# {OPT_OUT}: <reason>`."
)


def _read_targets(words: list[str]) -> list[str]:
    """The variable names a `read`/`mapfile`/`readarray` argument list binds,
    once its own value-taking options and their values are skipped."""
    names: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word.startswith("-"):
            skip = word in _READ_VALUE_OPTS
            continue
        if _NAME_RE.match(word):
            names.append(word)
    return names


def _assigned_lines(root) -> dict[str, int]:
    """{name: first 1-based line the script binds it on}, over every binding
    form the grammar makes structural: a `variable_assignment` (plain,
    inside `declare`/`export`/`local`/`readonly`/`typeset`, or a C-style
    `for ((…))` header), a `declare`-family name with no `=`, a `for NAME in`
    header, and a `read`/`mapfile`/`readarray`/`printf -v` target."""
    first: dict[str, int] = {}

    def note(name: str, line: int) -> None:
        if name not in first or line < first[name]:
            first[name] = line

    for node in iter_nodes(
        root, "variable_assignment", "for_statement", "declaration_command", "command"
    ):
        lineno = node.start_point[0] + 1
        if node.type == "variable_assignment":
            name_child = next(
                (c for c in node.children if c.type == "variable_name"), None
            )
            if name_child is not None:
                note(node_text(name_child), lineno)
        elif node.type == "for_statement":
            name_child = next(
                (c for c in node.children if c.type == "variable_name"), None
            )
            if name_child is not None:
                note(node_text(name_child), lineno)
        elif node.type == "declaration_command":
            for child in node.children:
                if child.type == "variable_name":
                    note(node_text(child), lineno)
        elif node.type == "command":
            name = command_name(node)
            words = [unquote(w) for w in command_words(node)]
            if name in _READ_LIKE:
                for target in _read_targets(words[1:]):
                    note(target, lineno)
            elif name == "printf" and "-v" in words:
                idx = words.index("-v")
                if idx + 1 < len(words) and _NAME_RE.match(words[idx + 1]):
                    note(words[idx + 1], lineno)
    return first


def violations(text: str) -> list[int]:
    """1-based line numbers where an ALL-CAPS name the script never assigns
    earlier sits inside a `$(( ))` arithmetic expansion."""
    root = parse(text)
    assigned = _assigned_lines(root)
    physical = text.splitlines()
    hits: set[int] = set()
    for arith in iter_nodes(root, "arithmetic_expansion"):
        lineno = arith.start_point[0] + 1
        end_line = arith.end_point[0] + 1
        external = any(
            _ALLCAPS_RE.match(name := node_text(var)) is not None
            and name not in _BUILTINS
            and assigned.get(name, lineno) >= lineno
            for var in iter_nodes(arith, "variable_name")
        )
        if external and not annotated_near(
            physical, lineno, OPT_OUT, span_end=end_line
        ):
            hits.add(lineno)
    return sorted(hits)


def main(argv: list[str]) -> int:
    """Run the detector one path at a time, so a file the grammar refuses to
    parse fails LOUDLY (naming the path, exit 1) instead of silently vanishing
    from the scan, while every other path is still checked."""
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
