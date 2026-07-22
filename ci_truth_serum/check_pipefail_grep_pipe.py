#!/usr/bin/env python3
"""Ban ``producer | grep -q`` under ``set -o pipefail`` — the SIGPIPE false-negative trap.

``grep -q`` exits 0 the instant it sees the FIRST match and closes its stdin. A producer
still writing to the pipe then dies with ``SIGPIPE`` (exit 141), and ``pipefail`` surfaces
that 141 as the pipeline's status — so a genuine MATCH is read as NO-MATCH. It is
load-dependent: it only bites when the producer is still writing after grep exits (output
larger than the ~64 KiB pipe buffer), so it passes every small-input test and fires in
production. The failure is silent and dangerous: a teardown check that verifies a secret
was removed (``secret_store ls | grep -q "$name"``) can report a still-present credential
as gone the moment the listing outgrows the buffer.

The fix is to capture the producer into a variable and feed grep a here-string, so grep's
early exit closes a pipe with no writer behind it::

    out="$(producer)"
    if grep -q PATTERN <<<"$out"; then …

This flags a pipeline that feeds a producer into ``grep`` with a quiet option (``-q`` in
any short-flag cluster, or ``--quiet``/``--silent``) while pipefail is in effect. A
here-string (``grep -q … <<<"$var"``) has no pipe and is NOT flagged — it is the
remediation.

The script is parsed with tree-sitter-bash (the shared ``_bash_ast`` grammar), so what
the lint sees is what bash would run: a pipeline wrapped across physical lines (trailing
``|`` / backslash continuations) is ONE pipeline node, and a ``|`` inside a string,
comment, or heredoc body is data, never a pipe. Pipefail must actually be IN EFFECT
where the pipeline runs: the first ``set -o pipefail`` command must precede the pipeline
in source order — a ``set -o pipefail`` after the pipe (or in dead code below it) does
not protect it, so it does not clear it. A pipeline inside a function body is gated on
pipefail being set anywhere in the file, since the body runs at call time, after a later
``set -o pipefail`` has executed. A sourced bash library (no shebang, declaring
``# shellcheck shell=bash``) inherits its strict-mode callers' pipefail, so it is
treated as pipefail-scoped from its first byte.

A producer that is a bounded shell builtin — ``echo``/``printf``/``:`` emitting an
already-materialized string — is exempt: its single bounded write practically never
outruns the pipe buffer, and flagging every ``echo "$x" | grep -q`` would drown the real
signal (streaming external commands, functions, ``git``/``find``/``docker``). A
genuinely-safe non-builtin producer (output provably tiny/bounded) opts out with a
same-line or immediately-preceding-line ``# pipefail-grep-ok: <reason>``.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import iter_nodes, parse  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import run_line_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position

# pipefail turned ON: `set` then a short-flag cluster ending in `o` whose option-argument
# is `pipefail` (`set -o pipefail`, `set -euo pipefail`, `set -Eeuo pipefail`, `set -eo
# pipefail -o errexit`). `set +o pipefail` (disable) has `+`, not `-`, so it never
# matches. Applied only to real `command` nodes from the bash AST, so a "pipefail"
# mention in a comment or heredoc body can never arm the check.
_PIPEFAIL_ON = re.compile(r"\bset\b\s+-[A-Za-z]*o\b[^;&|]*\bpipefail\b")

# A sourced bash library carries no shebang and declares `# shellcheck shell=bash`. By
# convention such a lib is sourced into strict-mode callers and must NOT re-set shell
# options — so pipefail is active at RUNTIME even though no `set -o pipefail` appears in
# the file. Treating it as pipefail-scoped is what catches the SIGPIPE trap in a sourced
# lib's teardown credential check, which the in-file-only heuristic would miss.
_SHELLCHECK_BASH = re.compile(r"#\s*shellcheck\s+shell=bash\b")

# Producers whose output is a single already-materialized, bounded write — practically
# immune to the SIGPIPE race, so they are not flagged.
_BOUNDED_PRODUCERS = {"echo", "printf", ":"}

_ALLOW = "pipefail-grep-ok"

# The tokens separating pipeline stages in the grammar.
_PIPE_TOKENS = frozenset({"|", "|&"})


def _command_basename(node) -> str | None:
    """The basename of NODE's command word (`/bin/grep` -> `grep`), or None when
    NODE is not a simple command. A `! cmd` pipeline stage parses as a
    `negated_command` wrapping the command — unwrap it so the negation cannot
    hide the command's identity."""
    while node.type == "negated_command" and node.children:
        node = node.children[-1]
    if node.type != "command":
        return None
    for child in node.children:
        if child.type == "command_name":
            return child.text.decode("utf-8", "replace").rsplit("/", 1)[-1]
    return None


