#!/usr/bin/env python3
"""Ban a fail-open idiom: a structured-data producer feeding a shell loop through a
construct that DISCARDS the producer's exit status.

Both ``done < <(PRODUCER …)`` (process substitution) and ``PRODUCER … | while read``
(pipeline) throw away PRODUCER's exit code: the ``while``/``mapfile``/``read`` consumer
reports on the redirect or the pipe, not on the command that filled it. So when the
producer errors — malformed input, a renamed key that makes ``jq`` emit ``null`` and
exit 5, an unreadable file — it prints nothing, the loop iterates ZERO times, and any
guard/allowlist the loop was building silently no-ops while the surrounding function
still returns 0.

Correct pattern — capture then iterate, so the producer's failure is observed:

    out="$(jq -r '.providers[]' "$file")" || die "jq failed"
    while IFS= read -r d; do …; done <<<"$out"

PRODUCER SET (deliberately small): ``jq`` and ``yq`` only. These are structured-data
extractors whose nonzero exit means "your query/data was wrong" — a fail-CLOSED signal
that must not be swallowed. ``grep``/``cat``/``find``/``sed`` are intentionally NOT in
the set: their nonzero exits (grep-no-match, find-permission) are routinely expected and
best-effort, so flagging ``done < <(grep …)`` would be noise. Widen the set only for a
producer whose empty-output-on-error is a genuine fail-open.

A site that must keep the construct opts out with a same-line or immediately-preceding
``# allow-substitution-exit: <reason>`` — the reason is REQUIRED; a bare annotation with
no reason does not suppress.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MESSAGE_PREFIX,
    logical_lines,
    run_line_checks,
)

# The curated producer set (see module docstring for why these two and not grep/cat).
_PRODUCERS = ("jq", "yq")
_PRODUCER_ALT = "|".join(_PRODUCERS)

# `done < <(jq …)` / `mapfile -t x < <(yq …)`: a `<` input redirect, whitespace, then a
# `<(` process substitution whose first command word is a producer. The space between
# the two `<` is required — a `<<` with no gap is a heredoc, not a redirect + proc-sub.
_PROC_SUB = re.compile(rf"<[ \t]+<\(\s*(?:command\s+)?(?:{_PRODUCER_ALT})\b")

# `jq … | while read …`: a producer at a command position (line start after indent, or
# right after a command separator / list operator / group opener / block keyword) that
# pipes DIRECTLY into a `while`. `[^|;&]*` keeps the producer and the `| while` inside one
# simple pipeline segment — a `;`/`&`/`|` between them means some OTHER command feeds the
# loop, not the producer, so `jq …; foo | while` is not flagged.
_PIPE_WHILE = re.compile(
    r"(?:^|[;&|(){}]|\b(?:then|do|else|in)\b)\s*"
    rf"(?:command\s+)?(?:{_PRODUCER_ALT})\b[^|;&]*\|[ \t]*while\b"
)

# The annotation only suppresses when it carries a non-empty reason after the colon.
_ALLOW_WITH_REASON = re.compile(r"allow-substitution-exit:\s*\S")


def _annotation_suppresses(physical: list[str], lineno: int) -> bool:
    """True when a reason-bearing ``# allow-substitution-exit:`` sits on the hit line
    (1-based ``lineno``) or the line immediately above it."""
    if _ALLOW_WITH_REASON.search(physical[lineno - 1]):
        return True
    return lineno >= 2 and bool(_ALLOW_WITH_REASON.search(physical[lineno - 2]))


def violations(text: str) -> list[int]:
    """1-based line numbers in TEXT where a structured-data producer feeds a loop
    through an exit-swallowing construct without a reason-bearing annotation.
    Scanned per LOGICAL line (continuations joined), so a wrapped
    `jq … \\\n  | while read` cannot evade the scan."""
    physical = text.splitlines()
    hits: list[int] = []
    for lineno, raw in logical_lines(text):
        stripped = raw.lstrip()
        if stripped.startswith("#") or MESSAGE_PREFIX.match(stripped):
            continue  # whole-line comment or a printed example, not real code
        if not (_PROC_SUB.search(raw) or _PIPE_WHILE.search(raw)):
            continue
        if _ALLOW_WITH_REASON.search(raw) or _annotation_suppresses(physical, lineno):
            continue
        hits.append(lineno)
    return hits


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "jq/yq exit status is discarded by this construct — capture then iterate "
        '(`out="$(jq …)" || die …; while read; do …; done <<<"$out"`) so the '
        "producer's failure is not silently swallowed, or annotate "
        "`# allow-substitution-exit: <reason>`",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
