"""Meta-contract: annotation/opt-out tokens are matched by the SHARED matcher.

The bug class this pins closed: a hook testing ``"some-token-ok" in line`` (a
bare substring) honors the token ANYWHERE in the byte stream — inside a
``group: "<token>"`` string value, a printed message, a URL — so live data can
silently disable the lint (a fail-open). The shared ``_linecheck.annotation_re``
/ ``annotated`` matcher scopes the token to a real comment (and, where the
hook's contract demands it, requires a ``: <reason>``); every hook that
recognizes a per-line annotation must build its predicate there.

``opted_out`` (the concurrency lints' whole-file comment scan) is the one other
sanctioned matcher — it already comment-scopes by splitting on ``#``.
"""

import re

from tests._helpers import HOOKS_DIR, load_hook

lc = load_hook("_linecheck.py", "lc_for_annotation_contract")

# The bare-substring predicate this contract bans: an annotation-shaped string
# literal (or the conventional ALLOW/OPT_OUT constant naming one) used with a
# bare `in` containment test.
_BARE_TOKEN_LITERAL = re.compile(
    r"""["'](?:allow-[\w-]+|[\w-]+-(?:ok|exempt))["']\s+in\s+"""
)
_BARE_TOKEN_CONSTANT = re.compile(r"\b(?:_ALLOW|ALLOW|OPT_OUT)\s+in\s+")


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
