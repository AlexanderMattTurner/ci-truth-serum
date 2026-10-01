#!/usr/bin/env python3
"""Ban the sibling-temp rename `mv "$f.tmp" "$f"` onto a path that may be a link.

`rename(2)` replaces a destination symlink. It does not follow it. So the temp
file becomes the destination, and the link to the real file is gone. The write
succeeds and nothing warns. Every later read gets the new file, and the real file
never changes. A dotfile manager and a persisted volume both keep config files as
symlinks, so an atomic write of `~/.npmrc` this way detaches it.

The banned shape is an `mv` whose source is its destination plus one dotted
suffix, where the destination expands at run time (`$f`, `${f}`, `$(cmd)`, `~`).
A literal destination is a path the script names itself, so it passes.

The fix resolves the link first and renames onto its target. Walk the chain with
plain `readlink`, because stock macOS `readlink` has no `-f` flag.

The decision reads the bash grammar (``_cts_bash_ast``), so quoting decides what
expands, and a `mv` inside a message or a heredoc body is not a command. Opt out
with `# allow-rename-onto-symlink: <reason>` on a line the command spans, or in the
comment block directly above it. The reason is REQUIRED.
"""

import re
import sys
from pathlib import Path

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ARGUMENT_TYPES,
    PathologicalInputError,
    is_lookup,
    iter_nodes,
    node_text,
    parse,
    program_name,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MESSAGE_PREFIX,
    annotated_near,
    run_file_cli,
    run_line_checks,
)

OPT_OUT = "allow-rename-onto-symlink"

MESSAGE = (
    "this renames a sibling temp file onto its own destination. A rename "
    "REPLACES a destination symlink instead of following it, so a config file "
    "kept as a link is silently detached. Resolve the link first and rename onto "
    "its target, with a plain `readlink` walk (stock macOS `readlink` has no -f "
    f"flag). Or annotate `# {OPT_OUT}: <reason>`."
)

# A bare `$(cmd)` is a whole operand too. Leaving it out would shift which two
# words are read as the source and the destination.
_OPERAND_TYPES = ARGUMENT_TYPES | {"command_substitution"}

# The node types whose value only the shell knows. A `raw_string` holds none of
# them, so `'$f'` names a file whose name holds a dollar sign.
_EXPANSIONS = ("simple_expansion", "expansion", "command_substitution")

# One dotted suffix with no slash: `.tmp`, `.part`, `.seed-tmp`. A slash would put
# the source in a different directory, which is not this idiom.
_SUFFIX = re.compile(r"\.[^./]+$")


def _words(command: Node) -> list[Node]:
    """COMMAND's name and argument nodes, in order, redirects left out."""
    words: list[Node] = []
    for child in command.children:
        if child.type == "command_name":
            words.extend(child.children)
        elif child.type in _OPERAND_TYPES:
            words.append(child)
    return words


def _spelling(node: Node) -> str:
    """NODE's text with double quotes removed, so `"$f".tmp` equals `$f.tmp`.

    Single quotes stay, because `'$f'` and `$f` name different files.
    """
    if node.type == "string":
        return node_text(node)[1:-1]
    if node.type == "concatenation":
        return "".join(_spelling(child) for child in node.children)
    return node_text(node)


def _expands(node: Node) -> bool:
    """True when the shell decides NODE's value at run time.

    The grammar reads a tilde as plain text, so an unquoted leading `~` is read
    off the first word here. Inside quotes it names a directory called `~`.
    """
    head = node.children[0] if node.type == "concatenation" else node
    if head.type == "word" and node_text(head).startswith("~"):
        return True
    return any(True for _ in iter_nodes(node, *_EXPANSIONS))


def _operands(words: list[Node]) -> list[Node]:
    """The non-option words in WORDS. A word after `--` is never an option."""
    operands: list[Node] = []
    options_end = False
    for word in words:
        text = node_text(word)
        if not options_end and text == "--":
            options_end = True
        elif options_end or not text.startswith("-"):
            operands.append(word)
    return operands


def _renames_onto_its_stem(command: Node) -> bool:
    """True when COMMAND runs `mv <dest><suffix> <dest>` and `<dest>` expands.

    `mv` counts anywhere in the words, so a wrapper (`sudo mv`) cannot hide it.
    A lookup (`command -v mv`) and a printed message (`echo mv …`) run no `mv`.
    """
    words = _words(command)
    texts = [node_text(word) for word in words]
    if not texts or is_lookup(texts) or MESSAGE_PREFIX.match(program_name(texts[0])):
        return False
    names = [program_name(text) for text in texts]
    if "mv" not in names:
        return False
    operands = _operands(words[names.index("mv") + 1 :])
    if len(operands) < 2 or not _expands(operands[-1]):
        return False
    source, destination = _spelling(operands[-2]), _spelling(operands[-1])
    return source != destination and _SUFFIX.sub("", source) == destination


def violations(text: str, root: Node | None = None) -> list[int]:
    """1-based line numbers in TEXT that rename a sibling temp onto its stem.

    A line is reported once, however many such renames it holds.
    """
    root = parse(text) if root is None else root
    lines = text.split("\n")
    hits = {
        command.start_point[0] + 1
        for command in iter_nodes(root, "command")
        if _renames_onto_its_stem(command)
        and not annotated_near(
            lines,
            command.start_point[0] + 1,
            OPT_OUT,
            span_end=command.end_point[0] + 1,
        )
    }
    return sorted(hits)


def main(argv: list[str]) -> int:
    """Run the detector over ARGV one path at a time. A file the grammar refuses
    fails loudly (path named, exit 1), and every other path is still checked."""
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
