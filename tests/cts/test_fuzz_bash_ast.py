"""Property/fuzz test for ci_truth_serum/_bash_ast — the shared tree-sitter-bash wrapper
both shell lints parse through.

The whole point of swapping the hand-rolled tokenizers for a real grammar is
crash-resistance on adversarial shell: whatever bytes are staged get parsed, so
``parse`` must never raise, must be deterministic, and must return a well-formed
tree whose node byte offsets stay in range and land on real lines. If this
degrades, every consuming lint silently mis-parses — the false green the pack
exists to catch — so the invariants are pinned directly on the parser here, not
only transitively through ``analyze`` / ``violations``.
"""

import string

from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

bash_ast = load_hook("_bash_ast.py", "fuzz_bash_ast")

# Shell tokens that exercise the constructs the old tokenizers mis-handled — the
# ones that must parse into real structure rather than desync a quote counter.
_TOKENS = [
    "case",
    "esac",
    "in",
    "--flag)",
    ";;",
    "|",
    "|&",
    "||",
    ">|",
    "<<EOF",
    "<<'EOF'",
    "EOF",
    "$'a\\'b'",
    'echo "a\\""',
    "`cmd`",
    "$(cmd)",
    "set -o pipefail",
    "# comment",
    "'",
    '"',
    "\\",
]
# Escapes so no invisible byte hides here: RLO, ZWSP, ZWJ, BOM, combining accent,
# astral emoji.
_WEIRD = "\u202e\u200b\u200d\ufeff\u0301\U0001f600"
_LINE = st.one_of(
    st.sampled_from(_TOKENS),
    st.text(alphabet=string.printable + _WEIRD, max_size=40),
)


@st.composite
def _scripts(draw) -> str:
    return draw(st.sampled_from(["\n", "\r\n", "\r", " ", " | "])).join(
        draw(st.lists(_LINE, max_size=16))
    )


@given(_scripts())
def test_parse_never_crashes_is_deterministic_and_in_range(script: str) -> None:
    root = bash_ast.parse(script)
    # Deterministic: same bytes → same tree shape (node count + top-level type).
    again = bash_ast.parse(script)
    assert root.type == again.type
    nbytes = len(script.encode("utf-8"))
    nlines = len(script.split("\n"))
    for node in bash_ast.iter_nodes(
        root, *{n.type for n in root.children} | {root.type}
    ):
        assert 0 <= node.start_byte <= node.end_byte <= nbytes
        assert 1 <= node.start_point[0] + 1 <= nlines


@given(_scripts())
def test_iter_nodes_yields_only_requested_types(script: str) -> None:
    root = bash_ast.parse(script)
    got = list(bash_ast.iter_nodes(root, "pipeline", "case_item"))
    assert all(n.type in {"pipeline", "case_item"} for n in got)
