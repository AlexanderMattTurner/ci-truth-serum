#!/usr/bin/env python3
"""Ban a `repo: local` pre-commit hook whose id or command is defined twice.

Two people can add the same hook on two branches, and both copies survive the
merge. pre-commit accepts the result without a word, and both copies then run:

  - `pre-commit run <id>` runs every hook with that id, so the work runs twice.
  - `SKIP=<id>` skips every copy, so you cannot turn off one of them.
  - Each copy carries its own `args`, `files` and comments, and they drift.

A duplicate ID is always reported: the id is the name every consumer uses, so a
second hook with it has no honest reading. A duplicate COMMAND — the `entry` plus
its `args` — is reported too, but one script can run twice under different
settings on purpose. Mark that hook `# duplicate-hook-ok: <reason>` in its own
lines or in the comment block above it. A marked hook leaves its command group,
so a third, accidental copy is still reported.

Reads `.pre-commit-config.yaml` in the working directory, or `--config PATH`.
"""

import argparse
import shlex
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_fastyaml import compose  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    yaml_script_view,
)

OPT_OUT = "duplicate-hook-ok"
DEFAULT_CONFIG = ".pre-commit-config.yaml"


class Hook(NamedTuple):
    """One `repo: local` hook: its id, its command, and the lines it spans."""

    hook_id: str
    command: tuple[str, ...]
    start: int
    end: int


def _value(node: yaml.Node) -> object:
    """NODE as a plain Python value, read the way `yaml.safe_load` reads it."""
    return yaml.constructor.SafeConstructor().construct_object(node, deep=True)


def _field(node: yaml.Node, key: str, kind: type[yaml.Node]) -> yaml.Node:
    """The value node under KEY in the mapping NODE. It must be a KIND node.

    A config pre-commit itself would refuse raises here, so the check never
    reports a clean pass over a config it could not read.
    """
    if isinstance(node, yaml.MappingNode):
        for key_node, value in node.value:
            if key_node.value == key and isinstance(value, kind):
                return value
    raise ValueError(
        f"line {node.start_mark.line + 1}: expected `{key}:` holding a "
        f"{kind.__name__.removesuffix('Node').lower()}"
    )


def _end_line(node: yaml.MappingNode) -> int:
    """The 1-based last line the mapping NODE writes a value on.

    The mapping's own end mark sits on the NEXT hook's `-`, so it is not used:
    it would lend this hook the next hook's opt-out. The last value's mark ends
    the hook instead, and a block value ends at column 0 of the line after it.
    """
    mark = node.value[-1][1].end_mark if node.value else node.end_mark
    return mark.line if mark.column == 0 else mark.line + 1


def command_of(hook: dict) -> tuple[str, ...]:
    """The hook's argv: its `entry` split as a shell splits it, then its `args`.

    Two hooks that run one script under different args are a legitimate pair, so
    the args are part of the command, not just the script name. A tuple keeps
    each word whole: `entry: tool` with `args: ["a b"]` is one argument, and
    `entry: tool a` with `args: [b]` is two.
    """
    args = hook.get("args") or []
    if not isinstance(args, list):
        raise ValueError(f"hook {hook.get('id')!r}: `args:` is not a list")
    return (*shlex.split(str(hook.get("entry", ""))), *(str(arg) for arg in args))


def local_hooks(text: str) -> list[Hook]:
    """Every hook under a `repo: local` block in the config TEXT, in file order.

    The YAML node tree supplies each hook's lines, and the loader supplies its
    values, so a quoted `id: "x"` is the hook `x`.
    """
    root = compose(text)
    if root is None:
        return []
    hooks: list[Hook] = []
    for repo in _field(root, "repos", yaml.SequenceNode).value:
        if _field(repo, "repo", yaml.ScalarNode).value != "local":
            continue
        for node in _field(repo, "hooks", yaml.SequenceNode).value:
            hook_id = _value(_field(node, "id", yaml.ScalarNode))
            start = node.start_mark.line + 1
            hook = Hook(str(hook_id), command_of(_value(node)), start, _end_line(node))
            hooks.append(hook)
    return hooks


def duplicates(hooks: list[Hook], excused: set[int]) -> list[tuple[int, str]]:
    """(1-based line, message) for each id defined twice, and each command more
    than one unexcused hook runs. Each is reported on its second copy.

    EXCUSED holds positions in HOOKS. An excused hook leaves its command group
    but does not dissolve it, so a third copy is still reported.
    """
    counts = Counter(hook.hook_id for hook in hooks)
    out = [
        (
            [h.start for h in hooks if h.hook_id == hook_id][1],
            _id_message(hook_id, count),
        )
        for hook_id, count in counts.items()
        if count > 1
    ]
    by_command: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for position, hook in enumerate(hooks):
        by_command[hook.command].append(position)
    for command, positions in by_command.items():
        counted = [p for p in positions if p not in excused]
        if len(counted) > 1:
            ids = sorted(hooks[p].hook_id for p in positions)
            out.append((hooks[counted[1]].start, _command_message(command, ids)))
    return sorted(out)


def findings(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for every duplicate in the config TEXT."""
    hooks = local_hooks(text)
    lines, said = text.splitlines(), yaml_script_view(text)
    excused = {
        position
        for position, hook in enumerate(hooks)
        if annotated_near(lines, hook.start, OPT_OUT, span_end=hook.end, comments=said)
    }
    return duplicates(hooks, excused)


def _id_message(hook_id: str, count: int) -> str:
    """The finding for an id defined COUNT times."""
    return (
        f"local hook id `{hook_id}` is defined {count} times. `pre-commit run "
        f"{hook_id}` runs every copy and `SKIP={hook_id}` skips every copy. "
        "Delete one copy and keep its comments on the other. A duplicate id has "
        "no opt-out."
    )


def _command_message(command: tuple[str, ...], ids: list[str]) -> str:
    """The finding for a command that the hooks IDS all run."""
    return (
        f"local hooks {', '.join(f'`{i}`' for i in ids)} all run "
        f"`{shlex.join(command)}`. "
        "Delete one copy. If the second run is on purpose, annotate that hook "
        f"`# {OPT_OUT}: <reason>`."
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, metavar="PATH")
    args = parser.parse_args(argv)
    path = Path(args.config or DEFAULT_CONFIG)
    if args.config is None and not path.is_file():
        # A tree with no pre-commit config has no hook to duplicate. The note
        # tells this honest empty scan from a real pass; a PATH the caller
        # named and got wrong still raises on the read below.
        print(
            f"note: no {DEFAULT_CONFIG} in this directory — this check scanned nothing.",
            file=sys.stderr,
        )
        return 0
    hits = findings(path.read_text(encoding="utf-8"))
    for line, message in hits:
        print(f"{path}:{line}: {message}", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
