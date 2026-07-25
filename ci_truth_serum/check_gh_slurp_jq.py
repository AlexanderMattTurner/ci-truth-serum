#!/usr/bin/env python3
"""Ban the ``gh api --slurp`` flag combinations that can never succeed.

``gh`` rejects two ``--slurp`` combinations while PARSING ITS ARGUMENTS, before a
single request goes out:

* ``--slurp`` with ``--jq``/``--template`` (either spelling, long or short) —
  "the `--slurp` option is not supported with `--jq` or `--template`".
* ``--slurp`` without ``--paginate`` — "`--paginate` required when passing
  `--slurp`".

Verified against gh 2.86.0 with a token set and ``--hostname 127.0.0.1``: both
shapes print the message above and exit 1, while the valid-flag controls
(``--paginate --slurp``, plain ``--jq``) instead report a dial error against
127.0.0.1 — so the rejection provably precedes the HTTP request.

Because these are hard mutual/required-with exclusions, such a call exits
non-zero on EVERY run: it is not a flaky call, it is a call that has never
worked. That makes it worse than a loud bug — a site that swallows the failure
(``|| true``, a ``2>/dev/null`` capture feeding a ``// empty`` default) reads as
a permanent vacuous green, and one that does not reds a scheduled job 100% of
the time.

The remedy is to capture ``gh api … --paginate --slurp`` output and apply the
filter in a SEPARATE ``jq`` invocation over the captured document — which is
also what the surrounding code usually wants, since ``--slurp`` yields one array
element per page and the filter must flatten (``.[][]``) before it selects.

Scanning: real call sites split a single ``gh api`` across backslash-continued
lines with ``--jq`` on a later line, so a same-line regex would MISS the live
shape; this scans the shared ``logical_lines`` joiner and reports the line the
logical line starts on. Flag matching is confined to the PIPELINE SEGMENT the
``gh api`` token opens: quoted argument values are blanked first, then the
segment is cut at the next ``|``/``;``/``&`` or the ``)`` closing an enclosing
substitution. So the remedy's own downstream filters — a ``| jq -r …``, a
``| column -t`` — are never mistaken for ``gh api``'s flags.

A call that must keep the combination — there is no legitimate one, since gh
refuses it — opts out with a same-line or immediately-preceding-line
``# allow-gh-slurp-jq: <reason>``. The reason is REQUIRED, matching the sibling
shell lints.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MESSAGE_PREFIX,
    annotation_re,
    logical_lines,
    run_line_checks,
)

# `gh api` at a command position: line start, or after whitespace / a shell
# operator / a substitution opener — never inside a longer word, so a retry
# wrapper or an env-var prefix in front is matched all the same.
_GH_API = re.compile(r"(?:^|[\s;|&(!{])gh\s+api\b")

_SLURP = re.compile(r"(?:^|\s)--slurp(?:=|\s|$)")
_PAGINATE = re.compile(r"(?:^|\s)--paginate(?:=|\s|$)")
# gh api's jq/template filters. The long spellings, plus any short-flag cluster
# containing `q` or `t` — cobra lets shorts bundle (`-iq '.x'`), and no other
# `gh api` short flag uses either letter, so the cluster form is unambiguous.
_FILTER = re.compile(r"(?:^|\s)(?:--jq|--template|-(?!-)[a-zA-Z]*[qt])(?:=|\s|$)")

_ALLOW = annotation_re("allow-gh-slurp-jq")


def _mask_quoted(text: str) -> str:
    """TEXT with the CONTENT of every quoted span blanked to spaces (delimiters
    and length preserved), so a `|`/`;`/`&`/`)` inside an argument value cannot be
    read as a command boundary.

    A `$(`/`<(`/backtick substitution re-enters command context even when it is
    nested inside double quotes — the `out="$(retry_stdout gh api …)"` shape every
    capture site uses — so the walk keeps a context stack rather than pairing
    quotes flatly, which would blank the command itself.
    """
    out = list(text)
    stack: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        top = stack[-1] if stack else None
        if top == "'":  # single quotes protect everything up to the next `'`
            if char == "'":
                stack.pop()
            else:
                out[index] = " "
        elif char == "\\" and index + 1 < len(text):
            if top == '"':
                out[index] = out[index + 1] = " "
            index += 2
            continue
        elif text.startswith(("$(", "<("), index):
            stack.append(")")
            index += 2
            continue
        elif char == "`":
            if top == "`":
                stack.pop()
            else:
                stack.append("`")
        elif top == '"':
            if char == '"':
                stack.pop()
            else:
                out[index] = " "
        elif char in "'\"":
            stack.append(char)
        elif char == ")" and top == ")":
            stack.pop()
        index += 1
    return "".join(out)


def _segment(args: str) -> str:
    """ARGS truncated at the end of the `gh api` simple command: the next
    unnested `|`/`;`/`&`, or the `)` that closes an enclosing substitution.

    Paren depth is tracked so a `)` belonging to an argument's own substitution
    (`gh api "$(build_url)" --slurp --jq .`) does not end the command early —
    truncating there would drop the very flags being checked.
    """
    depth = 0
    for index, char in enumerate(args):
        if char == "(":
            depth += 1
        elif char == ")":
            if not depth:
                return args[:index]
            depth -= 1
        elif char in "|;&" and not depth:
            return args[:index]
    return args


def _rejects(logical: str) -> bool:
    """True when any ``gh api`` call in LOGICAL carries a ``--slurp`` combination
    gh refuses at argument validation. Every call on the line is examined, so a
    clean first call does not mask an impossible second one."""
    blanked = _mask_quoted(logical)
    for call in _GH_API.finditer(blanked):
        args = _segment(blanked[call.end() :])
        if not _SLURP.search(args):
            continue
        if _PAGINATE.search(args) and not _FILTER.search(args):
            continue  # the sanctioned `--paginate --slurp` capture
        return True
    return False


def violations(text: str) -> list[int]:
    """1-based line numbers starting a logical line that runs ``gh api`` with a
    ``--slurp`` combination gh rejects outright — ``--slurp`` plus
    ``--jq``/``--template``, or ``--slurp`` without ``--paginate`` — absent a
    reason-bearing ``# allow-gh-slurp-jq:`` annotation."""
    physical = text.splitlines()
    hits: list[int] = []
    for start, logical in logical_lines(text):
        stripped = logical.lstrip()
        if stripped.startswith("#") or MESSAGE_PREFIX.match(stripped):
            continue  # whole-line comment or a printed example, not real code
        if not _rejects(logical):
            continue
        if _ALLOW.search(logical):
            continue
        # The opt-out may also sit on the line immediately above the call.
        if start >= 2 and _ALLOW.search(physical[start - 2]):
            continue
        hits.append(start)
    return hits


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "`gh api --slurp` is rejected at argument validation with `--jq`/"
        '`--template` ("the `--slurp` option is not supported with `--jq` or '
        '`--template`") and requires `--paginate` ("`--paginate` required when '
        'passing `--slurp`"), so this call can NEVER succeed — capture '
        "`gh api … --paginate --slurp` output and apply the filter in a SEPARATE "
        "`jq` invocation, or annotate `# allow-gh-slurp-jq: <reason>`.",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
