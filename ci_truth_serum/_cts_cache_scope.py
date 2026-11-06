"""What one GitHub Actions cache step can hold: its key, the files its
`hashFiles()` calls read, the paths it saves, and the package managers whose
store a setup action caches.

The module reads each `${{ }}` in a key or path with `_cts_gha_expr`. It
resolves the values the workflow text fixes: a composite action's `inputs.*`,
`format()`, `hashFiles()`, a cache step's `cache-primary-key`, a step output
that a `run:` script writes as `NAME=VALUE`, and `env.*` in a path. A context
that can hold a pin (`env`, `vars`, `matrix`, `inputs`) stays as written, so
a caller can find the pin's name in the key. A key with a value the module
cannot read gives `CacheScope(None)`: that value could name any pin. A part of
a path the module cannot read is `**`, any directory.

`check_uncached_download` asks this module which caches a job has, and then
decides which download each one holds.
"""

import posixpath
import re
import sys
from pathlib import Path
from typing import NamedTuple

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    command_arguments,
    iter_nodes,
    node_text,
    parse as parse_shell,
    program_name,
    unquote,
)
from _cts_gha_expr import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Call,
    Expression,
    Literal,
    Unsupported,
    format_string,
    parse as parse_expression,
)
from _cts_step_reach import tokens  # noqa: E402,I001  # pylint: disable=wrong-import-position

# An action that caches. `actions/cache` and its `restore`/`save` sub-actions
# cache whatever their `path` names. A setup action caches one package manager's
# own store, and only when its cache input is on.
_CACHE_ACTIONS = frozenset({"actions/cache", "buildjet/cache"})


class _SetupCache(NamedTuple):
    """How one setup action caches. INPUT turns the cache on, and DEFAULT is
    its value when the step passes none. INSTALLER is the package manager
    whose downloads the store holds, or None when the input's VALUE names it
    (`cache: pnpm`)."""

    input: str | None
    installer: str | None
    default: str


_SETUP_CACHES = {
    "actions/setup-node": _SetupCache("cache", None, ""),
    "actions/setup-python": _SetupCache("cache", None, ""),
    "actions/setup-java": _SetupCache("cache", None, ""),
    "actions/setup-go": _SetupCache("cache", "go", "true"),
    "actions/setup-dotnet": _SetupCache("cache", "dotnet", ""),
    "astral-sh/setup-uv": _SetupCache("enable-cache", "uv", "auto"),
    "ruby/setup-ruby": _SetupCache("bundler-cache", "bundle", ""),
    "Swatinem/rust-cache": _SetupCache(None, "cargo", "true"),
}
# A setup action that prunes its store before it saves, unless this input is
# "false". setup-uv runs `uv cache prune --ci`, which deletes every wheel the
# installs downloaded and keeps only the ones uv built.
_PRUNING_SETUPS = {"astral-sh/setup-uv": "prune-cache"}
# The installer a setup cache serves when its input is an expression.
ANY_INSTALLER = "*"

# One `${{ }}` in workflow text, with its body.
_EXPRESSION = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)
# The contexts a cache key keeps as written when the text does not fix their
# value. `env`, `vars`, `matrix` and a workflow's own `inputs` can hold a pin,
# and a caller finds that pin by the variable it reads. The runner, the
# event, the strategy and the job hold none.
_KEPT_CONTEXTS = frozenset(
    {"env", "vars", "matrix", "inputs", "runner", "github", "strategy", "job"}
)
# The outputs of a cache step that report how its restore went.
_CACHE_STATUS_OUTPUTS = frozenset({"cache-hit", "cache-matched-key"})
# The bash nodes of an argument that are literal text.
_LITERAL_PIECES = frozenset({"word", "string_content", "raw_string"})


def action_name(step: dict) -> str:
    """The action STEP uses, without its `@ref` (`actions/cache@0057852…`)."""
    uses = step.get("uses")
    return uses.split("@", 1)[0].strip() if isinstance(uses, str) else ""


def _is_cache_action(step: dict) -> bool:
    """True when STEP uses a cache action or its `restore` or `save` part."""
    name = action_name(step)
    return name in _CACHE_ACTIONS or name.rpartition("/")[0] in _CACHE_ACTIONS


class Text(NamedTuple):
    """Workflow text once this module has read its expressions. TEXT holds each
    value the text fixes, and the spelling of each value computed at run time
    (`${{ env.UV_VERSION }}`). HASHED are the patterns its `hashFiles()` calls
    read. LITERAL is True when no part of it is computed at run time."""

    text: str
    hashed: tuple[str, ...] = ()
    literal: bool = True


_ANY_DIRECTORY = Text("**", literal=False)


def _joined(pieces: list[Text]) -> Text:
    return Text(
        "".join(piece.text for piece in pieces),
        tuple(pattern for piece in pieces for pattern in piece.hashed),
        all(piece.literal for piece in pieces),
    )


def _computed(path: tuple[str, ...]) -> Text:
    return Text("${{ " + ".".join(path) + " }}", literal=False)


