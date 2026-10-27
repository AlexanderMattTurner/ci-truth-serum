"""The shell a CI step really runs: its own `run:` body and the repo files it reaches.

PROBLEM CLASS — a lint that reads only the `run:` text misses everything a step
does through a repository script. `run: bash .github/scripts/scan.sh` is one
command on the page. The download it causes sits two files away, in a function
that a sourced library defines. This module follows those edges on the bash
grammar (`_cts_bash_ast`) and hands a caller every command that runs, each with
the file it is written in.

A NAMESPACE is one shell process: an entry script plus every file it sources.
Functions and variables are shared across it. Three edges are followed:

  * `source X` and `. X` add X to the namespace;
  * a call to a function the namespace defines walks that function's body,
    and a body nobody calls is never walked;
  * `bash X`, `sh X` and `./X.sh` start a child namespace, and a Python
    interpreter handed `X.py` (`python3 X.py`, `uv run X.py`, `python -m m`)
    records X for the caller.

A path is resolved only from what the text fixes: a literal, a `# shellcheck
source=` directive, `${BASH_SOURCE[0]%/*}`, `$(dirname "$0")`, `$(cd X && pwd)`,
`$(git rev-parse --show-toplevel)`, `$GITHUB_WORKSPACE`, a variable with one
assignment of those forms, and the default of `${X:-default}` when no file
assigns X. Any other path ends the walk silently: a guessed path
would judge a file the step never runs. The depth is bounded and each file is
read once per namespace, so a cycle terminates.
"""

import functools
import os
import posixpath
import re
import sys
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ARGUMENT_TYPES,
    _inside_definition,
    _literal,
    command_arguments,
    command_words,
    iter_nodes,
    node_text,
    parse,
    unquote,
)
from _cts_comments import shell_comments  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_py_imports import interpreter_scripts  # noqa: E402,I001  # pylint: disable=wrong-import-position
from check_shell_source_declarations import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _DIRECTIVE,
    _comment_block_above,
    _directive_line,
    resolve_target,
)

# How many `bash X` / interpreter hops a walk follows from the `run:` body.
MAX_HOPS = 6

_SHELL_INTERPRETER = re.compile(r"(?:ba|da|z|k)?sh")
_SHELL_SUFFIX = re.compile(r"\.(?:sh|bash)$")
_SOURCE_COMMANDS = frozenset({"source", "."})
_ACTION_PATH_EXPR = re.compile(r"\$\{\{\s*github\.action_path\s*\}\}")


class Unit(NamedTuple):
    """One parsed shell text: a followed file, or a `run:` body (PATH None).
    ENTRY is the script `$0` names in the process that runs it."""

    path: Path | None
    text: str
    root: Node
    entry: Path | None


@functools.lru_cache(maxsize=512)
def load_shell(path: Path) -> tuple[str, Node]:
    """The text of the shell file PATH and its parse tree, read once."""
    text = path.read_text(encoding="utf-8")
    return text, parse(text)


_comments = functools.lru_cache(maxsize=512)(shell_comments)


def _unit(path: Path, entry: Path | None) -> Unit:
    text, root = load_shell(path)
    return Unit(path, text, root, entry)


@functools.lru_cache(maxsize=65536)
def _executed(node: Node) -> list[Node]:
    """The commands under NODE that run when NODE runs: a nested function
    definition runs nothing until something calls it."""
    return [c for c in iter_nodes(node, "command") if not _inside_definition(c, node)]


@functools.lru_cache(maxsize=1024)
def _index(root: Node) -> tuple[list[tuple[str, Node]], list[tuple[str, Node]]]:
    """(name, body) of each function ROOT defines, and (name, node) of each
    variable it assigns, at any depth."""
    functions = []
    for definition in iter_nodes(root, "function_definition"):
        name = definition.child_by_field_name("name")
        body = definition.child_by_field_name("body")
        if name is not None and body is not None:
            functions.append((node_text(name), body))
    assignments = []
    for assignment in iter_nodes(root, "variable_assignment"):
        name = assignment.child_by_field_name("name")
        if name is not None:
            assignments.append((node_text(name), assignment))
    return functions, assignments


@functools.lru_cache(maxsize=65536)
def tokens(command: Node) -> list[str]:
    """COMMAND's name and argument words after quote removal."""
    return [unquote(node_text(node)) for node in command_arguments(command)]


