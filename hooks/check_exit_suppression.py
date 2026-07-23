#!/usr/bin/env python3
"""Ban unjustified exit-status suppression (``|| true`` / ``|| :``).

Tacking ``|| true`` onto a command discards its exit status: a real failure
(a teardown that left a volume pinned, a verification that returned non-zero, a
readiness wait that timed out) becomes a silent success. In a security tool that
must *fail loud*, every such suppression should be a conscious, reviewed choice.

This is deliberately NARROW — it does not ban the many legitimate best-effort
idioms, only the cases where an exit code is dropped while the command's output
is kept (so a failure leaves no trace at all). Auto-allowed without annotation:

  * a value capture, where the ``|| true`` sits inside ``$(…)`` / ``<(…)`` /
    backticks — failure yields an empty string the caller already handles;
  * a command that also discards its output (``>/dev/null`` / ``2>/dev/null`` /
    ``&>/dev/null``) in the same simple command — already marked fully
    best-effort, with nothing left to surface.

Everything else — ``some_func || true`` with its output intact — must opt out
with a same-line or immediately-preceding-line ``# allow-exit-suppress: <reason>``
stating why the failure is safe to ignore (e.g. "best-effort GC reaper; the
callee warns internally on a real failure"). The reason is REQUIRED — a bare
``# allow-exit-suppress`` with no colon-and-reason does not suppress, matching the
sibling ``check_substitution_exit_swallow`` / ``check_flag_arity`` contract.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    inside_substitution,
    logical_lines,
    run_line_checks,
)

# The no-op suppressors: `|| true` or `|| :` (with any inter-token spacing). The
# `(?![\w-])` boundary rejects a longer command name (`|| truelove`) while still
# matching a suppressor glued to a following metacharacter (`|| true&`, `|| :;`),
# which a `(?:\s|;|$)` boundary would miss.
_SUPPRESS = re.compile(r"\|\|\s*(?:true|:)(?![\w-])")

# Lines whose first word only prints text — a `|| true` quoted inside them is an
# example or hint, not executed code. This extends the shared MESSAGE_PREFIX
# (_linecheck) with `cg_*`, a common status-message helper naming.
_MESSAGE_PREFIX = re.compile(r"^(?:echo|printf|warn|status|die|log|cg_\w+|:)\b")

# A simple-command boundary: text after the last of these (before the `|| true`)
# is the command whose exit status is being suppressed.
_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\bthen\b|\bdo\b|\{|\(")

_REDIRECT_DEVNULL = re.compile(r"(?:[0-9&]?>|&>)\s*/dev/null")
# An assignment whose whole right-hand side is a command substitution:
# `var=$(cmd) || true` is a value capture (empty var on failure, handled by the
# caller) exactly like `var=$(cmd || true)`, so it carries the same safety.
_ASSIGN_CAPTURE = re.compile(r"""^\s*\w+=["']?(?:\$\(.*\)|<\(.*\)|`.*`)["']?\s*$""")
# The opt-out suppresses only when it carries a non-empty reason after the colon.
# A bare `# allow-exit-suppress` (no colon, or an empty reason) states nothing and
# does not silence the finding — the sibling checks demand the same.
_ALLOW_WITH_REASON = re.compile(r"allow-exit-suppress:\s*\S")


def violations(text: str) -> list[int]:
    """1-based physical line numbers that suppress an exit status without a
    capture, an output redirect, or an `# allow-exit-suppress:` annotation."""
    physical = text.splitlines()
    hits: list[int] = []
    for start, logical in logical_lines(text):
        m = _SUPPRESS.search(logical)
        if not m:
            continue
        stripped = logical.lstrip()
        if stripped.startswith("#") or _MESSAGE_PREFIX.match(stripped):
            continue
        if _ALLOW_WITH_REASON.search(logical):
            continue
        # Annotation may sit on the line immediately above the suppressor.
        if start >= 2 and _ALLOW_WITH_REASON.search(physical[start - 2]):
            continue
        prefix = logical[: m.start()]
        if inside_substitution(prefix) or _ASSIGN_CAPTURE.match(prefix):
            continue  # value capture — empty-on-failure, handled by the caller
        segment = _SEGMENT_SPLIT.split(prefix)[-1]
        if _REDIRECT_DEVNULL.search(segment):
            continue  # output already discarded — nothing left to surface
        hits.append(start)
    return hits


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "exit status suppressed with `|| true` while the command's output is kept "
        "— a real failure would vanish. Discard the output too, capture it, or "
        "annotate `# allow-exit-suppress: <reason>`.",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
