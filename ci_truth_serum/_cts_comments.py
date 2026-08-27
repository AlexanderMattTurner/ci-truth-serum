"""Where the comments are — answered by each language's own grammar.

Four lints read NARRATION rather than code: `check_drift_guards`,
`check_graceful_handwave`, `check_historical_comments`, `check_workflow_refs`.
All of them need the same primitive — "which lines carry a comment, and what does
it say" — and that is a question about the grammar, not about the text. The text
answer is wrong in both directions, and the dangerous direction is silent. Each
of these is a delimiter scan's verdict on a real line from this repo's trees:

    const m = "see // not a comment";   read as a comment — it is a value
    run(); /* narration */              read as pure code — no ` // ` on the line
    hint = 'add # allow-history: <r>'   read as an opt-out, which SUPPRESSES,
                                        so the lint silently disarms itself

``comment_lines`` is the one entry point: hand it the text and the path, and it
picks the grammar the path names — ``tokenize`` for Python, tree-sitter-bash for
shell, tree-sitter-{javascript,typescript} for JS/TS. Judging the PROSE inside
the comment stays each lint's own regex; English has no grammar here to parse.

Line numbers are 1-based physical lines counted by ``\\n`` — the numbering every
grammar here reports, and what a caller must enumerate with (``str.splitlines``
also breaks on ``\\v``, ``\\f`` and U+2028, which no parser here treats as a line
break, so counting with it slides every later line number off by the difference).
"""

import io
import sys
import tokenize
from collections.abc import Iterable
from pathlib import Path

from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
# `iter_nodes` walks any tree-sitter tree, bash or not, so both grammars below
# share the one traversal rather than each growing a copy of it.
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    iter_nodes,
    parse as _parse_bash,
)
from _cts_js_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    is_js_source,
    parse as _parse_js,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    comment_body,
    is_python_source,
    is_shell_source,
)


def _merge(fragments: Iterable[tuple[int, str]]) -> dict[int, str]:
    """1-based line -> the comment text on it, joining the fragments of a line
    that carries more than one comment (``/*a*/ x /*b*/``) with a newline, so a
    phrase is matched within one comment rather than across two."""
    merged: dict[int, str] = {}
    for line, text in fragments:
        merged[line] = f"{merged[line]}\n{text}" if line in merged else text
    return merged


def _node_fragments(node: Node) -> list[tuple[int, str]]:
    """(1-based line, text on that line) for a comment NODE, one entry per line
    it spans — a JS block comment is a single node covering many lines, and every
    caller here is line-oriented."""
    text = node.text.decode("utf-8", "replace")
    first = node.start_point[0] + 1
    return [
        (first + offset, fragment.rstrip("\r"))
        for offset, fragment in enumerate(text.split("\n"))
    ]


def python_comments(source: str) -> dict[int, str]:
    """1-based line -> comment text, for every comment in Python SOURCE.

    Read from Python's OWN tokenizer rather than scanned out of the text. "Is
    this `#` a comment or a character inside a string literal?" is a question
    about the grammar, and the text answer gets it wrong in the direction that
    matters here: a lint fixture or an error message containing `# canonical
    rows, mirrored from X` is a value the program builds, not an author claiming
    anything about the tree — yet it reads as a comment to any `find(" # ")`.
    That false positive is not hypothetical; it is what these checks' own test
    fixtures produce.

    Raises ``tokenize.TokenError`` / ``SyntaxError`` on source that does not
    tokenize; ``comment_lines`` decides what a caller does about that.
    """
    return {
        token.start[0]: token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }


def shell_comments(script: str) -> dict[int, str]:
    """1-based line -> comment text, for every bash ``comment`` node in SCRIPT.

    Mandatory for the reason `.claude/rules/shell-lint-parsing.md` gives: "is
    this a comment, or data a command prints?" is a question about the grammar.
    Measured on this package's own two probes, the text heuristic reads a whole
    HEREDOC BODY as comments — the documented false-positive class, since a
    heredoc is data no shell executes and nobody authored as a claim about the
    tree.

    ``PathologicalInputError`` from ``parse`` propagates: a lint that degraded to
    "no findings" on the one input an adversary controls would be exactly the
    false green this pack exists to catch.
    """
    return _merge(
        fragment
        for node in iter_nodes(_parse_bash(script), "comment")
        for fragment in _node_fragments(node)
    )


def js_comments(source: str, path: str) -> dict[int, str]:
    """1-based line -> comment text, for every JS/TS ``comment`` node in SOURCE.

    Both comment shapes come from the grammar, so a `//` inside a string or a
    template literal is not one, and every line of a `/* … */` block is — the two
    the text heuristic inverts. PATH picks the grammar (see ``_cts_js_ast``); it is
    never sniffed from the content, because a `.ts` file's type annotations parse
    as ERROR nodes under the JavaScript grammar and would drop the comments
    after them.
    """
    return _merge(
        fragment
        for node in iter_nodes(_parse_js(source, path), "comment")
        for fragment in _node_fragments(node)
    )


def text_comments(text: str) -> dict[int, str]:
    """1-based line -> comment text, scanned out of TEXT by delimiter.

    Correct only where no grammar can answer: YAML, whose parsers discard
    comments outright, plus whatever else a comment-reading lint is pointed at.
    Everything above replaces this for the language it owns, so reaching for it
    is a statement that the language has no parser here — never a default.
    """
    return {
        lineno: body
        for lineno, line in enumerate(text.split("\n"), 1)
        if (body := comment_body(line)) is not None
    }


def comment_lines(text: str, path: str) -> dict[int, str]:
    """1-based line -> the comment text on that line, using the grammar PATH's
    language provides and falling back to ``text_comments`` when it has none.

    A Python file that does not tokenize (a syntax error, a half-written edit)
    falls back too rather than reporting no comments at all: "no findings" on an
    unparseable file is the false green this pack exists to catch, and other
    tooling owns the syntax error itself.
    """
    if is_python_source(path):
        try:
            return python_comments(text)
        except (tokenize.TokenError, SyntaxError):
            return text_comments(text)
    if is_shell_source(path, text.split("\n", 1)[0]):
        return shell_comments(text)
    if is_js_source(path):
        return js_comments(text, path)
    return text_comments(text)
