"""Example-based tests (mutation oracle) for ci_truth_serum/_js_ast.py — the shared
tree-sitter wrapper the JS/TS-reading lints parse through.

Pins the exact contract of ``is_js_source`` and ``parse``: which suffixes have a
grammar, which grammar each gets, and that a malformed file still yields a tree.
The property/fuzz invariants live in ``test_fuzz_comments.py``.
"""

import pytest

from tests._helpers import load_hook

js_ast = load_hook("_js_ast.py", "check_js_ast")


@pytest.mark.parametrize(
    "path, expected",
    [
        ("a.js", True),
        ("a.mjs", True),
        ("a.cjs", True),
        ("a.jsx", True),
        ("a.ts", True),
        ("a.mts", True),
        ("a.cts", True),
        ("a.tsx", True),
        # The suffix is read off the basename, so a dotted name and a Windows
        # separator both still resolve.
        ("dir/sub/chars.test.mjs", True),
        (r"dir\sub\chars.test.mjs", True),
        ("A.MJS", True),
        ("a.py", False),
        ("a.sh", False),
        ("a.json", False),
        ("a.md", False),
        # No suffix at all — the `name.rindex(".")` path must not be taken.
        ("hooks/pre-commit", False),
        # A name that only LOOKS like it carries one of our suffixes.
        ("a.mjs.bak", False),
        ("mjs", False),
    ],
)
def test_is_js_source(path: str, expected: bool) -> None:
    assert js_ast.is_js_source(path) is expected


def test_parse_returns_the_program_root() -> None:
    root = js_ast.parse("const x = 1;", "a.js")
    assert root.type == "program"
    assert root.text.decode() == "const x = 1;"


def test_parse_is_reusable_across_calls_and_grammars() -> None:
    # The cached parsers must keep working on a second input, and the JS one must
    # not be handed a TypeScript file (or vice versa) by the cache key.
    assert js_ast.parse("const a = 1;", "a.js").type == "program"
    assert js_ast.parse("let b: string;", "b.ts").type == "program"
    assert js_ast.parse("const c = 2;", "c.mjs").type == "program"


@pytest.mark.parametrize(
    "path, source",
    [
        # A type annotation is a syntax error under the JavaScript grammar.
        ("a.ts", "function f(x: number): string { return String(x); }"),
        # A JSX element is a syntax error under the plain TypeScript grammar.
        ("a.tsx", "const el = <div className='x'>hi</div>;"),
        ("a.jsx", "const el = <div className='x'>hi</div>;"),
    ],
)
def test_each_suffix_gets_a_grammar_that_accepts_its_syntax(
    path: str, source: str
) -> None:
    """The reason the suffix picks the grammar rather than a single default: a
    file parsed by the wrong one is a tree of ERROR nodes, and every comment
    after the first error is lost."""
    assert not js_ast.parse(source, path).has_error


def test_parse_rejects_a_path_it_has_no_grammar_for() -> None:
    """Loud, not a default: silently parsing a `.py` file as JavaScript would
    report comments that are not there and miss the ones that are."""
    with pytest.raises(ValueError, match="not a JavaScript/TypeScript path"):
        js_ast.parse("x = 1", "a.py")


def test_parse_yields_a_tree_for_malformed_source() -> None:
    """tree-sitter never raises on bad input — a half-written file is a partial
    tree, so a hook cannot die on an unrelated commit."""
    root = js_ast.parse("function ( { const // ", "a.js")
    assert root.type == "program"
    assert root.has_error
