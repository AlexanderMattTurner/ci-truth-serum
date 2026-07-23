"""Example-based tests (mutation oracle) for ci_truth_serum/_bash_ast.py — the shared
tree-sitter-bash wrapper the two shell lints parse through.

Pins the exact contract of ``parse`` and ``iter_nodes``: a real bash tree, node
type filtering, and document (pre-order, source) ordering. The property/fuzz
invariants live in ``test_fuzz_bash_ast.py``; this suite is the per-module oracle
the mutation gate runs, so every assertion is exact.
"""

import pytest

from tests._helpers import load_hook

bash_ast = load_hook("_bash_ast.py", "check_bash_ast")


def _types(script: str, *types: str) -> list[str]:
    root = bash_ast.parse(script)
    return [n.text.decode() for n in bash_ast.iter_nodes(root, *types)]


def test_parse_returns_the_program_root() -> None:
    root = bash_ast.parse("echo hi")
    assert root.type == "program"
    assert root.text.decode() == "echo hi"


def test_parse_is_reusable_across_calls() -> None:
    # The cached parser must keep working on a second, different input.
    assert bash_ast.parse("a | b").type == "program"
    assert bash_ast.parse("case $x in a) ;; esac").type == "program"


def test_iter_nodes_filters_to_requested_type() -> None:
    assert _types("a | b\nc || d\ne | f", "pipeline") == ["a | b", "e | f"]


def test_iter_nodes_yields_source_order_not_reversed() -> None:
    # Kills the mutant dropping `reversed(...)`: commands must come out first-to-last.
    assert _types("first; second; third", "command") == ["first", "second", "third"]


def test_iter_nodes_multiple_types() -> None:
    got = _types("run x  # note\nrun y", "command", "comment")
    assert got == ["run x", "# note", "run y"]


def test_iter_nodes_includes_the_root_when_its_type_is_requested() -> None:
    root = bash_ast.parse("echo hi")
    assert [n.type for n in bash_ast.iter_nodes(root, "program")] == ["program"]


def test_iter_nodes_empty_types_yields_nothing() -> None:
    root = bash_ast.parse("a | b | c")
    assert list(bash_ast.iter_nodes(root)) == []


def test_iter_nodes_finds_nested_nodes() -> None:
    # A pipe inside a command substitution is a real nested pipeline node.
    assert _types("echo $(a | b)", "pipeline") == ["a | b"]


def test_string_and_comment_pipes_are_not_pipelines() -> None:
    assert _types('echo "a | b"  # c | d', "pipeline") == []


# ── pathological-input refusal ────────────────────────────────────────────
def test_parse_refuses_quadratic_pipe_chains_loudly() -> None:
    """tree-sitter-bash allocates quadratically on chained pipeline stages
    (~3.3 GB at 20k `cmd |` stages, measured), failing as a C-level segfault
    rather than a Python error. `parse` refuses such input with a LOUD
    PathologicalInputError — never a silent no-findings pass."""
    with pytest.raises(bash_ast.PathologicalInputError):
        bash_ast.parse("x | " * (bash_ast._MAX_PIPE_BYTES + 1))
    # Just under the cap parses normally.
    assert bash_ast.parse("x | x").type == "program"


# ── supplementary-plane (non-BMP) neutralization ──────────────────────────
# tree-sitter-bash 0.25's C scanner corrupts the heap on an astral codepoint
# (≥ U+10000) next to a word-opening token like `{`, segfaulting the process
# non-deterministically. `parse` folds every non-BMP char to U+FFFD first.
_ASTRAL_CRASHERS = (0x10FFFF, 0xC6E8E, 0x10000, 0x1F600, 0x20000)


def test_neutralize_supplementary_folds_only_non_bmp() -> None:
    # Non-BMP → U+FFFD; BMP (incl. U+FFFF and multibyte U+00FF/U+0800) untouched;
    # character count and every line boundary preserved for caller alignment.
    src = "a{\U0010ffff\n\u00ff\uffff\U0001f600b"
    out = bash_ast._neutralize_supplementary(src)
    assert out == "a{\ufffd\n\u00ff\uffff\ufffdb"
    assert len(out) == len(src)
    # Line boundaries survive the fold, so line count and offsets stay aligned.
    assert len(out.splitlines()) == len(src.splitlines())
    # Pure-BMP input is returned unchanged (fast path) and the fold is idempotent.
    assert bash_ast._neutralize_supplementary("echo hi") == "echo hi"
    assert bash_ast._neutralize_supplementary(out) == out


def test_parse_survives_astral_next_to_brace() -> None:
    """Behavior oracle for the crash: on the unfixed parser these inputs
    segfaulted the whole process (the xdist worker died, "node down"); the fold
    makes every one parse to a normal program root instead."""
    for cp in _ASTRAL_CRASHERS:
        for tail in ("", "}", " x"):
            for _ in range(200):
                assert bash_ast.parse("{" + chr(cp) + tail).type == "program"


def test_strip_comments_stays_aligned_with_astral_char() -> None:
    # An astral char sits in code before a trailing comment: the comment is
    # blanked, the astral char and layout are preserved, and length is unchanged
    # (the byte-offset map is built against the neutralized string).
    src = "run \U0001f600  # note\nx\n"
    out = bash_ast.strip_comments(src)
    assert out == "run \U0001f600" + " " * 8 + "\nx\n"
    assert len(out) == len(src)
    assert out.splitlines() == ["run \U0001f600" + " " * 8, "x"]


def test_strip_comments_blanks_comment_keeps_layout() -> None:
    # A trailing comment is blanked to spaces; the code before it, the newline, and
    # every column offset are preserved so line-oriented lints stay aligned.
    src = "curl -o f url  # sha256sum later\nrun f\n"
    out = bash_ast.strip_comments(src)
    assert out == "curl -o f url                   \nrun f\n"
    assert out.splitlines() == ["curl -o f url                   ", "run f"]
    assert len(out) == len(src)


def test_strip_comments_blanks_full_line_comment() -> None:
    src = "a\n# TODO: verify with sha256sum\nb\n"
    assert bash_ast.strip_comments(src).splitlines() == [
        "a",
        " " * len("# TODO: verify with sha256sum"),
        "b",
    ]


def test_strip_comments_leaves_hash_inside_quotes_and_words() -> None:
    # The grammar, not a naive `#` split, decides what a comment is: a `#` inside a
    # quoted string or a word is code and is left untouched.
    assert bash_ast.strip_comments('curl -o "a#b" url\n') == 'curl -o "a#b" url\n'
    assert bash_ast.strip_comments("echo x#y\n") == "echo x#y\n"
