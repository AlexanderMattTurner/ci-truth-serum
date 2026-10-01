#!/usr/bin/env python3
"""Cap the length of a program written as a shell string.

A python, node, perl or ruby program passed as `-c`/`-e` text, or fed to the
interpreter's stdin from a heredoc, is a STRING to ruff, pyright, eslint and
coverage. No linter reads it, so it ships unchecked. The bash grammar
(``_cts_bash_ast``) finds three shapes:

  * ``python3 -c '<program>'``: the program is a literal argument.
  * ``python3 - <<'PY' … PY``: a heredoc feeds stdin. With ``-m`` or a FILE
    operand, the heredoc is input data, not a program.
  * ``read -r -d '' VAR <<'PY'`` or ``VAR='…'``, then ``python3 -c "$VAR"``.

A quoted heredoc body that starts with a shebang is a generated shell script,
and the check reads it one level deep. A shell file passes as its own path. A
workflow or composite action passes as its YAML path, and each `run:` value is
read. A `run:` value that `check-inline-run-length` already reports is left to
that check, so one span gets one finding. Tune `--max-lines` and `--max-chars`.
Opt out with `# allow-inline-program: <reason>` on the call or above it.
"""

import argparse
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import yaml
from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ARGUMENT_TYPES,
    PathologicalInputError,
    command_name,
    command_words,
    iter_nodes,
    node_text,
    parse,
    program_name,
    unquote,
)
from _cts_comments import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    shell_comments,
    yaml_comments,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    run_line_checks,
    yaml_run_scalars,
    yaml_run_script,
    yaml_scannable,
)
from check_inline_run_length import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _check_script as _inline_run_report,
)

OPT_OUT = "allow-inline-program"

# A probe at or below these sizes costs more as a file than inline.
DEFAULT_MAX_LINES = 5
DEFAULT_MAX_CHARS = 200

# Interpreters whose language has a linter, type checker or coverage tool.
INTERPRETERS = frozenset({"python", "python3", "node", "perl", "ruby", "deno", "bun"})

# Options whose VALUE is the program: `-c` (python, ruby), `-e`/`--eval` (node,
# perl, ruby), and perl's implicit-loop spellings `-ne`/`-pe`.
PROGRAM_OPTIONS = frozenset({"-c", "-e", "--eval", "-ne", "-pe"})

# An option that NAMES the program file, so a heredoc beside it is input.
MODULE_OPTIONS = frozenset({"-m"})

# Preload options that consume the next word without naming the program.
VALUE_OPTIONS = frozenset({"-r", "--require"})

# Command words that run the next command unchanged. `--wrapper` adds more.
TRANSPARENT_PREFIXES = frozenset(
    {"env", "sudo", "exec", "command", "time", "nohup", "builtin"}
)

# The `NAME=value` shape of an `env` binding, which the grammar reads as a word.
_ENV_BINDING = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")

# The paths whose YAML holds `run:` values that GitHub hands to a shell.
_WORKFLOW_PATH = re.compile(r"(?:^|/)\.github/(?:workflows|actions)/.*\.ya?ml$")

# How deep to read a generated shell script inside a heredoc body.
_MAX_SHELL_DEPTH = 1


class Limits(NamedTuple):
    """The thresholds and the extra wrapper words for one run."""

    lines: int = DEFAULT_MAX_LINES
    chars: int = DEFAULT_MAX_CHARS
    wrappers: frozenset[str] = TRANSPARENT_PREFIXES


class UnscannableWorkflowError(ValueError):
    """The YAML scanner cannot read a workflow, so its `run:` values are unknown."""


def significant_lines(program: str) -> int:
    """Lines of PROGRAM that carry code. Blank lines and `#`/`//` comment lines
    do not count."""
    return sum(
        1
        for line in (raw.strip() for raw in program.splitlines())
        if line and not line.startswith(("#", "//"))
    )


def _is_long(program: str, limits: Limits) -> bool:
    """True when PROGRAM is past either threshold. Every site uses this one
    test, so a binding and its use agree on what is long."""
    return significant_lines(program) > limits.lines or len(program) > limits.chars


