"""Property/fuzz tests for ci_truth_serum/check_unnamed_regex_groups.py.

This lint parses Python source with ``ast`` and inspects ``re.*`` calls whose
first argument is a string literal. Crash-resistance contract: feeding it
arbitrary Python text (valid, syntactically broken, weird unicode) must yield
findings or nothing, never an unhandled exception. A second invariant: every
reported line number addresses a real line of the input.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

unnamed = load_hook("check_unnamed_regex_groups.py", "fuzz_unnamed_regex")

# Snippets that hit real branches: re.* calls with literal / non-literal / no
# args, named vs unnamed groups, non-re attribute calls, and plain noise.
_PY_FRAGMENTS = [
    "import re\n",
    "re.compile('(a)(b)')\n",
    "re.match('(?P<x>a)', s)\n",
    "re.search('(?:a)', s)\n",
    "re.sub(pattern, repl, s)\n",
    "re.findall(f'{x}', s)\n",
    "re.compile()\n",
    "obj.compile('(a)')\n",
    "re.compile('[unterminated')\n",
    "x = 1\n",
    "def f():\n    return re.split('(,)', s)\n",
    "# a comment with re.compile('(a)')\n",
    "re.compile('(a' + ')')\n",
]


@st.composite
def python_text(draw: st.DrawFn) -> str:
    parts = draw(st.lists(st.sampled_from(_PY_FRAGMENTS), max_size=6))
    if draw(st.booleans()):
        parts.append(draw(st.text(max_size=60)))
    return "".join(parts)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(text=python_text())
def test_check_file_never_crashes(text: str, tmp_path_factory) -> None:
    path = tmp_path_factory.mktemp("src") / "m.py"
    path.write_text(text, encoding="utf-8")
    result = unnamed.check_file(path)
    assert isinstance(result, list)
    n_lines = len(text.splitlines())
    for item in result:
        assert isinstance(item, tuple) and len(item) == 2
        lineno, pattern = item
        assert isinstance(lineno, int) and isinstance(pattern, str)
        # The reported line must address a real line of the source.
        assert 1 <= lineno <= max(n_lines, 1)


@given(pattern=st.text(max_size=40))
def test_has_unnamed_group_never_crashes(pattern: str) -> None:
    # Fed an arbitrary (often invalid) regex; must answer bool, never raise.
    assert isinstance(unnamed._has_unnamed_group(pattern), bool)