class Namespace:
    """One shell process: the units it reads, and every command it runs."""

    def __init__(self, repo_root: Path, entry: Unit, action_dir: str | None):
        self.repo_root = repo_root
        self.action_dir = action_dir
        self.units: list[Unit] = []
        self.functions: dict[str, list[tuple[Node, Unit]]] = {}
        self.assignments: dict[str, list[tuple[Node, Unit]]] = {}
        self.executed: list[tuple[Node, Unit]] = []
        self._resolving: set[str] = set()
        self._variables: dict[str, str | None] = {}
        self._values: dict[int, str | None] = {}
        self._bodies: dict[str, list[tuple[Node, Unit]]] = {}
        self._add(entry)
        self._walk()

    # ── building ────────────────────────────────────────────────────────────

    def _add(self, unit: Unit) -> None:
        # A new file can add a second assignment, so a value read before it
        # was sourced may no longer be fixed.
        self._variables.clear()
        self._values.clear()
        self.units.append(unit)
        functions, assignments = _index(unit.root)
        for name, body in functions:
            self.functions.setdefault(name, []).append((body, unit))
        for name, assignment in assignments:
            self.assignments.setdefault(name, []).append((assignment, unit))

    def _walk(self) -> None:
        """Walk to a fixed point. A function a later-sourced file defines may be
        called by a command walked before that file was read, so the words of
        every executed command stay in MENTIONED until a definition appears."""
        pending: list[tuple[Node, Unit]] = [(self.units[0].root, self.units[0])]
        mentioned: set[str] = set()
        walked: set[str] = set()
        while pending:
            while pending:
                node, unit = pending.pop(0)
                for command in _executed(node):
                    self.executed.append((command, unit))
                    mentioned.update(tokens(command))
                    added = self._sourced(command, unit)
                    if added is not None:
                        pending.append((added.root, added))
            for name in sorted((mentioned & self.functions.keys()) - walked):
                walked.add(name)
                pending += self.functions[name]

    def _sourced(self, command: Node, unit: Unit) -> Unit | None:
        words = tokens(command)
        if len(words) < 2 or words[0] not in _SOURCE_COMMANDS:
            return None
        target = self._source_target(command, unit)
        if target is None or any(u.path == target for u in self.units):
            return None
        added = _unit(target, unit.entry)
        self._add(added)
        return added

    def _source_target(self, command: Node, unit: Unit) -> Path | None:
        """The file a `source` reads: its argument's value, else the
        `# shellcheck source=` directive above it."""
        argument = command_arguments(command)[1]
        found = self.file(self.value(argument, unit))
        if found is not None:
            return found
        comments = _comments(unit.text)
        block = _comment_block_above(comments, _directive_line(command))
        directive = next((m for line in block if (m := _DIRECTIVE.match(line))), None)
        if directive is None or directive.group("target").startswith("/"):
            return None  # an absolute directive is shellcheck's do-not-follow marker
        directory = unit.path.parent if unit.path else self.repo_root
        found = resolve_target(directory, directive.group("target"), self.repo_root, [])
        return self.file(str(found)) if found is not None else None

    # ── values ──────────────────────────────────────────────────────────────

    def file(self, value: str | None) -> Path | None:
        """The repo file VALUE names, or None. A relative path resolves against
        the repo root, which is the working directory GitHub starts a step in."""
        if not value:
            return None
        path = Path(os.path.normpath(self.repo_root / value))
        if not path.is_relative_to(self.repo_root) or not path.is_file():
            return None
        return path

    def variable(self, name: str, unit: Unit) -> str | None:
        """NAME's value when the text fixes it, else None."""
        if name == "0":
            return str(unit.entry) if unit.entry else None
        if name == "BASH_SOURCE":
            return str(unit.path) if unit.path else None
        if name == "GITHUB_WORKSPACE":
            return str(self.repo_root)
        if name == "GITHUB_ACTION_PATH" and self.action_dir:
            return str(self.repo_root / self.action_dir)
        if name in self._variables:
            return self._variables[name]
        if name in self._resolving or name not in self.assignments:
            return None
        self._resolving.add(name)
        values = {self._assigned(node, owner) for node, owner in self.assignments[name]}
        self._resolving.discard(name)
        # Assigned twice, or from something only the run knows: no fixed value.
        found = values.pop() if len(values) == 1 and None not in values else None
        self._variables[name] = found
        return found

    def _assigned(self, assignment: Node, unit: Unit) -> str | None:
        value = assignment.child_by_field_name("value")
        if value is None:
            return ""
        return self.value(value, unit)

    def value(self, node: Node, unit: Unit) -> str | None:
        """NODE's value after expansion when the text fixes it, else None."""
        if node.id not in self._values:
            self._values[node.id] = self._value(node, unit)
        return self._values[node.id]

    def _value(self, node: Node, unit: Unit) -> str | None:
        kind = node.type
        if kind in ("word", "raw_string", "number"):
            return unquote(node_text(node))
        if kind == "command_name":
            return self.value(node.children[0], unit) if node.children else None
        if kind == "concatenation":
            return self._joined(node.children, unit)
        if kind == "string":
            return self._string(node, unit)
        if kind == "simple_expansion":
            name = next((c for c in node.children if c.is_named), None)
            return self.variable(node_text(name), unit) if name is not None else None
        if kind == "expansion":
            return self._expansion(node, unit)
        if kind == "command_substitution":
            return self._substitution(node, unit)
        return None

    def _joined(self, parts: list[Node], unit: Unit) -> str | None:
        values = [self.value(part, unit) for part in parts]
        return None if None in values else "".join(values)  # type: ignore[arg-type]

    def _string(self, node: Node, unit: Unit) -> str | None:
        """A `"…"` string. The grammar leaves a newline and a lone `$` out of
        every child, so the literal text is read from the gaps between the
        expansions, as `_cts_bash_ast._string_removed` reads it."""
        raw = node.text or b""
        base = node.start_byte
        pieces: list[str] = []
        start = 1
        for child in node.named_children:
            if child.type == "string_content":
                continue
            pieces.append(_literal(raw[start : child.start_byte - base]))
            value = self.value(child, unit)
            if value is None:
                return None
            pieces.append(value)
            start = child.end_byte - base
        pieces.append(_literal(raw[start:-1]))
        return "".join(pieces)

    def _expansion(self, node: Node, unit: Unit) -> str | None:
        """`${X}`, `${BASH_SOURCE[0]}`, `${X%/*}` and `${X:-default}`."""
        inner = node.children[1:-1]
        if not inner:
            return None
        head = inner[0]
        if head.type == "subscript":
            index = head.child_by_field_name("index")
            name = head.child_by_field_name("name")
            if name is None or index is None or node_text(index) != "0":
                return None
            head_name = node_text(name)
        elif head.type == "variable_name":
            head_name = node_text(head)
        else:
            return None
        rest = inner[1:]
        if not rest:
            return self.variable(head_name, unit)
        operator = node_text(rest[0])
        if operator == "%" and len(rest) == 2 and node_text(rest[1]) == "/*":
            base = self.variable(head_name, unit)
            return posixpath.dirname(base) if base and "/" in base else None
        if operator in (":-", "-") and head_name not in self.assignments:
            return self._joined(rest[1:], unit) if len(rest) > 1 else ""
        return None

    def _substitution(self, node: Node, unit: Unit) -> str | None:
        """`$(dirname X)`, `$(cd X && pwd)` and `$(git rev-parse --show-toplevel)`."""
        body = list(node.named_children)
        if len(body) != 1:
            return None
        inner = body[0]
        if inner.type == "list":
            commands = list(inner.named_children)
            if len(commands) == 2 and command_words(commands[1]) == ["pwd"]:
                words = command_arguments(commands[0])
                if len(words) == 2 and node_text(words[0]) == "cd":
                    target = self.value(words[1], unit)
                    return os.path.normpath(target) if target else None
            return None
        if inner.type != "command":
            return None
        words = command_arguments(inner)
        texts = [node_text(w) for w in words]
        if texts == ["git", "rev-parse", "--show-toplevel"]:
            return str(self.repo_root)
        if len(words) == 2 and texts[0] == "dirname":
            target = self.value(words[1], unit)
            return posixpath.dirname(target) if target and "/" in target else None
        return None

    def words(self, command: Node, unit: Unit) -> list[str | None]:
        """COMMAND's name and argument values; None where only the run knows."""
        return [
            self.value(child, unit)
            for child in command.children
            if child.type == "command_name" or child.type in ARGUMENT_TYPES
        ]

    def function_commands(self, name: str) -> list[tuple[Node, Unit]]:
        """The commands NAME's bodies run directly, nested definitions excluded."""
        if name not in self._bodies:
            self._bodies[name] = [
                (command, unit)
                for body, unit in self.functions.get(name, [])
                for command in _executed(body)
            ]
        return self._bodies[name]

    def assigned_in(self, name: str) -> set[Path]:
        """The followed files that assign NAME."""
        return {u.path for _, u in self.assignments.get(name, []) if u.path}


