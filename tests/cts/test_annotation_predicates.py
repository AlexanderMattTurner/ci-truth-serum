"""Meta-contract: annotation/opt-out tokens are matched by the SHARED matcher.

The bug class this pins closed: a hook testing ``"some-token-ok" in line`` (a
bare substring) honors the token ANYWHERE in the byte stream — inside a
``group: "<token>"`` string value, a printed message, a URL — so live data can
silently disable the lint (a fail-open). The shared ``_cts_linecheck.annotation_re``
/ ``annotated`` matcher scopes the token to a real comment (and, where the
hook's contract demands it, requires a ``: <reason>``); every hook that
recognizes a per-line annotation must build its predicate there.

``opted_out`` (the concurrency lints' whole-file comment scan) is the one other
sanctioned matcher — it delegates to ``annotation_re``.

A SECOND population is banned here for a different reason. A hand-rolled
``re.compile(r"#\\s*<token>:\\s*\\S")`` is not fail-open on its face — the literal
``#`` and ``:`` supply both edges — so it survived the first ban. It is banned
anyway because it is behaviour-EQUIVALENT to ``annotation_re(token)``, i.e. it is
a copy of a contract that has since moved twice, and both divergences were live
bugs: every hand-spelled copy wrote the reason gap as ``\\s*``, which crosses a
NEWLINE where the builder's same-line class cannot (so a bare ``# <token>:``
ending a line borrowed the next line's first character as its "reason"), and none
of them carried the stand-alone-token edges the builder now guarantees.

Deliberately NOT banned: a matcher that PARSES A VALUE out of the annotation (a
named capture group) rather than merely testing for one — ``# gate-deps: <paths>``,
``# env-symmetry-ok: <NAME>``, ``# required-check: true|false``. Those are
annotation *readers* with their own grammar, not boolean opt-out predicates, and
``annotation_re`` does not model them.
"""

import re

from tests._helpers import HOOKS_DIR, load_hook

lc = load_hook("_cts_linecheck.py", "lc_for_annotation_contract")

# The bare-substring predicate this contract bans: an annotation-shaped string
# literal (or the conventional ALLOW/OPT_OUT constant naming one) used with a
# bare `in` containment test.
_BARE_TOKEN_LITERAL = re.compile(
    r"""["'](?:allow-[\w-]+|[\w-]+-(?:ok|exempt))["']\s+in\s+"""
)
_BARE_TOKEN_CONSTANT = re.compile(r"\b(?:_ALLOW|ALLOW|OPT_OUT)\s+in\s+")

# An open-coded equivalent of `annotation_re(token)`: a compiled pattern that is
# comment-scoped (`#`) and asserts a reason is PRESENT (`:\s*\S` — a bare
# presence tail, no value extracted). The absence of a named group is what
# separates this from the sanctioned annotation READERS; `_COMPILED_PATTERN`
# pulls the pattern text out first so the two tests can be applied to it.
_COMPILED_PATTERN = re.compile(r"""re\.compile\(\s*r?f?(?P<q>["'])(?P<pat>.*?)(?P=q)""")
_COMMENT_SCOPED_REASON_TAIL = re.compile(r"#.*:\\s\*(?:\\S|\[\^\\s\])")


def _hook_sources() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(HOOKS_DIR.glob("check_*.py"))
    }


def test_no_hook_uses_a_bare_substring_annotation_predicate() -> None:
    offenders = []
    for name, src in _hook_sources().items():
        for lineno, line in enumerate(src.splitlines(), 1):
            if _BARE_TOKEN_LITERAL.search(line) or _BARE_TOKEN_CONSTANT.search(line):
                offenders.append(f"{name}:{lineno}")
    assert offenders == [], f"bare-substring annotation predicates: {offenders}"


def _handrolled_annotation_matchers(src: str) -> list[int]:
    """1-based line numbers of compiled patterns in SRC that reimplement
    `annotation_re(token)`: comment-scoped, reason-presence-asserting, and
    extracting nothing (no named group)."""
    hits = []
    for lineno, line in enumerate(src.splitlines(), 1):
        for match in _COMPILED_PATTERN.finditer(line):
            pat = match.group("pat")
            if _COMMENT_SCOPED_REASON_TAIL.search(pat) and "(?P<" not in pat:
                hits.append(lineno)
    return hits


def test_no_hook_hand_rolls_the_shared_annotation_matcher() -> None:
    offenders = {
        name: lines
        for name, src in _hook_sources().items()
        if (lines := _handrolled_annotation_matchers(src))
    }
    assert offenders == {}, (
        "these compiled patterns reimplement annotation_re(token) — build the "
        f"matcher with _cts_linecheck.annotation_re instead: {offenders}"
    )


def test_handrolled_matcher_detector_actually_matches() -> None:
    """Non-vacuity: the detector recognizes the real spellings this repo shipped
    (both were live, and one was a live newline-crossing fail-open), and stays
    silent on the annotation READERS that legitimately parse a value."""
    assert _handrolled_annotation_matchers(
        '_ALLOW_RE = re.compile(rf"#\\s*{ALLOW}\\s*:\\s*\\S")'
    ) == [1]
    assert _handrolled_annotation_matchers(
        '_ALLOW_WITH_REASON = re.compile(r"#\\s*allow-no-timeout:\\s*\\S")'
    ) == [1]
    assert _handrolled_annotation_matchers(
        '_A = re.compile(r"#\\s*tok:\\s*[^\\s]")'
    ) == [1]
    # Sanctioned: extracts a value, so it is a reader with its own grammar.
    assert (
        _handrolled_annotation_matchers(
            '_GATE_DEPS = re.compile(r"#\\s*gate-deps:\\s*(?P<paths>\\S.*?)\\s*$")'
        )
        == []
    )
    # Sanctioned: the shared builder itself.
    assert _handrolled_annotation_matchers("_RE = annotation_re(ALLOW)") == []