class Context(NamedTuple):
    """What an expression in one list of steps can read. STEPS are the steps of
    its job or composite action, for `steps.<id>.outputs`. INPUTS are a
    composite action's inputs as its caller passes them, or None in a
    workflow, where `inputs.*` is the run's own and stays as written. ENV is
    the `env:` the steps inherit."""

    steps: tuple[dict, ...]
    inputs: dict[str, "Text | None"] | None
    env: dict


def env_of(container: object) -> dict:
    env = container.get("env") if isinstance(container, dict) else None
    return (
        {k: v for k, v in env.items() if k != "__line__"}
        if isinstance(env, dict)
        else {}
    )


def _render(
    template: object, step: dict, context: Context, *, path: bool = False
) -> list[Text] | None:
    """TEMPLATE in pieces, with each `${{ }}` read in STEP's CONTEXT. None when
    an expression holds a value this module cannot read. PATH reads `env.*` from
    the `env:` blocks, because a path needs the value and a key needs only the
    name, and it reads an expression it cannot read as `**`, any directory."""
    if isinstance(template, bool):
        template = "true" if template else "false"
    if not isinstance(template, (str, int, float)):
        return None
    text, pieces, last = str(template), [], 0
    for match in _EXPRESSION.finditer(text):
        pieces.append(Text(text[last : match.start()]))
        last = match.end()
        try:
            value = _evaluate(parse_expression(match["body"]), step, context, path)
        except Unsupported:
            value = None
        if value is None and not path:
            return None
        pieces.append(_ANY_DIRECTORY if value is None else value)
    pieces.append(Text(text[last:]))
    return pieces


def rendered(template: object, step: dict, context: Context) -> Text | None:
    pieces = _render(template, step, context)
    return None if pieces is None else _joined(pieces)


def _evaluate(
    tree: Expression, step: dict, context: Context, path: bool
) -> Text | None:
    """The value of the expression TREE in STEP's CONTEXT, or None."""
    if isinstance(tree, Literal):
        value = tree.value
        if isinstance(value, bool):
            return Text("true" if value else "false")
        if isinstance(value, float) and value.is_integer():
            return Text(str(int(value)))
        return Text("" if value is None else str(value))
    if isinstance(tree, Call):
        args = [_evaluate(arg, step, context, path) for arg in tree.args]
        if any(arg is None or not arg.literal for arg in args):
            return None
        texts = [arg.text for arg in args if arg is not None]
        if tree.name.lower() == "hashfiles":
            quoted = ", ".join("'" + text.replace("'", "''") + "'" for text in texts)
            return Text(f"${{{{ hashFiles({quoted}) }}}}", tuple(texts), False)
        if tree.name.lower() == "format" and texts:
            return Text(format_string(texts[0], texts[1:]))
        return None
    head = tree.path[0].lower()
    if head == "inputs" and context.inputs is not None and len(tree.path) == 2:
        return context.inputs.get(tree.path[1], Text(""))
    if head == "steps" and len(tree.path) == 4 and tree.path[2] == "outputs":
        return _step_output(tree.path[1], tree.path[3], step, context)
    if head == "env" and path and len(tree.path) == 2:
        value = {**context.env, **env_of(step)}.get(tree.path[1])
        # The value renders without PATH, so an `env.*` inside it is `**`
        # and an `env:` entry that names itself ends.
        pieces = _render(value, step, context) if value is not None else None
        if pieces is not None:
            return Text(_path_text(pieces))
    return _computed(tree.path) if head in _KEPT_CONTEXTS else None


def _step_output(
    step_id: str, name: str, consumer: dict, context: Context
) -> Text | None:
    """`steps.<STEP_ID>.outputs.<NAME>`, read in CONSUMER's CONTEXT. Only a
    step that runs before CONSUMER can set it, which also ends any cycle. A
    cache step's primary key is its own key, and its other outputs report how
    the restore went. A `run:` step's output is what its script writes."""
    index = next((i for i, s in enumerate(context.steps) if s is consumer), 0)
    producer = next((s for s in context.steps[:index] if s.get("id") == step_id), None)
    if producer is None:
        return None
    if _is_cache_action(producer):
        if name == "cache-primary-key":
            with_block = producer.get("with")
            key = with_block.get("key") if isinstance(with_block, dict) else None
            return rendered(key, producer, context)
        return (
            _computed(("steps", step_id, "outputs", name))
            if (name in _CACHE_STATUS_OUTPUTS)
            else None
        )
    script = producer.get("run")
    return _written_output(script, name) if isinstance(script, str) else None


