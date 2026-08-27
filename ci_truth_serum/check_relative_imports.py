#!/usr/bin/env python3
"""Ban a relative import specifier that names no file on disk.

PROBLEM CLASS — Node's ESM resolver does no extension guessing and no
directory-index fallback. ``import { run } from "./lib-hook-io"`` for a file
named ``lib-hook-io.mjs`` is ``ERR_MODULE_NOT_FOUND``, and ``import "./lib"``
for a ``lib/`` directory is ``ERR_UNSUPPORTED_DIR_IMPORT``. Neither is a syntax
error, so a linter and a type checker both pass it. The script dies the moment
a runner or a git hook starts it — the place nobody is watching for it.

One worked case. A hook script moved from ``.claude/hooks/`` into a nested
``lib/`` directory and kept its old ``import "../lib-io.mjs"``, one directory
short of the new depth. No test loads a hook script by running it, so the
suite stayed green; the hook then broke on the next real commit.

The rule. For each JavaScript/TypeScript file on argv, every STATIC relative
specifier is joined to the importing file's own directory, and the result must
name an existing FILE. Checked: ``import … from "./x"``, ``import "./x"``,
``export … from "./x"``, ``export * from "./x"``, and ``import("./x")`` when
the argument is a plain string or a template with no interpolation. Nothing is
ever loaded or executed — resolution is a filesystem stat, not a module load.

Blind spots, each deliberate:

  * A bare specifier (``node:fs``, a package name) resolves through
    ``node_modules`` and needs real package resolution. Not checked.
  * A ``#subpath`` specifier resolves through ``package.json`` imports. Not
    checked.
  * A dynamic ``import(name)`` whose argument is computed has no static
    target. Skipped rather than guessed at.
  * A specifier written with a string escape (``"./x\\u002ey"``) is skipped:
    decoding it here would re-implement the lexer this check exists to avoid.
  * A query or fragment (``./x.mjs?v=2``) is stripped before resolution,
    which is what Node does. Stat'ing the raw specifier would report a
    working import as broken.

The specifier comes from the ECMAScript/TypeScript grammar (``_cts_js_ast``),
never from a text scan: "is this string a module specifier?" is a question
about the tree, and a relative-looking path inside a comment, a message, or a
template literal is not an import.

Scope is the file list on argv. The consumer decides which files those are
with its own ``files:``/``exclude`` regex, so this check carries no directory
list of its own.

A specifier whose target a later build step writes opts out with a same-line
or preceding-line ``// allow-dangling-import: <reason>`` comment.

Invoked by pre-commit with the staged JavaScript/TypeScript files.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import iter_nodes  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_js_ast import is_js_source, parse  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import annotated_near, run_file_cli, run_source_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-dangling-import"

MESSAGE = (
    "relative import specifier resolves to no file; Node ESM guesses no "
    "extension and falls back to no directory index, so this is an "
    "ERR_MODULE_NOT_FOUND the moment the script runs — name the file, or "
    f"annotate `// {OPT_OUT}: <reason>`"
)

# The two declaration shapes that carry a `source` field: `import … from "x"` /
# `import "x"`, and `export … from "x"` / `export * from "x"`.
_FROM_STATEMENTS = frozenset({"import_statement", "export_statement"})


def _literal_text(node) -> str | None:
    """The specifier NODE spells, when a reader can resolve it without running
    the language's own string lexer.

    A ``string`` and a no-substitution ``template_string`` both qualify —
    TypeScript's own resolver reads the latter as a specifier too — but only
    while every child is a plain ``string_fragment``. An ``escape_sequence``
    or a ``template_substitution`` means the text is not what the runtime
    resolves, so this returns None and the caller skips the specifier rather
    than decoding or guessing at it.
    """
    if node.type not in ("string", "template_string"):
        return None
    parts = []
    for child in node.named_children:
        if child.type != "string_fragment":
            return None
        parts.append(child.text.decode("utf-8", "replace"))
    return "".join(parts)


def relative_specifiers(source: str, path: str) -> list[tuple[int, str]]:
    """Every static RELATIVE specifier in SOURCE, as (1-based line, specifier).

    Exported so a test drives the extraction on its own: this is the half
    that decides what counts as an import, and a miss here is a silent clean
    pass over the very files it should have flagged.
    """
    root = parse(source, path)
    found: list[tuple[int, str]] = []

    def record(node) -> None:
        text = _literal_text(node)
        if text is not None and text.startswith("."):
            found.append((node.start_point[0] + 1, text))

    for statement in iter_nodes(root, *_FROM_STATEMENTS):
        node = statement.child_by_field_name("source")
        if node is not None:
            record(node)

    for call in iter_nodes(root, "call_expression"):
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if function is None or function.type != "import" or arguments is None:
            continue
        args = [arg for arg in arguments.named_children if arg.type != "comment"]
        if args:
            record(args[0])

    return found


def _resolves(specifier: str, path: str) -> bool:
    """True when SPECIFIER, read from the file at PATH, names an existing FILE.

    The query and fragment come off first: Node lets a file specifier carry a
    cache-busting ``?v=2`` and resolves only the part before it, so stat'ing
    the raw specifier would report a working import as broken.
    """
    bare = specifier.split("?", 1)[0].split("#", 1)[0]
    target = os.path.normpath(os.path.join(os.path.dirname(path), bare))
    return Path(target).is_file()


def violations(text: str, path: str) -> list[int]:
    """1-based lines in TEXT whose relative specifier resolves to no file.

    A path with no JavaScript/TypeScript grammar has no specifiers to read.
    """
    if not is_js_source(path):
        return []
    physical = text.split("\n")
    hits = {
        lineno
        for lineno, specifier in relative_specifiers(text, path)
        if not _resolves(specifier, path)
    }
    return sorted(
        lineno
        for lineno in hits
        if lineno <= len(physical) and not annotated_near(physical, lineno, OPT_OUT)
    )


def main(argv: list[str]) -> int:
    return run_source_checks(argv, violations, MESSAGE)


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
