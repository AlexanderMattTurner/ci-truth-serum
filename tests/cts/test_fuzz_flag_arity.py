"""Property/fuzz test for check_flag_arity.

Same contract as the other parser fuzz suites: fed whatever bytes happen to be
staged, ``violations`` must never raise, every reported line number must refer to
a real line, and the same input must yield the same result. The example-based
suite pins the exact rule semantics; this pins the invariants that hold for ALL
inputs — in particular that the stateful case/arm walker can't run off the end of
a slice on adversarial `case`/`esac`/`;;` nesting.
"""

import string

from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

flag_arity = load_hook("check_flag_arity.py", "fuzz_check_flag_arity")

# Tokens that hit real branches of the walker (case open/close, flag vs non-flag
# labels, guarded/unguarded reads, arm terminators, opt-out markers) instead of
# generating inert noise.
_TOKENS = [
    "#!/usr/bin/env bash",
    "while [[ $# -gt 0 ]]; do",
    'case "$1" in',
    "case $x in",
    "--branch)",
    "-f | --file)",
    "--privacy=*)",
    "doctor)",
    "read) x=$2; shift 2 ;;",
    "*)",
    '  BRANCH="$2"',
    "  shift 2",
    "  shift 3",
    "  [[ $# -ge 2 ]] || die x",
    '  A="${2:?needs a value}"',
    '  need_val "$@"',
    "  # flag-arity-ok: defaulted below",
    "  # flag-arity-ok:",
    "  ;;",
    "  ;&",
    "  ;;&",
    "esac",
    "done",
    'echo "${#arr}"',
    "$2",
    "${2}",
    "#",
    ")",
    "(",
]

# Join variants: `\n`-separated exercises the multi-line walker; the others
# collapse to a single `\n`-line (violations counts lines by `\n`, as does the
# invariant below), stressing long single-line case/arm sequences.
_NEWLINES = ["\n", "\r\n", "\r"]
# Written as escapes so no invisible byte hides in this source (RLO, ZWSP, ZWJ,
# BOM, a combining accent, an astral emoji).
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
    lines = draw(st.lists(_LINE, max_size=14))
    nl = draw(st.sampled_from(_NEWLINES))
    return nl.join(lines)


@given(_texts())
def test_violations_no_crash_valid_lines_and_deterministic(text: str) -> None:
    hits = flag_arity.violations(text)
    assert hits == flag_arity.violations(text)  # deterministic
    n = len(text.split("\n"))
    for lineno, message in hits:
        assert isinstance(lineno, int) and 1 <= lineno <= n
        assert isinstance(message, str) and message