def _written_output(script: str, name: str) -> Text | None:
    """The value the `run:` SCRIPT gives output NAME: the argument `NAME=VALUE`
    of a command in it. That is how `echo "NAME=…" >> "$GITHUB_OUTPUT"` writes
    one, and how a helper such as `gha_output "NAME=…"` does. A variable in
    VALUE reads as `env.<variable>`, so a key that holds the output names the
    pin the step wrote. None when no such argument, or two different ones,
    write NAME, or when VALUE holds a command substitution or is a `printf`
    format."""
    values = set()
    for command in iter_nodes(parse_shell(script), "command"):
        for argument in command_arguments(command):
            pieces = _pieces(argument)
            head = pieces[0] if pieces else None
            if head is None or head.type not in _LITERAL_PIECES:
                continue
            first = _literal_piece(head)
            if not first.startswith(f"{name}="):
                continue
            if program_name(tokens(command)[0]) == "printf":
                values.add(None)  # the value is a format, not the text
                continue
            rest = [Text(first[len(name) + 1 :])]
            rest += [_shell_piece(piece) for piece in pieces[1:]]
            values.add(None if None in rest else _joined(rest))
    return values.pop() if len(values) == 1 else None


def _pieces(node: Node) -> list[Node]:
    """NODE's parts in order, with quotes and concatenation opened."""
    if node.type in ("string", "concatenation"):
        return [piece for child in node.named_children for piece in _pieces(child)]
    return [node]


def _literal_piece(node: Node) -> str:
    return (
        node_text(node) if node.type == "string_content" else unquote(node_text(node))
    )


def _shell_piece(node: Node) -> Text | None:
    """One part of a shell word as workflow text: literal text, or a variable
    spelled as the `env.*` it reads. None for anything computed."""
    if node.type in _LITERAL_PIECES:
        return Text(_literal_piece(node))
    names = (
        list(iter_nodes(node, "variable_name"))
        if node.type in ("simple_expansion", "expansion")
        else []
    )
    return _computed(("env", node_text(names[0]))) if names else None


def _setup_installers(step: dict, context: "Context") -> frozenset[str] | None:
    """The installers whose store STEP's setup action caches, or None when it
    is no setup action or its cache input is off. An input this module cannot
    read may turn the cache on for any installer."""
    action = action_name(step)
    row = _SETUP_CACHES.get(action)
    if row is None:
        return None
    name, installer, default = row
    with_block = step.get("with")
    with_block = with_block if isinstance(with_block, dict) else {}
    text = _setup_input(with_block, name, default, step, context)
    if text is not None and text.literal and text.text.strip() in ("", "false"):
        return None
    if action in _PRUNING_SETUPS:
        prune = _setup_input(with_block, _PRUNING_SETUPS[action], "true", step, context)
        if prune is not None and prune.literal and prune.text.strip() != "false":
            return None
    if text is None or not text.literal:
        return frozenset({ANY_INSTALLER})
    return frozenset({installer or text.text.strip()})


def _setup_input(
    with_block: dict, name: str | None, default: str, step: dict, context: "Context"
) -> Text | None:
    """The setup input NAME as STEP passes it, or DEFAULT when STEP passes none.
    A key with no value passes the empty string."""
    if name is None:
        return Text(default)
    value = with_block.get(name, default)
    return rendered("" if value is None else value, step, context)


class CacheScope(NamedTuple):
    """What one cache step can hold. KEY is its key with each expression the
    lint could read resolved, HASHED the patterns its `hashFiles()` calls read,
    PATHS the paths it saves as far as the text fixes them, and INSTALLERS the
    package managers whose store a setup action caches. KEY is None when the
    key holds a value this module cannot read: that value could name any pin, so
    the cache covers every fetch in the job."""

    key: str | None
    hashed: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    installers: frozenset[str] = frozenset()


def cache_scope(step: dict, context: Context) -> CacheScope | None:
    """What the cache STEP can hold, read in its CONTEXT, or None when STEP
    restores and saves no cache."""
    installers = _setup_installers(step, context)
    if installers is not None:
        return CacheScope("", installers=installers)
    if not _is_cache_action(step):
        return None
    with_block = step.get("with")
    if not isinstance(with_block, dict):
        return CacheScope(None)
    key = rendered(with_block.get("key"), step, context)
    if key is None:
        return CacheScope(None)
    return CacheScope(
        key.text, key.hashed, _cache_paths(with_block.get("path"), step, context)
    )


def _cache_paths(path: object, step: dict, context: Context) -> tuple[str, ...]:
    """The paths a cache's PATH saves, with each value the text does not fix
    read as `**`. A line the text fixes no part of is left out. It could be
    any directory, and reading it as every directory lets one computed store
    path (`${{ steps.store.outputs.dir }}`) cover every download in the job.
    Such a cache still covers a fetch through its key."""
    paths = []
    for line in path.splitlines() if isinstance(path, str) else []:
        line = line.strip()
        pieces = _render(line, step, context, path=True)
        if not line or line.startswith("!") or pieces is None:
            continue  # a `!` line excludes, and pieces is None for no string
        text = _path_text(pieces)
        if text.replace("**", "").strip("/"):
            paths.append(posixpath.normpath(home(text)))
    return tuple(paths)


def _path_text(pieces: list[Text]) -> str:
    """PIECES as a path glob: each part computed at run time is `**`."""
    return "".join(piece.text if piece.literal else "**" for piece in pieces)


def home(path: str) -> str:
    for spelling in ("${HOME}", "$HOME"):
        if path.startswith(spelling):
            return "~" + path[len(spelling) :]
    return path
