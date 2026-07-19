#!/usr/bin/env python3
"""Fail when documentation cites a repo source file by an exact line number.

Line-number citations rot the instant code moves: `deploy.sh:121-146`, `(L139)`,
`malformed-JSON tolerance (L98-110)`, `~L660` all point at whatever now happens
to live on that line, silently misleading the reader. This check bans them so a
durable pointer is used instead — a function/section/symbol name, a Markdown
anchor, or just the file path with no line number.

Flagged forms:
  - a source path + line: `<path>.<ext>:<N>` / `<path>.<ext>:<N>-<M>` for a real
    source extension (.sh/.bash/.py/.mjs/.js/.ts/.json/.yaml/.yml), skipping any
    match that sits inside an http(s) URL (so `host.com:8080` is never flagged);
  - a prose line-cite: `(L<N>)` / `(L<N>-<M>)` (the leading number needs >= 2
    digits, so a bare `(L4)`-style OSI-layer mention is not flagged), a range
    `L<N>-<M>`, and the approximate forms `~L<N>` / `~:<N>`.

Lines inside fenced code blocks (```) are skipped — a colon-number there is
usually a shell/config example, not a citation.

A file named CHANGELOG.md is always skipped: released changelog entries are an
immutable audit record of what a past change touched, and rewording them to
satisfy a lint would falsify that record.

Escape hatch: a genuinely load-bearing reference is suppressed by an inline
`<!-- allow-line-ref: <reason> -->` comment on the same line or the line directly
above the offending line (the reason is required). Prefer rewording over
escaping — a durable pointer never rots.

Invoked by pre-commit with the staged Markdown files as arguments.
"""

import re
import sys
from pathlib import Path

# A source path immediately followed by :line or :line-range. The extension gate
# is what keeps ports (`localhost:8080`), timestamps (`10:00:00`) and IPs
# (`172.30.0.2`) out — none carries a source extension before the colon.
_FILE_LINE_RE = re.compile(
    r"[\w./-]+\.(?:sh|bash|py|mjs|js|ts|json|ya?ml):\d+(?:-\d+)?"
)
# Prose line-cites. The parenthesized bare form requires >= 2 leading digits so an
# OSI-style `(L4)` is not mistaken for a line reference; a range (`(L2-9)`, `L2-9`)
# and the `~L`/`~:` "approximately line" prefixes are unambiguous at any width.
_PROSE_RES = (
    re.compile(r"\(L\d{2,}(?:-\d+)?\)"),
    re.compile(r"\(L\d-\d+\)"),
    re.compile(r"(?<![\w~L])L\d+-\d+"),
    re.compile(r"~L\d+(?:-\d+)?"),
    re.compile(r"~:\d+"),
)
_URL_RE = re.compile(r"https?://\S+")
# An escape-hatch comment with a non-empty reason.
_ALLOW_RE = re.compile(r"<!--\s*allow-line-ref:\s*\S.*?-->")

MESSAGE = (
    "fragile line-number citation — cite a function/section name or drop the "
    "line number (exact line numbers rot the moment code moves; suppress a "
    "genuinely-needed one with `<!-- allow-line-ref: <reason> -->`)."
)


def _first_offense(line: str) -> str | None:
    """The first flagged reference in LINE, or None. File+line matches inside a
    URL are ignored; the prose forms carry no URL-collision risk."""
    url_spans = [m.span() for m in _URL_RE.finditer(line)]
    for m in _FILE_LINE_RE.finditer(line):
        if not any(s <= m.start() < e for s, e in url_spans):
            return m.group(0)
    for pattern in _PROSE_RES:
        m = pattern.search(line)
        if m:
            return m.group(0)
    return None


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, matched text) for every un-suppressed line-number citation
    outside a fenced code block."""
    lines = text.splitlines()
    hits: list[tuple[int, str]] = []
    in_fence = False
    for lineno, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _first_offense(line)
        if match is None:
            continue
        # Suppressed by an allow-line-ref on this line or the one above it.
        above = lines[lineno - 2] if lineno >= 2 else ""
        if _ALLOW_RE.search(line) or _ALLOW_RE.search(above):
            continue
        hits.append((lineno, match))
    return hits


def main(argv: list[str]) -> int:
    status = 0
    for path in argv:
        if Path(path).name == "CHANGELOG.md":
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, match in violations(text):
            print(f"{path}:{lineno}: `{match}` — {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
