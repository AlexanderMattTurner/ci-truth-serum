"""Tests for ci_truth_serum/check_replacement_expansion.py — the lint that bans an
assembled string in a replacement position.

The value of the lint is the line it draws. A replacement the author can read in
full is fine; a replacement built out of other values carries whatever those
values hold into a pattern parser. So both sides of that line are asserted
member by member, in both languages.

Two probes decide whether the lint reads structure or text, the same pair
`.claude/rules/shell-lint-parsing.md` prescribes for the shell lints: the banned
idiom inside a comment, and inside a string literal. Neither is executed code, so
a finding on either is a false positive.

Drives ``violations`` for the rules, ``main()`` for the argv and exit-code
contract, and two Hypothesis properties for crash-resistance.
"""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

mod = load_hook("check_replacement_expansion.py", "check_replacement_expansion")

# The defect that motivated the lint, reduced: the release section is built with
# an interpolation, and a fragment holding `$'` copies the rest of the file in.
ASSEMBLER = "changelog.replace(MARKER, `${MARKER}\n\n${body}`);\n"


# ── JavaScript: assembled replacements fire ──────────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        ("interpolating template", "s.replace(m, `${a}`);\n"),
        ("template with text around the hole", "s.replace(m, `head ${a} tail`);\n"),
        ("replaceAll takes the same argument", "s.replaceAll(m, `${a}`);\n"),
        ("concatenation with a literal", 's.replace(m, "a" + b);\n'),
        ("concatenation the other way round", 's.replace(m, b + "a");\n'),
        # The name sits in an INNER node of the chain; reading only the outer
        # node's own children finds a group and a literal, and misses it.
        ("a name nested inside a chain", 's.replace(m, ("a" + b) + "c");\n'),
        ("a template joined to a literal", 's.replace(m, `${a}` + "c");\n'),
        ("a deeper receiver still resolves", "x.y.z.replace(m, `${a}`);\n"),
        ("extra arguments do not shift the replacement", "s.replace(m, `${a}`, z);\n"),
        ("the assembler's own call", ASSEMBLER),
    ],
)
def test_js_assembled_replacement_fires(name: str, src: str) -> None:
    assert mod.violations(src, "x.mjs") == [1], name


def test_js_name_bound_to_an_assembled_string_fires() -> None:
    """One hop is followed: naming the assembled string does not hide it."""
    src = "const insert = `${a}b`;\ns.replace(m, insert);\n"
    assert mod.violations(src, "x.mjs") == [2]


def test_js_name_assigned_later_fires_too() -> None:
    """A plain assignment binds the same way a declaration does."""
    src = "let insert;\ninsert = `${a}`;\ns.replace(m, insert);\n"
    assert mod.violations(src, "x.mjs") == [3]


@pytest.mark.parametrize(
    "name, src",
    [
        ("a plain literal", 's.replace(m, "lit");\n'),
        ("a template with no hole", "s.replace(m, `plain`);\n"),
        ("an arrow function — the fix this lint asks for", "s.replace(m, () => x);\n"),
        ("a function expression", "s.replace(m, function () {});\n"),
        ("a named function reference", "s.replace(m, escapeChar);\n"),
        ("two literals joined", 's.replace(m, "a" + "b");\n'),
        ("three literals joined", 's.replace(m, "a" + "b" + "c");\n'),
        ("two plain templates joined", "s.replace(m, `a` + `b`);\n"),
        ("one argument, so no replacement at all", "s.replace(m);\n"),
        ("a method that is not replace", "s.slice(m, `${a}`);\n"),
        ("a bare call, not a method", "replace(m, `${a}`);\n"),
        ("an unrelated name ending in replace", "s.myreplace(m, `${a}`);\n"),
    ],
)
def test_js_clean_calls_do_not_fire(name: str, src: str) -> None:
    assert mod.violations(src, "x.mjs") == [], name


# ── the two probes: is this structure, or text? ──────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        ("the idiom inside a line comment", f"// {ASSEMBLER}"),
        ("the idiom inside a block comment", f"/* {ASSEMBLER} */\n"),
        ("the idiom inside a string literal", 'const doc = "s.replace(m, `${a}`)";\n'),
        (
            "the idiom inside a template literal",
            "const doc = `s.replace(m, ${x})`;\n",
        ),
    ],
)
def test_js_text_that_is_not_code_does_not_fire(name: str, src: str) -> None:
    assert mod.violations(src, "x.mjs") == [], name


def test_typescript_is_parsed_with_its_own_grammar() -> None:
    """A `.ts` file parsed as JavaScript is ERROR nodes from its first type
    annotation on, and every call after that is lost."""
    src = "function f(a: string): string {\n  return a.replace(m, `${a}`);\n}\n"
    assert mod.violations(src, "x.ts") == [2]


def test_the_opt_out_suppresses_and_a_bare_marker_does_not() -> None:
    call = "s.replace(m, `${a}`);\n"
    assert (
        mod.violations(f"// {mod.OPT_OUT}: the value is a fixed id\n{call}", "x.mjs")
        == []
    )
    assert (
        mod.violations(f"{call.rstrip()}  // {mod.OPT_OUT}: fixed id\n", "x.mjs") == []
    )
    # A marker with no reason states nothing, so it must not suppress.
    assert mod.violations(f"// {mod.OPT_OUT}\n{call}", "x.mjs") == [2]


