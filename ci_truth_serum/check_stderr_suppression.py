#!/usr/bin/env python3
"""Ban stderr suppression (``2>/dev/null``, ``&>/dev/null``, or the canonical
``>/dev/null 2>&1``) on container launch/build commands.

Discarding stderr on a command whose only other failure signal is its exit code
hides the diagnostic and leaves nothing to debug — the bug that motivated this
check was a container launch that swallowed stderr and reported only a bare
non-zero, so the actual cause was unrecoverable. Fires on:

  * ``devcontainer up`` / ``devcontainer build``
  * ``docker compose … up`` / ``docker compose … build`` (and ``docker-compose``)
  * ``docker build`` / ``docker buildx … build``
  * the same launchers invoked through an array variable, e.g.
    ``DC=(docker compose -p foo …)`` then ``"${DC[@]}" up`` — caught by a
    two-pass scan so the indirection can't smuggle a suppressed launch past us.

A launch that legitimately must discard stderr opts out with a same-line
trailing ``# allow-stderr-suppress: <reason>``.

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

_SUPPRESS = re.compile(r"(?:2|&)>\s*/dev/null")
# The canonical `>/dev/null 2>&1` sends stdout to the bit-bucket and then dups
# stderr onto it, so stderr is discarded even though neither token alone is
# `2>/dev/null`. Detect it as the co-occurrence of a stdout->null redirect (`>` or
# `1>`) and a `2>&1` dup on the same command. A lone `2>&1` (stderr merged into a
# live stdout) discards nothing and must NOT match.
_STDOUT_NULL = re.compile(r"(?:^|\s)1?>\s*/dev/null")
_STDERR_DUP = re.compile(r"2>&1")


def _suppresses_stderr(line: str) -> bool:
    """True if LINE discards stderr — a direct `2>`/`&>` to /dev/null, or the
    `>/dev/null 2>&1` pair."""
    if _SUPPRESS.search(line):
        return True
    return bool(_STDERR_DUP.search(line) and _STDOUT_NULL.search(line))


# The up/build verb as a subcommand, not a flag: `(?<![-\w])` rejects `--build`
# (a flag to `docker compose run`, not the `build` subcommand) while still
# matching a space-preceded ` up`/` build`.
_VERB = re.compile(r"(?<![-\w])(?:up|build)\b")

# A launcher named literally on the line, reaching an up/build verb (flags may
# sit between, e.g. `docker compose -f x up`). The compose verb uses the same
# flag-rejecting lookbehind so `docker compose run --build` isn't mistaken for a
# `build` subcommand.
_LITERAL_LAUNCH = re.compile(
    r"\bdevcontainer\s+(?:up|build)\b"
    r"|\bdocker[\s-]compose\s+.*(?<![-\w])(?:up|build)\b"
    r"|\bdocker\s+(?:buildx\s+.*)?build\b"
)

# An array assigned a launcher as its first element: `DC=(docker compose …)`.
_ARRAY_ASSIGN = re.compile(
    r"\b(?P<name>[A-Za-z_]\w*)=\(\s*(?:docker[\s-]compose|devcontainer|docker\s+build)\b"
)


def _array_launch(line: str, arrays: set[str]) -> bool:
    """True if LINE invokes one of ARRAYS (`"${NAME[@]}"`) followed by up/build."""
    for name in arrays:
        m = re.search(r"\$\{" + re.escape(name) + r"\[@\]\}", line)
        if m and _VERB.search(line[m.end() :]):
            return True
    return False


def violations(text: str) -> list[int]:
    """1-based line numbers in TEXT that suppress stderr on a launch/build.
    Scanned per LOGICAL line (continuations joined), so wrapping the launch
    across physical lines cannot split the suppression from the command."""
    logicals = logical_lines(text)
    joined = "\n".join(line for _, line in logicals)
    arrays = set(_ARRAY_ASSIGN.findall(joined))  # collected file-wide (two-pass)
    hits = []
    for lineno, line in logicals:
        stripped = line.lstrip()
        if stripped.startswith("#") or MESSAGE_PREFIX.match(stripped):
            continue  # whole-line comment or a printed example, not real code
        if not _suppresses_stderr(line) or "allow-stderr-suppress" in line:
            continue
        if _LITERAL_LAUNCH.search(line) or _array_launch(line, arrays):
            hits.append(lineno)
    return hits


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "stderr suppressed on a launch/build command — capture and surface it, or "
        "annotate `# allow-stderr-suppress: <reason>`",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
