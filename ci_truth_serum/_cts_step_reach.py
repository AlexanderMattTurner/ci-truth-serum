"""The shell a CI step really runs: its own `run:` body and the repo files it reaches.

A lint that reads only the `run:` text misses everything a step does through a
repository script. `run: bash .github/scripts/scan.sh` is one command on the
page, and the download it causes can sit two files away. This module follows
those edges on the bash grammar (`_cts_bash_ast`) and hands a caller every
command that runs, each with the file it is written in.

A NAMESPACE is one shell process: an entry script plus every file it sources.
Functions and variables are shared across it. Three edges are followed:

  * `source X` and `. X` add X to the namespace;
  * a call to a function the namespace defines walks that function's body;
  * `bash X`, `sh X` and `./X.sh` start a child namespace, and a Python
    interpreter handed `X.py` records X for the caller.

`Namespace.value` lists the path forms that resolve. Any other path ends the
walk silently, because a guessed path judges a file the step never runs. The
depth is bounded and each file is read once per namespace, so a cycle ends.
"""

import functools
import os
import posixpath
import re
import sys
from collections import deque
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
    program_name,
    unquote,
)
from _cts_comments import shell_comments  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import MESSAGE_PREFIX  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_py_imports import _PY_INTERPRETER, interpreter_scripts  # noqa: E402,I001  # pylint: disable=wrong-import-position
from check_shell_source_declarations import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _DIRECTIVE,
    _comment_block_above,
    _directive_line,
    resolve_target,
)

# How many `bash X` / interpreter hops a walk follows from the `run:` body.
MAX_HOPS = 6

_SHELL_INTERPRETER = re.compile(r"(?:ba|da|z|k)?sh")
# A short-option cluster whose last letter takes the next word as its value
# (`bash -eo pipefail x.sh`, `bash -O extglob x.sh`).
_SHELL_VALUE_OPTION = re.compile(r"[-+][A-Za-z]*[oO]")
_SHELL_SUFFIX = re.compile(r"\.(?:sh|bash)$")
_SOURCE_COMMANDS = frozenset({"source", "."})
_ACTION_PATH_EXPR = re.compile(r"\$\{\{\s*github\.action_path\s*\}\}")
_WORKSPACE_EXPR = re.compile(r"\$\{\{\s*github\.workspace\s*\}\}")
# The `uv run` options that take the next word as their value.
_UV_VALUE_OPTIONS = frozenset(
    {
        "--with",
        "--with-editable",
        "--with-requirements",
        "--directory",
        "--project",
        "--python",
        "-p",
        "--group",
        "--only-group",
        "--no-group",
        "--extra",
        "--package",
        "--env-file",
        "--index",
        "--index-url",
        "--extra-index-url",
    }
)


# The commands that move the shell, and the nodes that run their own shell,
# whose `cd` moves nothing outside them: bash runs each pipeline member in a
# subshell.
_DIRECTORY_CHANGERS = frozenset({"cd", "pushd", "popd"})
_OWN_SHELL = frozenset(
    {"subshell", "command_substitution", "process_substitution", "pipeline"}
)
# The words that run the builtin they name: `command cd sub` moves the shell.
_BUILTIN_RUNNERS = frozenset({"command", "builtin"})


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


def _changes_directory(words: list[str]) -> bool:
    """True when WORDS run `cd`, `pushd` or `popd`, bare or through `command`
    or `builtin`."""
    while words and words[0] in _BUILTIN_RUNNERS:
        words = words[1:]
    return bool(words) and program_name(words[0]) in _DIRECTORY_CHANGERS


def body_start(node: Node) -> int:
    """The byte NODE's own text starts at. The grammar can fold the blanks
    before an expansion inside a string into the expansion's node."""
    raw = node.text or b""
    return node.start_byte + len(raw) - len(raw.lstrip())


@functools.lru_cache(maxsize=65536)
def _executed(node: Node) -> list[Node]:
    """The commands under NODE that run when NODE runs: a nested function
    definition runs nothing until something calls it."""
    return [c for c in iter_nodes(node, "command") if not _inside_definition(c, node)]


@functools.lru_cache(maxsize=1024)
def definitions(root: Node) -> tuple[list[tuple[str, Node]], list[tuple[str, Node]]]:
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


def callable_words(command: Node) -> list[str]:
    """The words of COMMAND that may name a function it runs: its name, and
    each argument (`retry f`, `trap f EXIT`). A printing command's arguments
    are text, so only its own name counts."""
    words = tokens(command)
    return words[:1] if words and MESSAGE_PREFIX.match(words[0]) else words


