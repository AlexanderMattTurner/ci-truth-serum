"""Example-based tests (mutation oracle) for ci_truth_serum/_py_ast.py — the shared
stdlib-``ast`` wrapper the Python-reading lints parse through.

Pins the two contracts its callers depend on: node line numbers index the
SOURCE's own physical lines (whole-module parse and per-line recovery alike), and
source that does not parse still yields the lines that do — never an empty
result, which would false-green a half-written file. The property/fuzz invariants
live in ``test_fuzz_parsers.py``.
"""

import ast

import pytest

from tests._helpers import load_hook

py_ast = load_hook("_py_ast.py", "check_py_ast")


def _assignments(source: str) -> list[tuple[int, str]]:
    """(line, dotted target) for every simple assignment the module reports."""
    return [
        (node.lineno, py_ast.name_of(target))
        for tree in py_ast.trees(source)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
    ]


def test_whole_module_parses_as_one_tree() -> None:
    trees = py_ast.trees("a = 1\nb = 2\n")
    assert len(trees) == 1
    assert _assignments("a = 1\nb = 2\n") == [(1, "a"), (2, "b")]


def test_multiline_statement_is_one_node_anchored_at_its_first_line() -> None:
    source = "x = (\n    1,\n    2,\n)\n"
    assert _assignments(source) == [(1, "x")]


def test_unparseable_source_falls_back_to_the_lines_that_do_parse() -> None:
    """A syntax error must not read as "no findings": every line that IS Python
    is still offered, at its own line number."""
    source = "a = 1\ndef broken(:\nb.c = 2\n"
    assert _assignments(source) == [(1, "a"), (3, "b.c")]


def test_a_block_header_alone_parses_as_a_statement() -> None:
    """A fragment that only OPENS a block is completed with a `pass` body, so a
    lint driven on a bare `with …:` line still sees the node."""
    trees = py_ast.trees("with redirect_stdout(buf):")
    calls = [n for tree in trees for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert [py_ast.name_of(call.func) for call in calls] == ["redirect_stdout"]
    assert [call.lineno for call in calls] == [1]


def test_prose_yields_no_trees() -> None:
    assert py_ast.trees("redirect the verdict to sys.stdout in the hook") == []


def test_nul_byte_is_handled_like_a_syntax_error() -> None:
    """``ast`` refuses a NUL outright (ValueError, not SyntaxError); the lines
    around it must still be reachable."""
    assert _assignments("a = 1\n\0\nb = 2\n") == [(1, "a"), (3, "b")]


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_line_numbers_agree_with_lines_for_every_line_ending(ending: str) -> None:
    """Python's parser counts CR and CRLF as line breaks; ``str.split("\\n")``
    counts only the last. ``lines`` is the enumeration that agrees with what
    ``trees`` reports, so an opt-out is never read off the wrong line."""
    source = ending.join(["a = 1", "sys.stdout = buf", "c = 3"])
    assert _assignments(source) == [(1, "a"), (2, "sys.stdout"), (3, "c")]
    assert py_ast.lines(source)[1] == "sys.stdout = buf"


def test_empty_source_is_one_empty_tree() -> None:
    assert _assignments("") == []


@pytest.mark.parametrize(
    "source, expected",
    [
        ("sys.stdout", "sys.stdout"),
        ("stdout", "stdout"),
        ("a.b.c.d", "a.b.c.d"),
        # No stable dotted spelling: the base is a call result / a subscript / a
        # literal, so there is nothing to match a name against.
        ("f().attr", None),
        ("a[0].attr", None),
        ("(1).real", None),
    ],
)
def test_name_of_reads_a_dotted_spelling_or_nothing(
    source: str, expected: str | None
) -> None:
    expression = ast.parse(source, mode="eval").body
    assert py_ast.name_of(expression) == expected
