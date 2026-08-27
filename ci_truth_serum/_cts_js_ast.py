"""Shared tree-sitter parsing for the JavaScript/TypeScript-reading lints.

The comment-reading lints (`check_drift_guards`, `check_graceful_handwave`,
`check_historical_comments`, `check_workflow_refs`) all ask one question of a
`.mjs` / `.ts` suite: is this `//` a comment, or characters inside something the
program builds? A text scan answers it wrong in both directions — `const u =
"https://x/y"` reads as a comment, a `/* … */` block or a `//` after a `)` on the
same line does not — and the dangerous direction is silent, because an opt-out
token spelled inside a string literal disarms the lint that reads it. This module
hands those lints a real ECMAScript/TypeScript grammar instead.

Fails LOUD when the bindings are absent: a lint that silently degraded to "no
findings" on a missing dependency would be exactly the false green this pack
exists to catch, so the ImportError propagates rather than being swallowed. The
bindings are pinned as a hook runtime dependency (pyproject `dependencies`, and
each reading hook's `additional_dependencies`).

Neither pathological-input guard `_cts_bash_ast` carries applies here, measured on
all three grammars (tree-sitter-javascript 0.25, tree-sitter-typescript 0.23)
rather than assumed: 20k-deep parenthesis nesting and 20k-stage `|` / `+` / `&&`
chains all parse inside the baseline RSS (tree-sitter-bash needs ~3.3 GB at 20k
pipeline stages), and a supplementary-plane codepoint lexes normally in source,
in a string, in a template literal and in a comment (tree-sitter-bash's external
C scanner corrupts the heap on one). Reproduce with `resource.ru_maxrss` around
`parse` on each shape.
"""

from functools import lru_cache

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

# Suffix -> the grammar that accepts a superset of the others' syntax for it.
# `.jsx` is plain JavaScript here (tree-sitter-javascript's grammar includes
# JSX); TypeScript needs its own, and `.tsx` a third, because TS resolves `<T>`
# as a type assertion where TSX resolves it as an element.
_JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx"})
_TS_SUFFIXES = frozenset({".ts", ".mts", ".cts"})
_TSX_SUFFIXES = frozenset({".tsx"})

_GRAMMARS = {
    **dict.fromkeys(_JS_SUFFIXES, tree_sitter_javascript.language),
    **dict.fromkeys(_TS_SUFFIXES, tree_sitter_typescript.language_typescript),
    **dict.fromkeys(_TSX_SUFFIXES, tree_sitter_typescript.language_tsx),
}


# Building a Language and its Parser is the expensive part; reuse both across
# every parse in a run, keyed by grammar rather than by path. `lru_cache` rather
# than a module-level dict a function writes: the cache carries its own
# `cache_clear`, so a caller that must not inherit a previous run's parser can
# drop it.
@lru_cache(maxsize=len(_GRAMMARS))
def _parser_for(grammar) -> Parser:
    return Parser(Language(grammar()))


def _suffix(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name[name.rindex(".") :].lower() if "." in name else ""


def is_js_source(path: str) -> bool:
    """True when PATH names JavaScript or TypeScript — the file classes this
    module has a grammar for."""
    return _suffix(path) in _GRAMMARS


def _parser(path: str) -> Parser:
    """The parser for PATH's grammar. A non-JS/TS path is a caller bug, so it
    raises rather than picking a default that would silently parse TypeScript
    as JavaScript (and read every type annotation as an ERROR node)."""
    suffix = _suffix(path)
    if suffix not in _GRAMMARS:
        raise ValueError(f"{path!r} is not a JavaScript/TypeScript path")
    return _parser_for(_GRAMMARS[suffix])


def parse(source: str, path: str) -> Node:
    """The root node of SOURCE parsed with the grammar PATH's suffix names.

    tree-sitter NEVER raises on malformed input — a syntax error surfaces as
    ``ERROR`` nodes in the tree, so a half-written file yields a partial tree
    instead of crashing a pre-commit hook on an unrelated commit."""
    return _parser(path).parse(source.encode("utf-8")).root_node
