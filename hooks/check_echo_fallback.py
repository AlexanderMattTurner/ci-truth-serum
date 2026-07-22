#!/usr/bin/env python3
"""Ban `|| echo "fallback"` — a failure converted into a parseable string.

`$(cmd || echo "fallback")` turns a non-zero exit into a benign-looking value:
the caller receives a well-formed string, parses it, and proceeds as if the
command had succeeded. Real incidents: a `|| echo "error"` and a
`|| echo "Unable to get diff"` each fed a release-version decision — the
literal fallback text became the input the release logic ranked.

Flagged:

  * `|| echo` / `|| printf` inside a command substitution — the fallback text
    IS the captured value;
  * the bare-statement form `cmd || echo "…"` where the echo is the whole
    recovery — the failure is narrated but not acted on (no exit/return), so
    the script continues as if nothing happened.

NOT flagged (each is a real recovery, not a masking):

  * the fallback output goes to stderr (`>&2`) — diagnostics, not a value;
  * the same logical line also exits/returns after the echo
    (`cmd || { echo "…" >&2; exit 1; }` and friends);
  * message-printing lines (echo/printf/warn/… as the FIRST word) quoting the
    idiom as text.

Opt out with `# echo-fallback-ok: <reason>` on the line or the line above
(e.g. a documented sentinel value the caller explicitly branches on).

Sibling of check-exit-suppression: same file discovery (pre-commit passes the
staged shell files as arguments), same logical-line joining, different vice —
that one drops an exit code, this one replaces the VALUE.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MESSAGE_PREFIX,
    run_line_checks,
)

OPT_OUT = "echo-fallback-ok"

# `|| echo` / `|| printf` — the fallback producer.
_FALLBACK = re.compile(r"\|\|\s*(?:echo|printf)\b")
# End of the fallback's simple command: the next statement separator.
_SEGMENT_END = re.compile(r";|&&|\|\||\)|\}")
# The fallback's output is redirected to stderr — diagnostics, not a value.
_STDERR_REDIRECT = re.compile(r">\s*&\s*2|>&2|1>&2|>>\s*/dev/stderr")
# An abort after the narration: the failure still stops the script.
_ABORTS = re.compile(r"\b(?:exit|return)\b")

_CONTINUES = re.compile(r"(?:\\|\||&&)\s*$")
_SUBST_TOKEN = re.compile(r"\\.|\$\(|<\(|`|\)")


def _inside_substitution(prefix: str) -> bool:
    """True if PREFIX has an unclosed ``$(`` / ``<(`` / backtick."""
    depth = 0
    backtick = False
    for token in _SUBST_TOKEN.finditer(prefix):
        tok = token.group()
        if tok[0] == "\\":
            continue
        if tok in ("$(", "<("):
            depth += 1
        elif tok == ")" and depth:
            depth -= 1
        elif tok == "`":
            backtick = not backtick
    return depth > 0 or backtick


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join continued lines into one logical line, tagged with the 1-based
    physical line number where it starts (a multi-line ``$( … )`` capture is
    analyzed whole)."""
    out: list[tuple[int, str]] = []
    pending = ""
    start = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not pending:
            start = lineno
        joined = raw[:-1] if raw.endswith("\\") else raw
        if _CONTINUES.search(raw) or _inside_substitution(pending + joined):
            pending += joined + " "
            continue
        out.append((start, pending + raw))
        pending = ""
    if pending:
        out.append((start, pending))
    return out


def _fallback_segment(logical: str, start: int) -> str:
    """The fallback's own simple command: text from START (the echo/printf) to
    the next statement separator."""
    end = _SEGMENT_END.search(logical, start)
    return logical[start : end.start()] if end else logical[start:]


def violations(text: str) -> list[int]:
    """1-based line numbers whose `|| echo`/`|| printf` converts a failure into
    a parseable value (no stderr redirect, no abort, no annotation)."""
    physical = text.splitlines()
    hits: list[int] = []
    for start, logical in _logical_lines(text):
        stripped = logical.lstrip()
        if stripped.startswith("#") or MESSAGE_PREFIX.match(stripped):
            continue
        if OPT_OUT in logical or (start >= 2 and OPT_OUT in physical[start - 2]):
            continue
        for m in _FALLBACK.finditer(logical):
            segment = _fallback_segment(logical, m.end())
            if _STDERR_REDIRECT.search(segment):
                continue  # narrated on stderr — never a captured value
            if not _inside_substitution(logical[: m.start()]) and _ABORTS.search(
                logical[m.end() :]
            ):
                continue  # bare form that still aborts — a real recovery
            hits.append(start)
            break
    return hits


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "`|| echo`/`|| printf` converts a failure into a benign parseable string "
        '(a literal `"error"` fed a release-version decision this way). Let the '
        "failure propagate, redirect the message to stderr and abort, or "
        f"annotate `# {OPT_OUT}: <reason>`.",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