class Reach(NamedTuple):
    """Every namespace a step runs, and every Python file it hands an
    interpreter."""

    namespaces: tuple[Namespace, ...]
    python: tuple[Path, ...]


def _python_target(words: list[str | None]) -> str | None:
    """The `.py` file, or `-m` module as a path, that WORDS hand to Python."""
    found = interpreter_scripts(words)
    if found:
        return found[0]
    for index, word in enumerate(words):
        if word is None:
            return None
        name = word.rsplit("/", 1)[-1]
        if name == "uv" and words[index + 1 : index + 2] == ["run"]:
            return _uv_run_target(words[index + 2 :])
        if re.fullmatch(r"python[0-9.]*", name) and words[index + 1 : index + 2] == [
            "-m"
        ]:
            module = words[index + 2] if len(words) > index + 2 else None
            return module.replace(".", "/") + ".py" if module else None
    return None


def _uv_run_target(rest: list[str | None]) -> str | None:
    """The `.py` file `uv run` executes: its program word when that is a `.py`
    file, or the script a Python interpreter in that place is handed. A `.py`
    word after any other program (`uv run pytest t.py`) is that program's
    argument, not a script the step runs."""
    for index, word in enumerate(rest):
        if word is None:
            return None
        if word.startswith("-"):
            continue
        if word.endswith(".py"):
            return word
        if re.fullmatch(r"python[0-9.]*", word.rsplit("/", 1)[-1]):
            return _python_target(rest[index:])
        return None
    return None


