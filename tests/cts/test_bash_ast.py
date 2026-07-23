"""Example-based tests (mutation oracle) for ci_truth_serum/_bash_ast.py — the shared
tree-sitter-bash wrapper the two shell lints parse through.

Pins the exact contract of ``parse`` and ``iter_nodes``: a real bash tree, node
type filtering, and document (pre-order, source) ordering. The property/fuzz
invariants live in ``test_fuzz_bash_ast.py``; this suite is the per-module oracle
the mutation gate runs, so every assertion is exact.
"""

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


# ── crash-safety: sanitize adversarial bytes before the native parser ────────
def test_safe_byte_table_maps_non_ascii_and_control_to_x_length_preserving() -> None:
    tbl = bash_ast._SAFE_BYTE_TABLE
    assert len(tbl) == 256
    # Printable ASCII plus tab/newline/CR are kept verbatim (real shell syntax).
    for b in [*range(0x20, 0x7F), 0x09, 0x0A, 0x0D]:
        assert tbl[b] == b, hex(b)
    # Every other byte — C0 controls (except \t\n\r), DEL, and all high bytes that
    # carry non-ASCII UTF-8 — maps to 'x' (0x78), the crash-trigger classes removed.
    for b in [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), *range(0x7F, 0x100)]:
        assert tbl[b] == 0x78, hex(b)


def test_parse_replaces_non_ascii_but_preserves_offsets() -> None:
    # A non-ASCII char inside a string is rewritten to 'x' in the bytes tree-sitter
    # sees, yet byte length is preserved so every node offset/row still lines up
    # with the original source (what strip_comments / start_point rely on).
    script = 'x="café"\n'
    root = bash_ast.parse(script)
    assert root.type == "program"
    assert root.end_byte == len(script.encode("utf-8"))
    assert "é" not in root.text.decode()  # the non-ASCII byte class is gone
    # ASCII structure is untouched: the assignment is still parsed as one.
    assert [n.text.decode() for n in bash_ast.iter_nodes(root, "variable_name")] == [
        "x"
    ]


def test_parse_survives_adversarial_unicode() -> None:
    # The byte classes that segfault tree-sitter-bash 0.25.1's native parser:
    # high-plane codepoints, stacked combining marks, zero-width / BOM / paragraph
    # separator, and C0 controls, spliced into shell-ish fragments. Sanitization
    # must let parse() complete and return a real tree instead of crashing.
    adversarial = (
        'done < <(jq -r " ​﻿\U000c0981́́\x0c\x92\U000da6dd\U000ab905 \U000ab905) # c\n'
    ) * 40
    root = bash_ast.parse(adversarial)
    assert root.type == "program"


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