def test_banned_idiom_detectors_actually_match() -> None:
    """Non-vacuity: the detectors above still recognize every accepted spelling
    of the banned idiom — if a refactor of this test's regexes stopped matching,
    the ban would pass silently forever."""
    assert _BARE_TOKEN_LITERAL.search('if "pipefail-grep-ok" in raw:')
    assert _BARE_TOKEN_LITERAL.search("if 'pin-exempt' in line:")
    assert _BARE_TOKEN_LITERAL.search('if "allow-stderr-suppress" in line:')
    assert _BARE_TOKEN_CONSTANT.search("if OPT_OUT in logical or x:")
    assert _BARE_TOKEN_CONSTANT.search("if _ALLOW in physical[lineno - 2]:")
    # ...and do NOT fire on the sanctioned shared-matcher calls.
    assert not _BARE_TOKEN_LITERAL.search('annotated(line, "pin-exempt")')
    assert not _BARE_TOKEN_CONSTANT.search("annotated(raw, _ALLOW)")
    assert not _BARE_TOKEN_CONSTANT.search("opted_out(text, OPT_OUT)")


def test_hooks_route_through_the_shared_matcher() -> None:
    """Positive marker: the ban above is satisfied by USING the shared matcher,
    not by hooks dropping their annotations. A healthy majority of the hook
    modules reference annotated()/annotation_re()/opted_out()."""
    users = [
        name
        for name, src in _hook_sources().items()
        if re.search(r"\b(?:annotated|annotation_re|opted_out)\(", src)
    ]
    assert len(users) >= 15, f"only {len(users)} hooks use the shared matcher: {users}"


def test_shared_matcher_is_comment_scoped_and_reason_bearing() -> None:
    """The matcher's own contract: comment-scoped (a token in live data never
    suppresses), reason-required by default, bare-token form on request."""
    token = "example-ok"
    # comment-scoped, reason present
    assert lc.annotated("cmd  # example-ok: bounded output", token)
    assert lc.annotated("<!-- example-ok: documents the token -->", token)
    assert lc.annotated("code // example-ok: reason", token)
    # no reason -> only the require_reason=False form matches
    assert not lc.annotated("cmd  # example-ok", token)
    assert lc.annotated("cmd  # example-ok", token, require_reason=False)
    # outside any comment: never a suppression, reason or not
    assert not lc.annotated('group: "example-ok: yes"', token)
    assert not lc.annotated("echo example-ok: reason", token, require_reason=False)


def test_the_required_reason_may_not_be_borrowed_from_the_next_line() -> None:
    """The reason must sit on the annotation's OWN line.

    Several hooks scan a multi-line span (a job block, a whole file) with this
    matcher rather than one line at a time. A reason tail that could cross a
    line boundary would let a bare `# <token>:` at end-of-line adopt the next
    line's first character as its "reason" and suppress the lint with an empty
    claim — a fail-open. Every boundary below is one Python splits lines on, so
    a hook indexing by line would report a different line than it matched."""
    token = "example-ok"
    for boundary in ("\n", "\r\n", "\v", "\f", "\x1c", "\x85", "\u2028"):
        block = f"    # {token}:{boundary}    - run: echo hi\n"
        assert not lc.annotated(block, token), repr(boundary)
    # Non-vacuity: the same block WITH a same-line reason does suppress, so the
    # assertions above fail on the borrowed reason, not on the fixture's shape.
    assert lc.annotated(f"    # {token}: vendored\n    - run: echo hi\n", token)


# ── the WINDOW is SSOT too, not just the matcher ─────────────────────────
# The matcher above answers "is this line annotated". The window answers "which
# lines may carry it", and the pack held ~20 open-coded answers: some accepted
# the line above, some a whole span, some only the flagged line. The same
# annotation was then honoured by one check and rejected by its neighbour, and
# an author could not learn the rule once. `annotation_window` is the one
# answer; reaching for a neighbouring line by hand is how that drifts back.
_HANDROLLED_WINDOW = re.compile(
    r"\bannotated\(\s*[\w.]+\[[^\]]*[-+]\s*\d+\s*\]",
)


def _handrolled_windows(src: str) -> list[int]:
    return [
        lineno
        for lineno, line in enumerate(src.splitlines(), 1)
        if _HANDROLLED_WINDOW.search(line)
    ]


def test_no_hook_hand_rolls_the_annotation_window() -> None:
    offenders = {
        name: lines
        for name, src in _hook_sources().items()
        if (lines := _handrolled_windows(src))
    }
    assert offenders == {}, (
        "these reach for a neighbouring line by hand instead of asking "
        f"_cts_linecheck.annotation_window (via annotated_near): {offenders}"
    )


def test_the_handrolled_window_detector_actually_matches() -> None:
    """Non-vacuity: the detector recognizes the spellings this pack shipped, and
    stays silent on a same-line check, which is not a window at all."""
    assert _handrolled_windows("if annotated(lines[lineno - 2], _ALLOW):") == [1]
    assert _handrolled_windows("start >= 2 and annotated(raw[start - 2], OPT_OUT)") == [
        1
    ]
    assert _handrolled_windows("annotated(physical[lineno - 1], OPT_OUT)") == [1]
    # Sanctioned: a bare line, a loop variable, and the shared helper.
    assert _handrolled_windows("if annotated(line, OPT_OUT):") == []
    assert _handrolled_windows("annotated_near(lines, lineno, OPT_OUT)") == []