def _arguments(command: Node, limits: Limits) -> list[Node] | None:
    """COMMAND's argument nodes when it runs an interpreter, else None.

    A transparent wrapper (`env -i PATH=… python3`) is stepped over with its own
    options and `NAME=value` words. Leading options are stripped only after a
    wrapper, so a real command keeps its arguments.
    """
    words = [
        child
        for child in command.children
        if child.type == "command_name" or child.type in ARGUMENT_TYPES
    ]
    while words:
        text = unquote(node_text(words[0]))
        if _ENV_BINDING.match(text):
            words = words[1:]
        elif program_name(text) in limits.wrappers:
            words = words[1:]
            while words and node_text(words[0]).startswith("-"):
                words = words[1:]
        else:
            break
    if not words or program_name(node_text(words[0])) not in INTERPRETERS:
        return None
    return words[1:]


def _heredoc_body(command: Node) -> Node | None:
    """The heredoc body COMMAND reads, when it reads one.

    The grammar hangs the heredoc on the enclosing `redirected_statement`, a
    sibling of the command, and only that statement's first child reads it.
    """
    parent = command.parent
    if parent is None or parent.type != "redirected_statement":
        return None
    if parent.children[0] != command:
        return None
    for redirect in parent.children:
        if redirect.type != "heredoc_redirect":
            continue
        for child in redirect.children:
            if child.type == "heredoc_body":
                return child
    return None


def _reads_stdin_program(args: list[str]) -> bool:
    """True when the interpreter reads its PROGRAM from stdin, not a file or `-m`.

    A bare `-` names stdin, and the words after it are the program's argv. The
    grammar keeps that `-` only when a word follows it, so no `-` also means stdin.
    """
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word == "-":
            return True
        elif word in MODULE_OPTIONS:
            return False
        elif word.startswith("-"):
            skip = word in VALUE_OPTIONS
        else:
            return False
    return True


def _referenced_names(node: Node) -> set[str]:
    """The variable names NODE expands. A `raw_string` and a quoted heredoc hold
    no expansion nodes, so their `$NAME` text is not a reference."""
    return {
        node_text(name)
        for expansion in iter_nodes(node, "simple_expansion", "expansion")
        for name in expansion.children
        if name.type == "variable_name"
    }


def _long_bindings(root: Node, limits: Limits) -> set[str]:
    """Names bound to a long program, through `read … NAME <<'PY'` or `NAME='…'`."""
    bound: set[str] = set()
    for command in iter_nodes(root, "command"):
        body = _heredoc_body(command)
        if program_name(command_name(command) or "") != "read" or body is None:
            continue
        if _is_long(node_text(body), limits):
            words = [unquote(word) for word in command_words(command)[1:]]
            bound.update(word for word in words if word.isidentifier())
    for assignment in iter_nodes(root, "variable_assignment"):
        name = assignment.child_by_field_name("name")
        value = assignment.child_by_field_name("value")
        if name is None or value is None:
            continue
        # A `$(…)` value binds a command's OUTPUT, not the text written here.
        if next(iter_nodes(value, "command_substitution"), None) is not None:
            continue
        if _is_long(unquote(node_text(value)), limits):
            bound.add(node_text(name))
    return bound


def _program(args: list[Node], command: Node) -> Node | None:
    """The node that holds COMMAND's program: an option's value, or the heredoc
    body when the interpreter reads stdin. None when it runs a file."""
    texts = [node_text(arg) for arg in args]
    for index, text in enumerate(texts[:-1]):
        if text in PROGRAM_OPTIONS:
            return args[index + 1]
    if _reads_stdin_program(texts):
        return _heredoc_body(command)
    return None


