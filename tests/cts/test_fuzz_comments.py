"""Property/fuzz tests for ci_truth_serum/_comments and its ci_truth_serum/_js_ast
parser — the shared "which lines carry narration" layer under the four
comment-reading lints.

Those lints are fed whatever bytes are staged, in five languages, so the
invariants that must hold for ALL inputs are pinned here rather than only
transitively through each ``violations()``:

  * ``comment_lines`` never raises on any (text, path) pair, in any language;
  * every reported line number is a real 1-based line of the text;
  * the reported body is text that actually occurs on that line — a comment the
    grammar located, never a fragment the extractor invented;
  * the result is deterministic, and choosing the grammar by path never depends
    on the content.
"""

import string

from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

comments = load_hook("_comments.py", "fuzz_comments")
js_ast = load_hook("_js_ast.py", "fuzz_js_ast")

# Fragments that exercise the constructs a text scan gets wrong: delimiters
# opened inside strings, block comments split over lines, heredocs, and a
# trailing `#`/`//` with and without the surrounding space the heuristic needed.
_TOKENS = [
    "// line",
    "/* block */",
    "/**",
    " * jsdoc",
    " */",
    "# hash",
    "x = 1  # trailing",
    "run();  // trailing",
    'const m = "see // not a comment";',
    "const t = `a ${/* real */ 1} b`;",
    "cat <<'EOF'",
    "EOF",
    'echo "# printed"',
    "let x: number = 1;",
    "<div className='x'>hi</div>",
    "'",
    '"',
    "`",
    "\\",
    "/*",
    "*/",
]
# Escapes so no invisible byte hides here: RLO, ZWSP, ZWJ, BOM, combining accent,
# astral emoji, and the two Unicode line separators JS treats as terminators.
_WEIRD = "\u202e\u200b\u200d\ufeff\u0301\U0001f600\u2028\u2029"
_LINE = st.one_of(
    st.sampled_from(_TOKENS),
    st.text(alphabet=string.printable + _WEIRD, max_size=40),
)
# One path per branch of the dispatcher, including the two with no grammar.
_PATHS = st.sampled_from(
    [
        "a.py",
        "a.pyi",
        "a.sh",
        ".hooks/pre-commit",
        "a.js",
        "suite.test.mjs",
        "a.ts",
        "a.tsx",
        "a.yaml",
        "a.md",
        "no_suffix",
    ]
)


@st.composite
def _sources(draw) -> str:
    return draw(st.sampled_from(["\n", "\r\n", "\n\n", " "])).join(
        draw(st.lists(_LINE, max_size=16))
    )


@given(_sources(), _PATHS)
def test_comment_lines_never_crashes_and_reports_real_lines(
    source: str, path: str
) -> None:
    found = comments.comment_lines(source, path)
    lines = source.split("\n")
    for lineno, body in found.items():
        assert 1 <= lineno <= len(lines)
        # Every fragment on the line must occur on that line: an extractor that
        # slid a span (a block comment's rows, a multibyte column) would report
        # narration the author never wrote there.
        for fragment in body.split("\n"):
            assert fragment in lines[lineno - 1]


@given(_sources(), _PATHS)
def test_comment_lines_is_deterministic(source: str, path: str) -> None:
    assert comments.comment_lines(source, path) == comments.comment_lines(source, path)


@given(_sources())
def test_grammar_choice_is_a_function_of_the_path_alone(source: str) -> None:
    """A `.ts` file full of shell, or a `.sh` file full of JSX, must still be
    parsed by the grammar its path names — a content sniff would make a lint's
    verdict depend on how much of a file an attacker can shape."""
    assert js_ast.is_js_source("a.ts") and not js_ast.is_js_source("a.sh")
    assert comments.comment_lines(source, "a.ts") == comments.js_comments(
        source, "a.ts"
    )


@given(_sources(), st.sampled_from([".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"]))
def test_js_parse_never_crashes_and_stays_in_range(source: str, suffix: str) -> None:
    root = js_ast.parse(source, f"a{suffix}")
    # Not asserted to be a `program`: input that parses to nothing valid yields
    # an ERROR root, which is tree-sitter reporting rather than crashing.
    nbytes = len(source.encode("utf-8"))
    assert 0 <= root.start_byte <= root.end_byte <= nbytes
    for node in root.children:
        assert 0 <= node.start_byte <= node.end_byte <= nbytes