def _shell_target(words: list[str | None]) -> str | None:
    """The shell script WORDS run: the command word itself (`./x.sh`), or the
    first script a shell is handed (`bash -e x.sh`). A `-c` hands the shell a
    string, so nothing after it is a file."""
    if not words or words[0] is None:
        return None
    if _SHELL_SUFFIX.search(words[0]):
        return words[0]
    if not _SHELL_INTERPRETER.fullmatch(words[0].rsplit("/", 1)[-1]):
        return None
    for word in words[1:]:
        if word is None or word == "-c":
            return None
        if not word.startswith("-"):
            return word
    return None


@functools.lru_cache(maxsize=1024)
def _file_namespace(path: Path, repo_root: Path, action_dir: str | None) -> Namespace:
    """The namespace a followed script starts, built once per run of the lint:
    a shared script is reached from many jobs."""
    return Namespace(repo_root, _unit(path, path), action_dir)


@functools.lru_cache(maxsize=256)
def reach(script: str, repo_root: Path, action_dir: str | None) -> Reach:
    """Every namespace and Python file the `run:` body SCRIPT reaches.

    ACTION_DIR is the composite action the step comes from, so its
    `$GITHUB_ACTION_PATH` and `${{ github.action_path }}` resolve."""
    if action_dir:
        script = _ACTION_PATH_EXPR.sub(lambda _: str(repo_root / action_dir), script)
    entry = Unit(None, script, parse(script), None)
    namespaces: list[Namespace] = []
    python: list[Path] = []
    queue: list[tuple[Unit, int]] = [(entry, 0)]
    seen: set[Path] = set()
    while queue:
        unit, hops = queue.pop(0)
        namespace = (
            Namespace(repo_root, unit, action_dir)
            if unit.path is None
            else _file_namespace(unit.path, repo_root, action_dir)
        )
        namespaces.append(namespace)
        if hops >= MAX_HOPS:
            continue
        for command, owner in namespace.executed:
            words = namespace.words(command, owner)
            py = namespace.file(_python_target(words))
            if py is not None and py.suffix == ".py" and py not in python:
                python.append(py)
                continue
            target = namespace.file(_shell_target(words))
            if target is not None and target not in seen:
                seen.add(target)
                queue.append((_unit(target, target), hops + 1))
    return Reach(tuple(namespaces), tuple(python))