# ── Python: the same rule, the same evidence ─────────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        ("an f-string", 'import re\nre.sub(p, f"{a}", s)\n'),
        ("a percent format", 'import re\nre.sub(p, "%s" % a, s)\n'),
        ("a concatenation with a literal", 'import re\nre.sub(p, "a" + b, s)\n'),
        ("a name nested inside a chain", 'import re\nre.sub(p, ("a" + b) + "c", s)\n'),
        ("a str.format call", 'import re\nre.sub(p, "{}".format(a), s)\n'),
        ("subn takes the same argument", 'import re\nre.subn(p, f"{a}", s)\n'),
        ("the replacement passed by keyword", 'import re\nre.sub(p, repl=f"{a}")\n'),
        ("an aliased module", 'import re as rx\nrx.sub(p, f"{a}", s)\n'),
        ("a bare name from a from-import", 'from re import sub\nsub(p, f"{a}", s)\n'),
        ("an aliased from-import", 'from re import sub as s_\ns_(p, f"{a}", x)\n'),
    ],
)
def test_py_assembled_replacement_fires(name: str, src: str) -> None:
    assert mod.violations(src, "x.py") == [2], name


def test_py_name_bound_to_an_assembled_string_fires() -> None:
    """The shape the shipped defect had: an f-string bound, then substituted."""
    src = 'import re\npinned = f"{image}@{digest}"\nre.sub(p, pinned, rest)\n'
    assert mod.violations(src, "x.py") == [3]


def test_py_compiled_pattern_method_shifts_the_argument() -> None:
    """`PATTERN.sub(repl, string)` holds the replacement FIRST — reading position
    1 there would judge the subject string instead."""
    fires = 'import re\nR = re.compile("a")\nR.sub(f"{a}", s)\n'
    clean = 'import re\nR = re.compile("a")\nR.sub(cb, f"{a}")\n'
    assert mod.violations(fires, "x.py") == [3]
    assert mod.violations(clean, "x.py") == []


@pytest.mark.parametrize(
    "name, src",
    [
        ("a plain literal", 'import re\nre.sub(p, "lit", s)\n'),
        ("literals joined", 'import re\nre.sub(p, "a" + "b" + "c", s)\n'),
        ("a literal percent format", 'import re\nre.sub(p, "%s" % "x", s)\n'),
        (
            "a lambda — the fix this lint asks for",
            "import re\nre.sub(p, lambda _: x, s)\n",
        ),
        ("a named function reference", "import re\nre.sub(p, escape, s)\n"),
        (
            "a name bound to something unassembled",
            "import re\nr = x\nre.sub(p, r, s)\n",
        ),
        ("a different re function", 'import re\nre.split(p, f"{a}")\n'),
        ("some other object's sub method", 'import re\nobj.sub(f"{a}", s)\n'),
        (
            "the idiom inside a string literal",
            'import re\nmsg = "re.sub(p, f\\"{a}\\", s)"\n',
        ),
        ("the idiom inside a comment", 'import re\n# re.sub(p, f"{a}", s)\n'),
    ],
)
def test_py_clean_calls_do_not_fire(name: str, src: str) -> None:
    assert mod.violations(src, "x.py") == [], name


def test_a_python_file_that_does_not_parse_still_reports_its_real_lines() -> None:
    """ "No findings" on a half-written file would be a false green."""
    assert mod.violations('def broken(:\nre.sub(p, f"{a}", s)\n', "x.py") == [2]


def test_a_path_of_neither_language_has_no_grammar_and_no_findings() -> None:
    assert mod.violations('re.sub(p, f"{a}", s)\n', "notes.md") == []


def test_main_wires_violations_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() runs this detector through the shared loop with its own message.
    The loop itself is covered in test_cts_linecheck.py."""
    bad = tmp_path / "bad.mjs"
    bad.write_text(f"const body = 1;\n{ASSEMBLER}", encoding="utf-8")
    clean = tmp_path / "clean.mjs"
    clean.write_text('s.replace(m, "lit");\n', encoding="utf-8")
    assert mod.main([str(clean)]) == 0
    assert mod.main([str(bad)]) == 1
    assert f"{bad}:2: passes an assembled string" in capsys.readouterr().err


# ── properties: crash-resistance and in-range line numbers ───────────────
_FRAGMENTS = st.sampled_from(
    [
        "s.replace(m, `${a}`);\n",
        "s.replace(m, x);\n",
        'const x = "a" + b;\n',
        "// comment\n",
        "/* block\n",
        "`unterminated\n",
        'import re\nre.sub(p, f"{a}", s)\n',
        "R = re.compile(p)\n",
        "def f(:\n",
        "\r\n",
        " ",
        "\U0001f600",
        "\\",
        "'",
    ]
)


@given(
    st.lists(_FRAGMENTS, max_size=40).map("".join),
    st.sampled_from(["x.mjs", "x.ts", "x.py"]),
)
def test_violations_never_raises_and_reports_real_lines(text: str, path: str) -> None:
    found = mod.violations(text, path)
    assert all(1 <= lineno <= max(len(text.splitlines()), 1) for lineno in found)


@given(
    st.lists(_FRAGMENTS, max_size=40).map("".join), st.sampled_from(["x.mjs", "x.py"])
)
def test_violations_is_deterministic(text: str, path: str) -> None:
    assert mod.violations(text, path) == mod.violations(text, path)


def test_py_annotated_compiled_pattern_is_recognized() -> None:
    """An annotated binding compiles a pattern the same way a plain one does."""
    src = 'import re\nR: re.Pattern[str] = re.compile("a")\nR.sub(f"{a}", s)\n'
    assert mod.violations(src, "x.py") == [3]