class Namespace:
    """One shell process: the units it reads, and every command it runs."""

    def __init__(
        self, repo_root: Path, entry: Unit, action_dir: str | None, cwd: Path | None
    ):
        self.repo_root = repo_root
        self.action_dir = action_dir
        self.cwd = cwd
        self.units: list[Unit] = []
        self.functions: dict[str, list[tuple[Node, Unit]]] = {}
        self.assignments: dict[str, list[tuple[Node, Unit]]] = {}
        self.executed: list[tuple[Node, Unit]] = []
        self._resolving: set[str] = set()
        self._variables: dict[str, str | None] = {}
        self._values: dict[int, str | None] = {}
        self._bodies: dict[str, list[tuple[Node, Unit]]] = {}
        self._changers: list[tuple[Node, Unit]] | None = None
        # The `source` command that read each sourced unit, by the unit's tree.
        self._anchors: dict[int, tuple[Node, Unit]] = {}
        self._directories: dict[int, Path | None] = {}
        self._add(entry)
        self._walk()

    # ── building ────────────────────────────────────────────────────────────

    def _add(self, unit: Unit) -> None:
        # A new file can add a second assignment, so a value read before it
        # was sourced may no longer be fixed.
        self._variables.clear()
        self._values.clear()
        self.units.append(unit)
        functions, assignments = definitions(unit.root)
        for name, body in functions:
            self.functions.setdefault(name, []).append((body, unit))
        for name, assignment in assignments:
            self.assignments.setdefault(name, []).append((assignment, unit))

    def _walk(self) -> None:
        """Walk to a fixed point. A function a later-sourced file defines may be
        called by a command walked before that file was read, so the words of
        every executed command stay in MENTIONED until a definition appears."""
        pending = deque([(self.units[0].root, self.units[0])])
        mentioned: set[str] = set()
        walked: set[str] = set()
        while pending:
            while pending:
                node, unit = pending.popleft()
                for command in _executed(node):
                    self.executed.append((command, unit))
                    mentioned.update(callable_words(command))
                    added = self._sourced(command, unit)
                    if added is not None:
                        pending.append((added.root, added))
            for name in sorted((mentioned & self.functions.keys()) - walked):
                walked.add(name)
                pending.extend(self.functions[name])

    def _sourced(self, command: Node, unit: Unit) -> Unit | None:
        words = tokens(command)
        if len(words) < 2 or words[0] not in _SOURCE_COMMANDS:
            return None
        target = self._source_target(command, unit)
        if target is None or any(u.path == target for u in self.units):
            return None
        added = _unit(target, unit.entry)
        self._add(added)
        self._anchors[id(added.root)] = (command, unit)
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
        """The repo file VALUE names from the step's working directory."""
        return self.file_in(value, self.cwd)

    def file_in(self, value: str | None, directory: Path | None) -> Path | None:
        """The repo file VALUE names, or None. A relative path resolves against
        DIRECTORY, and against nothing when that is None."""
        if not value or (directory is None and not value.startswith("/")):
            return None
        path = Path(os.path.normpath((directory or self.repo_root) / value))
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
        """NODE's value after expansion when the text fixes it, else None.

        The text fixes a literal, `${BASH_SOURCE[0]%/*}`, `$(dirname "$0")`,
        `$(cd X && pwd)`, `$(git rev-parse --show-toplevel)`,
        `$GITHUB_WORKSPACE`, `$GITHUB_ACTION_PATH`, a variable with one
        assignment of those forms, and the default of `${X:-default}` when no
        file assigns X."""
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
            pieces.append(_literal(raw[start : body_start(child) - base]))
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

    def directory_at(self, command: Node, unit: Unit) -> Path | None:
        """The directory COMMAND in UNIT runs in: the step's, or the target of
        the last `cd` before it when the text fixes that target. None when the
        text does not fix it: a `cd` to a computed path, a `pushd` or `popd`,
        or a `cd` in another file or a function body, whose order the text
        does not fix."""
        if command.id not in self._directories:
            self._directories[command.id] = self._directory_at(command, unit)
        return self._directories[command.id]

    def _directory_at(self, command: Node, unit: Unit) -> Path | None:
        if self._changers is None:
            self._changers = [
                (other, owner)
                for other, owner in self.executed
                if _changes_directory(tokens(other))
            ]
        last: tuple[Node, Unit] | None = None
        for other, owner in self._changers:
            scope = other.parent
            while scope is not None and scope.type not in _OWN_SHELL:
                scope = scope.parent
            same_file = owner.root is unit.root
            if scope is not None and not (
                same_file and scope.start_byte <= command.start_byte < scope.end_byte
            ):
                continue  # a subshell's `cd` moves only the subshell
            before = self._before((other, owner), (command, unit))
            if before is None:
                return None
            if before and (last is None or self._before(last, (other, owner))):
                last = (other, owner)
        if last is None:
            return self.cwd
        return self._cd_target(*last)

    def _before(
        self, first: tuple[Node, Unit], second: tuple[Node, Unit]
    ) -> bool | None:
        """True when the command FIRST runs before SECOND, compared in the
        deepest file both run from. None when the text does not fix the order:
        one of them runs from a function body."""
        chain_a, chain_b = self._chain(*first), self._chain(*second)
        if chain_a is None or chain_b is None:
            return None
        roots_b = {id(u.root): node for node, u in chain_b}
        for node_a, unit_a in chain_a:
            node_b = roots_b.get(id(unit_a.root))
            if node_b is not None:
                return node_a.start_byte < node_b.start_byte
        return None

    def _chain(self, node: Node, unit: Unit) -> list[tuple[Node, Unit]] | None:
        """NODE in UNIT, then the `source` command that read UNIT, and so on
        up to the file the namespace starts from. A sourced file's commands
        run where that `source` runs. None when a link sits in a function
        body, which runs where it is called."""
        chain = []
        while True:
            if _inside_definition(node, unit.root):
                return None
            chain.append((node, unit))
            anchor = self._anchors.get(id(unit.root))
            if anchor is None:
                return chain
            node, unit = anchor

    def _cd_target(self, command: Node, unit: Unit) -> Path | None:
        """Where the `cd` COMMAND goes, when the text fixes it."""
        arguments = command_arguments(command)
        while arguments and node_text(arguments[0]) in _BUILTIN_RUNNERS:
            arguments = arguments[1:]
        if len(arguments) != 2 or node_text(arguments[0]) != "cd":
            return None  # `pushd`, `popd`, a bare `cd`, or an option
        target = self.value(arguments[1], unit)
        if not target or target.startswith(("-", "~")):
            return None
        if target.startswith("/"):
            return Path(os.path.normpath(target))
        before = self.directory_at(command, unit)
        return Path(os.path.normpath(before / target)) if before else None

    def assigned_in(self, name: str) -> set[Path]:
        """The followed files that assign NAME."""
        return {u.path for _, u in self.assignments.get(name, []) if u.path}


