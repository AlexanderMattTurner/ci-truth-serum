#!/usr/bin/env python3
"""Flag "graceful" / "gracefully" in prose and code comments.

The word almost always stands in for a requirement the author never pinned down —
"fails gracefully", "degrades gracefully", "graceful fallback". It reads as a
guarantee while specifying nothing: which inputs, which outputs, which exit code?
A reader (or a reviewer) can't tell whether the behaviour is real or wished-for,
and an LLM writing a PR body reaches for it precisely when it is papering over an
unverified claim. So this errors on the word and tells the author to state the
concrete behaviour instead — "on a read-only cache, pip exits 0 and skips the
write" beats "pip degrades gracefully".

Scanned surfaces:
  - PROSE — Markdown / reStructuredText files (and any file under --prose):
    every line.
  - CODE — everything else: only true comments, located by the language's own
    grammar (see `_comments`). Identifiers and string literals are NOT comments,
    so a `graceful_shutdown()` symbol or a wordlist entry is never flagged; a
    `/* … */` block in a JS suite IS one, every line of it.

Opt out — only when the concrete behaviour is named in the annotation itself — with
`allow-graceful: <what actually happens>` on the flagged line or the line above it
(any comment syntax: `# allow-graceful: ...`, `<!-- allow-graceful: ... -->`). The
annotation is the escape hatch AND the documentation the word was dodging.

Usage:
    check_graceful_handwave.py [--prose] PATH...
`--prose` forces prose mode for every PATH — useful for a free-standing text
document with no telltale extension, e.g. a PR body dumped to a temp file;
otherwise the mode is chosen per file by extension. `.txt` is deliberately not
prose: text files are often data (a wordlist carries the word as a dictionary
entry, not a claim).

Invoked by pre-commit with the staged prose/code files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _comments import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    comment_lines,
    text_comments,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
)

_WORD_RE = re.compile(r"\bgraceful(?:ly)?\b", re.IGNORECASE)
_ALLOW = "allow-graceful"
# A free-standing prose document (--prose) is excused by a reason-bearing
# annotation anywhere, comment syntax or not — a PR body has no comments.
_DOC_ALLOW = re.compile(rf"\b{_ALLOW}:\s*\S")
_PROSE_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".rst"})

MESSAGE = (
    'the word "graceful"/"gracefully" reads as a hand-wave for a requirement '
    "you did not pin down — state the concrete behaviour instead (which input "
    "produces which output / exit code / fallback), or, when the behaviour is "
    "genuinely named, annotate `allow-graceful: <what actually happens>`."
)


def violations(
    text: str, prose: bool, comments: dict[int, str] | None = None
) -> list[int]:
    """1-based line numbers where the word appears un-annotated.

    In PROSE mode every line is scanned; in CODE mode only comment bodies are. A
    line is excused when ``allow-graceful`` appears on it or the line above.

    COMMENTS maps 1-based line -> comment body, from ``comment_lines`` — omitting
    it applies the text delimiter scan to the whole file, which only the caller's
    path can improve on. It is unused in PROSE mode, where every line counts."""
    lines = text.split("\n")
    if comments is None:
        comments = text_comments(text)
    hits: list[int] = []
    for lineno, raw in enumerate(lines, 1):
        target = raw if prose else comments.get(lineno)
        if target is None or not _WORD_RE.search(target):
            continue
        if annotated_near(lines, lineno, _ALLOW):
            continue
        hits.append(lineno)
    return hits


def main(argv: list[str]) -> int:
    force_prose = "--prose" in argv
    paths = [a for a in argv if a != "--prose"]
    status = 0
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # --prose scans a single free-standing document (e.g. a PR title+body),
        # which makes one argument as a whole — there, one reason-bearing
        # `allow-graceful: reason` line anywhere excuses the document (a
        # document ABOUT the word could never satisfy per-line annotation).
        # Files keep per-line annotation: each occurrence owes its own stated
        # behaviour.
        if force_prose and _DOC_ALLOW.search(text):
            continue
        prose = force_prose or Path(path).suffix.lower() in _PROSE_SUFFIXES
        for lineno in violations(text, prose, comment_lines(text, path)):
            print(f"{path}:{lineno}: {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
