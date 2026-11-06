"""Property/fuzz tests for `_cts_gha_expr.parse`: any text either parses to a tree
or raises `Unsupported`, and the same text always gives the same answer."""

from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

gha = load_hook("_cts_gha_expr.py", "fuzz_gha_expr")

_PIECES = st.sampled_from(
    ["a", "b-c", ".", "[", "]", "(", ")", ",", "'x'", "''", "*", " ", "1", "||", "!"]
)


@given(st.one_of(st.text(), st.lists(_PIECES, max_size=20).map("".join)))
def test_parse_returns_a_tree_or_refuses(text: str) -> None:
    try:
        tree = gha.parse(text)
    except gha.Unsupported:
        return
    assert isinstance(tree, (gha.Literal, gha.Ref, gha.Call))
    assert gha.parse(text) == tree