class _Pending(NamedTuple):
    """A unit the walk still has to read: HOPS from the `run:` body, in the
    working directory CWD (None when the text does not fix it)."""

    unit: Unit
    hops: int
    cwd: Path | None


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
        if _PY_INTERPRETER.fullmatch(name) and words[index + 1 : index + 2] == ["-m"]:
            module = words[index + 2] if len(words) > index + 2 else None
            return module.replace(".", "/") + ".py" if module else None
    return None


def _uv_run_target(rest: list[str | None]) -> str | None:
    """The `.py` file `uv run` executes: its program word when that is a `.py`
    file, or the script a Python interpreter in that place is handed. A `.py`
    word after any other program (`uv run pytest t.py`) is that program's
    argument, not a script the step runs."""
    skip = False
    for index, word in enumerate(rest):
        if skip:
            skip = False
            continue
        if word is None:
            return None
        if word.startswith("-"):
            skip = word in _UV_VALUE_OPTIONS
            continue
        if word.endswith(".py"):
            return word
        if _PY_INTERPRETER.fullmatch(word.rsplit("/", 1)[-1]):
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
    rest = iter(words[1:])
    for word in rest:
        if word is None or word == "-c":
            return None
        if _SHELL_VALUE_OPTION.fullmatch(word):
            next(rest, None)
        elif not word.startswith(("-", "+")):
            return word
    return None


@functools.lru_cache(maxsize=1024)
def _file_namespace(
    path: Path, repo_root: Path, action_dir: str | None, cwd: Path | None
) -> Namespace:
    """The namespace a followed script starts, built once per run of the lint:
    a shared script is reached from many jobs."""
    return Namespace(repo_root, _unit(path, path), action_dir, cwd)


@functools.lru_cache(maxsize=256)
def reach(
    script: str,
    repo_root: Path,
    action_dir: str | None,
    working_directory: str | None = "",
) -> Reach:
    """Every namespace and Python file the `run:` body SCRIPT reaches.

    ACTION_DIR is the composite action the step comes from, so its
    `$GITHUB_ACTION_PATH` and `${{ github.action_path }}` resolve.
    WORKING_DIRECTORY is the step's `working-directory`, relative to the repo
    root; None means the text does not fix it, so a relative path resolves to
    nothing."""
    if action_dir:
        script = _ACTION_PATH_EXPR.sub(lambda _: str(repo_root / action_dir), script)
    script = _WORKSPACE_EXPR.sub(lambda _: str(repo_root), script)
    cwd = None if working_directory is None else repo_root / working_directory
    entry = Unit(None, script, parse(script), None)
    namespaces: list[Namespace] = []
    python: list[Path] = []
    queue = deque([_Pending(entry, 0, cwd)])
    seen: set[Path] = set()
    while queue:
        unit, hops, here = queue.popleft()
        namespace = (
            Namespace(repo_root, unit, action_dir, here)
            if unit.path is None
            else _file_namespace(unit.path, repo_root, action_dir, here)
        )
        namespaces.append(namespace)
        if hops >= MAX_HOPS:
            continue
        for command, owner in namespace.executed:
            words = namespace.words(command, owner)
            here = namespace.directory_at(command, owner)
            py = namespace.file_in(_python_target(words), here)
            if py is not None and py.suffix == ".py" and py not in python:
                python.append(py)
                continue
            target = namespace.file_in(_shell_target(words), here)
            if target is not None and target not in seen:
                seen.add(target)
                # A child runs in the directory its parent was in.
                queue.append(_Pending(_unit(target, target), hops + 1, here))
    return Reach(tuple(namespaces), tuple(python))
