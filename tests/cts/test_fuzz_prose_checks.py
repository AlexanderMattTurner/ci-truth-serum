"""Property/fuzz tests for the prose/comment-honesty extras: check_drift_guards,
check_graceful_handwave, check_historical_comments, check_doc_line_refs.

Same contract as test_fuzz_parsers.py: the detectors are fed whatever bytes
happen to be staged and must never raise an unexpected exception; every reported
line number refers to a real line; the same input yields the same result. The
example-based suites pin the exact rule semantics — fuzzing pins the invariants
that hold for ALL inputs.
"""

import string

from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

drift_guards = load_hook("check_drift_guards.py", "fuzz_check_drift_guards")
graceful = load_hook("check_graceful_handwave.py", "fuzz_check_graceful_handwave")
historical = load_hook("check_historical_comments.py", "fuzz_check_historical_comments")
doc_line_refs = load_hook("check_doc_line_refs.py", "fuzz_check_doc_line_refs")

# Tokens the detectors actually look for, so generated text isn't all inert
# noise — it hits real branches (guard phrasing, markers, annotations, fences,
# line-cite shapes, comment/URL boundaries, Python syntax fragments).
_TOKENS = [
    "def test_sync():",
    '    """drift guard: the two lists agree"""',
    '@pytest.mark.drift_guard("external value, no SSOT")',
    "@pytest.mark.drift_guard()",
    "async def test_kept_in_sync():",
    "class T:",
    "# a graceful fallback",
    "// degrades gracefully",
    "graceful_shutdown()",
    "# allow-graceful: exits 0 and skips the write",
    "# formerly a no-op",
    "// switched to the lazy reader",
    "# allow-history: parses the legacy shape",
    'msg = "renamed from old to new"',
    "```",
    "```yaml",
    "(L12)",
    "(L12-34)",
    "(L4)",
    "~L660",
    "~:42",
    "L98-110",
    "foo.sh:12-34",
    "bin/tool.py:17",
    "https://example.com/tree/foo.py:42",
    "localhost:8080",
    "<!-- allow-line-ref: pinned banner -->",
    "<!-- allow-line-ref: -->",
    'echo "${#arr}"',
    "    ",
    "\\",
    '"""',
    "#",
    "*",
]

# Newline variants str.splitlines() recognises, written as escapes so no
# invisible byte hides in this source.
_NEWLINES = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"]

_WEIRD = "\u202e\u200b\u200d\ufeff\u0301\U0001f600"

_LINE = st.one_of(
    st.sampled_from(_TOKENS),
    st.text(
        alphabet=string.printable.replace("\n", "").replace("\r", "") + _WEIRD,
        max_size=40,
    ),
)


@st.composite
def _texts(draw) -> str:
    lines = draw(st.lists(_LINE, max_size=12))
    nl = draw(st.sampled_from(_NEWLINES))
    return nl.join(lines)


def _assert_line_numbers_valid(hits: list[int], text: str) -> None:
    n = len(text.splitlines())
    assert all(isinstance(h, int) and 1 <= h <= n for h in hits)
    assert hits == sorted(hits)


@given(_texts())
def test_historical_violations_no_crash_and_valid_lines(text: str) -> None:
    hits = historical.violations(text)
    assert hits == historical.violations(text)  # deterministic
    _assert_line_numbers_valid(hits, text)


@given(_texts(), st.booleans())
def test_graceful_violations_no_crash_and_valid_lines(text: str, prose: bool) -> None:
    hits = graceful.violations(text, prose)
    assert hits == graceful.violations(text, prose)  # deterministic
    _assert_line_numbers_valid(hits, text)
    # code mode can only ever flag a subset of what prose mode sees
    if not prose:
        assert set(hits) <= set(graceful.violations(text, True))


@given(_texts())
def test_drift_guards_violations_no_crash_and_shape(text: str) -> None:
    hits = drift_guards.violations(text)
    assert hits == drift_guards.violations(text)  # deterministic
    for lineno, name in hits:
        assert isinstance(lineno, int) and lineno >= 1
        assert name.startswith("test_")


@given(_texts())
def test_doc_line_refs_violations_no_crash_and_shape(text: str) -> None:
    hits = doc_line_refs.violations(text)
    assert hits == doc_line_refs.violations(text)  # deterministic
    n = len(text.splitlines())
    for lineno, match in hits:
        assert 1 <= lineno <= n
        assert match in text.splitlines()[lineno - 1]