def _sites(script: str, limits: Limits, depth: int) -> Iterator[tuple[int, int]]:
    """(first line, last line) of each call in SCRIPT that runs a long program.

    One entry per PROGRAM, so a line with two programs appears twice.
    """
    root = parse(script)
    bound = _long_bindings(root, limits)
    for command in iter_nodes(root, "command"):
        span = (command.start_point[0] + 1, command.end_point[0] + 1)
        args = _arguments(command, limits)
        if args is None:
            body = _heredoc_body(command) if depth < _MAX_SHELL_DEPTH else None
            if body is not None and node_text(body).lstrip().startswith("#!"):
                inner = _violations(node_text(body), limits, depth + 1)
                yield from [span] * len(inner)
            continue
        program = _program(args, command)
        if program is None:
            continue
        text = node_text(program)
        if program.type != "heredoc_body":
            text = unquote(text)
        if _is_long(text, limits) or _referenced_names(program) & bound:
            yield span


def _comment_view(comments: dict[int, str], count: int) -> list[str]:
    """COMMENTS as one entry per line of a COUNT-line text, blank where none."""
    return [comments.get(line, "") for line in range(1, count + 1)]


def _violations(text: str, limits: Limits, depth: int) -> list[int]:
    """1-based lines of TEXT that run a long program with no opt-out.

    The opt-out is read from real shell comments only, so a program that
    quotes the marker in its own text cannot exempt itself.
    """
    lines = text.split("\n")
    view = _comment_view(shell_comments(text), len(lines))
    return sorted(
        start
        for start, end in _sites(text, limits, depth)
        if not annotated_near(lines, start, OPT_OUT, span_end=end, comments=view)
    )


def violations(text: str, limits: Limits = Limits()) -> list[int]:
    """1-based lines of shell TEXT that run a program past LIMITS."""
    return _violations(text, limits, 0)


def workflow_violations(text: str, limits: Limits = Limits()) -> list[int]:
    """1-based lines of workflow TEXT whose `run:` values run a long program.

    The opt-out sits in a real YAML comment or a real shell comment. A `run:`
    value that `check-inline-run-length` reports is skipped: extracting it to a
    shell file moves its programs under this check.
    """
    if not yaml_scannable(text):
        raise UnscannableWorkflowError(
            "the YAML scanner cannot read this workflow, so no `run:` value "
            "was checked. Fix the YAML syntax and run the check again."
        )
    lines = text.split("\n")
    view = _comment_view(yaml_comments(text), len(lines))
    hits: list[int] = []
    for scalar in yaml_run_scalars(text):
        value = yaml.compose(text[scalar.start : scalar.end])
        if isinstance(value, yaml.ScalarNode) and _inline_run_report(value.value, ""):
            continue
        offset = text.count("\n", 0, scalar.start)
        for start, end in _sites(yaml_run_script(text, scalar), limits, 0):
            line, last = offset + start, offset + end
            if not annotated_near(lines, line, OPT_OUT, span_end=last, comments=view):
                hits.append(line)
    return sorted(hits)


def _detector(path: str, limits: Limits):
    """The detector for PATH, or None when PATH is YAML that holds no `run:`."""
    normal = path.replace("\\", "/")
    if _WORKFLOW_PATH.search(normal):
        return lambda text: workflow_violations(text, limits)
    if normal.endswith((".yaml", ".yml")):
        return None
    return lambda text: violations(text, limits)


def main(argv: list[str]) -> int:
    """Check each path in ARGV. A file the grammar or the YAML scanner cannot
    read fails loudly with its path, and the other paths are still checked."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument(
        "--wrapper",
        action="append",
        default=[],
        metavar="NAME",
        help="a command word that runs the next command unchanged (repeatable)",
    )
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    limits = Limits(
        args.max_lines, args.max_chars, TRANSPARENT_PREFIXES | set(args.wrapper)
    )
    message = (
        f"a program of more than {limits.lines} significant lines or "
        f"{limits.chars} characters is written as a shell string. Linters, type "
        "checkers and coverage tools read past a string, so the program ships "
        "unchecked. Move it to its own file and run that file. Or annotate "
        f"`# {OPT_OUT}: <reason>`."
    )
    status = 0
    for path in args.files:
        detector = _detector(path, limits)
        if detector is None:
            continue
        try:
            status = max(status, run_line_checks([path], detector, message))
        except (PathologicalInputError, UnscannableWorkflowError) as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