def _is_quiet_grep(node) -> bool:
    """True when NODE is a `grep` command carrying a quiet option. Scans the leading
    option words (grep options precede the pattern), stopping at the pattern (the first
    non-`-` argument, or any non-word node such as a quoted string) or an explicit `--`
    terminator."""
    if _command_basename(node) != "grep":
        return False
    seen_name = False
    for child in node.children:
        if child.type == "command_name":
            seen_name = True
            continue
        if not seen_name:
            continue
        if child.type != "word":
            return False  # a quoted/expanded argument is the pattern, not an option
        opt = child.text.decode("utf-8", "replace")
        if opt == "--" or not opt.startswith("-"):
            return False
        if opt in ("--quiet", "--silent"):
            return True
        # A short-flag cluster like -q, -qF, -iq, -nqE: `q` anywhere in a bare
        # `-<letters>` token means quiet. A long `--foo` token is not a short cluster.
        if not opt.startswith("--") and "q" in opt[1:]:
            return True
    return False


def _producer_is_bounded(node) -> bool:
    """True when the pipeline stage feeding grep is a bounded builtin (echo/printf/:)."""
    return _command_basename(node) in _BOUNDED_PRODUCERS


def _in_function(node) -> bool:
    """True when NODE sits inside a function body — executed at call time, not at
    its source position."""
    current = node.parent
    while current is not None:
        if current.type == "function_definition":
            return True
        current = current.parent
    return False


def _pipefail_start(text: str, root) -> int | None:
    """The byte offset from which pipefail is in effect, or None when it never is.

    A sourced bash library (no shebang + `# shellcheck shell=bash`) inherits strict
    mode from its callers, so it is scoped from byte 0. Otherwise the first `set`
    command that enables pipefail marks the start."""
    physical = text.splitlines()
    no_shebang = not (physical and physical[0].startswith("#!"))
    if no_shebang and any(_SHELLCHECK_BASH.search(raw) for raw in physical[:5]):
        return 0
    starts = [
        node.start_byte
        for node in iter_nodes(root, "command")
        if _PIPEFAIL_ON.search(node.text.decode("utf-8", "replace"))
    ]
    return min(starts) if starts else None


def violations(text: str) -> list[int]:
    """1-based line numbers whose pipeline feeds a producer into ``grep -q`` with
    pipefail in effect, without a ``# pipefail-grep-ok:`` annotation. Empty when
    pipefail is never enabled, or enabled only after the pipeline runs."""
    root = parse(text)
    start = _pipefail_start(text, root)
    if start is None:
        return []
    physical = text.splitlines()
    hits: set[int] = set()
    for pipeline in iter_nodes(root, "pipeline"):
        # A pipeline that runs before pipefail is enabled returns grep's own status —
        # no SIGPIPE false-negative is possible there. A function body is the
        # exception: it executes at call time, after a later `set -o pipefail`.
        if pipeline.start_byte < start and not _in_function(pipeline):
            continue
        stages = [c for c in pipeline.children if c.type not in _PIPE_TOKENS]
        for idx in range(1, len(stages)):
            stage = stages[idx]
            if not _is_quiet_grep(stage):
                continue
            if _producer_is_bounded(stages[idx - 1]):
                continue
            lineno = stage.start_point[0] + 1
            raw = physical[lineno - 1] if lineno - 1 < len(physical) else ""
            if _ALLOW in raw:
                continue
            if lineno >= 2 and _ALLOW in physical[lineno - 2]:
                continue
            hits.add(lineno)
    return sorted(hits)


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "`producer | grep -q` under `set -o pipefail`: grep's early exit SIGPIPEs the "
        "still-writing producer, and pipefail surfaces exit 141 so a MATCH reads as "
        'NO-MATCH. Capture first, then here-string: `out="$(producer)"; grep -q PAT '
        '<<<"$out"`, or annotate `# pipefail-grep-ok: <reason>` when the producer output '
        "is provably tiny.",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
