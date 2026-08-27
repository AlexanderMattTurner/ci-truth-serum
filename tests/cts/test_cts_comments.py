"""Example-based tests (mutation oracle) for ci_truth_serum/_cts_comments.py — the one
place the comment-reading lints ask "which lines carry narration".

Pins each extractor against the text scan it replaces: the cases here are the
ones where "looks like a comment" and "is a comment" disagree, in both
directions. The property/fuzz invariants live in ``test_fuzz_comments.py``.
"""

import tokenize

import pytest

from tests._helpers import load_hook

comments = load_hook("_cts_comments.py", "check_comments")


# ── python_comments ──────────────────────────────────────────────────────
def test_python_comments_reads_tokens_not_text() -> None:
    """A `#` inside a string literal is a character, not a comment; a real
    trailing comment is one whatever spacing precedes it. The text heuristic
    gets both backwards."""
    src = 'MSG = "run # canonical, mirrored"\nx = 1  # a real comment\ny = 2 #tight\n'
    assert comments.python_comments(src) == {2: "# a real comment", 3: "#tight"}


def test_python_comments_ignores_a_docstring() -> None:
    """A docstring is a value the module builds, so `#` inside one is not
    narration about the tree — the shape every lint's own fixtures produce."""
    src = 'def f():\n    """add a # allow-history: <reason> comment"""\n    return 1\n'
    assert comments.python_comments(src) == {}


def test_python_comments_raises_on_source_that_does_not_tokenize() -> None:
    """Loud at this layer; ``comment_lines`` is where the fallback decision is
    made and stated."""
    with pytest.raises(tokenize.TokenError):
        comments.python_comments("# narration\nx = (\n")


# ── shell_comments ───────────────────────────────────────────────────────
def test_shell_comments_reads_the_grammar() -> None:
    """Quoted `#` and heredoc bodies are not comments, and the reported line
    numbers are the real ones."""
    script = (
        "# a real comment\n"
        "cat <<'EOF' > d.txt\n"
        "# heredoc data, not a comment\n"
        "EOF\n"
        'gb_warn "# printed, not a comment"\n'
        "x=1  # trailing\n"
    )
    assert comments.shell_comments(script) == {1: "# a real comment", 6: "# trailing"}


# ── js_comments ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "source, expected",
    [
        ("// a line comment", {1: "// a line comment"}),
        ("run();  // trailing", {1: "// trailing"}),
        # No ` // ` delimiter on the line, so the text scan read it as pure code.
        ("run(); /* trailing block */", {1: "/* trailing block */"}),
        # A block comment is ONE node over many lines; every line of it is
        # narration, including the first and last, which carry no ` * `.
        (
            "/** one\n * two\n */",
            {1: "/** one", 2: " * two", 3: " */"},
        ),
        # `//` inside a string or a template literal opens nothing.
        ('const m = "see // not a comment";', {}),
        ("const t = `a // b`;", {}),
        ('const u = "https://x/y";', {}),
        # A comment inside a template substitution is still a comment.
        ("const t = `a ${/* real */ 1} b`;", {1: "/* real */"}),
        # Two comments on one line are joined, so a phrase is matched within one
        # comment rather than across the pair.
        ("/*a*/ x; /*b*/", {1: "/*a*/\n/*b*/"}),
        ("", {}),
    ],
)
def test_js_comments(source: str, expected: dict[int, str]) -> None:
    assert comments.js_comments(source + "\n", "suite.test.mjs") == expected


def test_js_comments_uses_the_typescript_grammar_for_ts() -> None:
    """Under the JavaScript grammar the annotation is a syntax error and the
    comment after it is lost — the recall hole a single default grammar has."""
    source = "const rows: Record<string, number> = load();\n// narration\n"
    assert comments.js_comments(source, "a.ts") == {2: "// narration"}


def test_js_comments_survives_malformed_source() -> None:
    """A half-written suite yields a partial tree, so the comments before the
    error are still read rather than the hook dying on an unrelated commit."""
    assert comments.js_comments("// keep me\nfunction ( {\n", "a.js") == {
        1: "// keep me"
    }


# ── text_comments: the no-grammar fallback ───────────────────────────────
def test_text_comments_scans_by_delimiter() -> None:
    text = "key: 1  # trailing\n# full line\nplain: 2\n"
    assert comments.text_comments(text) == {1: "# trailing", 2: "# full line"}


# ── comment_lines: the dispatcher ────────────────────────────────────────
@pytest.mark.parametrize(
    "path, source, expected",
    [
        # Python: the string literal is not a comment.
        ("a.py", 'S = "# no"\n# yes\n', {2: "# yes"}),
        ("stubs/a.pyi", "# yes\n", {1: "# yes"}),
        # Shell by suffix, and shell by shebang on an extensionless path.
        ("a.sh", "cat <<'EOF'\n# no\nEOF\n# yes\n", {4: "# yes"}),
        (
            ".hooks/pre-commit",
            "#!/usr/bin/env bash\ncat <<'EOF'\n# no\nEOF\n# yes\n",
            {1: "#!/usr/bin/env bash", 5: "# yes"},
        ),
        # JS/TS by suffix.
        ("a.test.mjs", 'const m = "// no";\n// yes\n', {2: "// yes"}),
        ("a.ts", "let x: number = 1; // yes\n", {1: "// yes"}),
        # No grammar for YAML — the delimiter scan owns it, stated rather than
        # defaulted.
        ("a.yaml", "key: 1  # yes\n", {1: "# yes"}),
        ("a.md", "# yes\n", {1: "# yes"}),
    ],
)
def test_comment_lines_picks_the_grammar_the_path_names(
    path: str, source: str, expected: dict[int, str]
) -> None:
    assert comments.comment_lines(source, path) == expected


@pytest.mark.parametrize(
    "tail",
    [
        "x = (\n",  # TokenError: EOF in multi-line statement
        "\tif x:\n  pass\n",  # IndentationError, a SyntaxError subclass
    ],
)
def test_comment_lines_falls_back_when_python_does_not_tokenize(tail: str) -> None:
    """ "No findings" on an unparseable file is the false green this pack exists
    to catch, so an unfinished edit keeps the lint running on the text scan;
    other tooling owns the syntax error itself."""
    assert comments.comment_lines("# narration\n" + tail, "a.py") == {1: "# narration"}
