#!/usr/bin/env python3
"""Fail a shell script whose CLI flag parser consumes a value without first
proving the value exists.

The bug this guards: a ``case "$1" in`` arm labelled with a value-taking flag
reads ``$2`` / does ``shift 2`` while relying only on the loop's outer
``while [[ $# -gt 0 ]]``. That outer guard proves $1 exists, not $2 — so
``--branch`` passed as the FINAL argument makes ``$2`` unbound and, under
``set -u``, the parser dies with a raw ``$2: unbound variable`` instead of a
clean "--branch needs a value".

A value-consuming flag arm must carry its own arity guard BEFORE the read::

    [[ $# -ge 2 ]] || die "--branch needs a value"   # or -gt 1 / (( $# >= 2 ))
    BRANCH="${2:?--branch needs a value}"            # self-guarding read
    need_val "$@"                                     # an allowlisted helper

Scope is deliberately narrow to keep false positives at zero: only arms whose
LABEL is one or more ``-x`` / ``--xxx`` / ``--xxx=*`` options fire the check.
Subcommand dispatch (``read)``, ``write)``), catch-alls (``*)``), and value
reads inside ordinary function bodies (``local x="$1"; shift 2``) are never
flags, so they are excluded by construction.

Invoked by pre-commit with the changed shell files as arguments; ``--all`` walks
the whole tracked shell surface. Exits non-zero on any violation.
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import iter_nodes, parse  # noqa: E402,I001  # pylint: disable=wrong-import-position

# Helpers that themselves assert `[[ $# -ge 2 ]]` before returning — calling one
# at the top of an arm is an accepted guard. A small named allowlist, not a
# pattern, so a new helper is a deliberate one-line addition here.
ALLOWLISTED_HELPERS = ("need_val", "need_arg")

_OPTOUT_RE = re.compile(r"#\s*flag-arity-ok:(?P<reason>.*)$")

# A case-arm label alternative is a flag when it is a single `-x` / `--xxx`
# option, optionally a `--xxx=*` glob. `doctor)`, `*)`, `read)` and
# quoted/globbed data labels fail this and are skipped.
_FLAG_ALT_RE = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9_-]*(?:=\*)?$")

# A `$#`-vs-number comparison in either polarity: the positive form guarding the
# read (`[[ $# -ge 2 ]] || die`) and the negative bail (`[[ $# -lt 2 ]] && die`).
_ARITY_RE = re.compile(
    r'\$#"?\s*(?P<op>-ge|-gt|-eq|-lt|-le|>=|<=|>|<|==)\s*"?(?P<n>[0-9]+)'
)
# `${2:?…}` / `${2:-…}` / `${2:=…}` / `${2:+…}`: a self-guarding read.
_SELF_GUARD_RE = re.compile(r"\$\{2:[?=+-]")
# A bare positional read past $1 that is NOT a self-guarding `${2:…}` expansion.
_BARE_POS_RE = re.compile(r"\$(?P<d>[2-9])(?![0-9])")
_BRACE_POS_RE = re.compile(r"\$\{(?P<n>[0-9]+)\}")
_SHIFT_RE = re.compile(r"\bshift\s+(?P<n>[0-9]+)\b")

# Case-arm terminators the bash grammar emits as their own child token.
_ARM_TERMINATORS = (";;", ";&", ";;&")

_MSG_UNGUARDED = (
    "value flag consumes $2/shift without an arity guard — "
    "add '[[ $# -ge 2 ]] || die ...' or '${2:?...}'"
)
_MSG_EMPTY_OPTOUT = (
    "flag-arity-ok opt-out needs a non-empty reason (# flag-arity-ok: <why>)"
)


def _strip_comment(line: str) -> str:
    """Strip a trailing ``# comment`` without eating a ``$#`` / ``${#…}``
    parameter: only a ``#`` at line start or preceded by whitespace begins a
    comment."""
    for i, ch in enumerate(line):
        if ch != "#":
            continue
        if i == 0:
            return ""
        prev = line[i - 1]
        if prev in ("$", "{"):  # $# or ${#…}
            continue
        if prev.isspace():
            return line[:i]
    return line


def _has_arity_guard(code: str) -> bool:
    for m in _ARITY_RE.finditer(code):
        op, n = m.group("op"), int(m.group("n"))
        # A read succeeds when >= 2 args remain; each operator implies that at
        # its own threshold (a `< 2` / `-lt 2` bail leaves >= 2 in fall-through).
        if op in ("-ge", ">=", "-eq", "==") and n >= 2:
            return True
        if op in ("-gt", ">") and n >= 1:
            return True
        if op in ("-lt", "<") and n >= 2:
            return True
        if op in ("-le", "<=") and n >= 1:
            return True
    return False


def _calls_allowlisted_helper(code: str) -> bool:
    return any(
        re.search(rf"(?:^|[\s;&|(]){re.escape(h)}(?:\s|$)", code)
        for h in ALLOWLISTED_HELPERS
    )


def _reads_self_guarded(code: str) -> bool:
    return bool(_SELF_GUARD_RE.search(code))


def _reads_bare_positional(code: str) -> bool:
    if _BARE_POS_RE.search(code):
        return True
    return any(int(m.group("n")) >= 2 for m in _BRACE_POS_RE.finditer(code))


def _shifts_past_first(code: str) -> bool:
    return any(int(m.group("n")) >= 2 for m in _SHIFT_RE.finditer(code))


def _is_flag_label(label: str) -> bool:
    alts = [a.strip() for a in label.split("|")]
    alts = [a for a in alts if a]
    return bool(alts) and all(_FLAG_ALT_RE.match(a) for a in alts)


def _arm_label(item) -> str | None:
    """The label of a ``case_item`` node — the source text before its own ``)``
    delimiter (e.g. ``--branch``, ``-f | --file``, ``--privacy=*``), or None when
    the arm has no closing ``)`` (malformed / partial parse)."""
    close = next((c for c in item.children if c.type == ")"), None)
    if close is None:
        return None
    return item.text[: close.start_byte - item.start_byte].decode().strip()


def _arm_body_bytes(item) -> tuple[int, int]:
    """The [start, end) byte span of a ``case_item``'s BODY — after its ``)`` up to
    its terminator (`;;`/`;&`/`;;&`), or the item end when the terminator is
    omitted (a final arm may drop it)."""
    close = next((c for c in item.children if c.type == ")"), None)
    start = close.end_byte if close is not None else item.start_byte
    term = next((c for c in item.children if c.type in _ARM_TERMINATORS), None)
    end = term.start_byte if term is not None else item.end_byte
    return start, end


def _masked_lines(src: bytes, keep_start: int, keep_end: int, holes) -> list[str]:
    """SRC decoded into physical lines with every byte OUTSIDE ``[keep_start,
    keep_end)`` — or inside any ``holes`` span — blanked to a space, while newlines
    are preserved everywhere.

    This isolates one arm's own body: text belonging to other arms, the label, the
    terminator, and any NESTED ``case`` inside this arm (the holes) becomes blank,
    so a `$2` read in a sibling/nested arm is never attributed here. Node byte
    offsets always fall on UTF-8 character boundaries, so a multi-byte character is
    either wholly kept or wholly blanked — the result is always valid UTF-8 with the
    same line count as ``src``."""
    out = bytearray(0x20 if b != 0x0A else 0x0A for b in src)
    for i in range(keep_start, min(keep_end, len(src))):
        if not any(h0 <= i < h1 for h0, h1 in holes):
            out[i] = src[i]
    return out.decode("utf-8").split("\n")


def _scan_arm(item, lines: list[str], src: bytes, found: list[tuple[int, str]]) -> None:
    """Append at most one violation for a single flag-labelled ``case_item``.

    Walks the arm's own body line by line: the FIRST line carrying an arity guard,
    an allowlisted helper, or a self-guarding read resolves the arm as safe; the
    first line that reads ``$2+``/``shift N>=2`` before any such guard is the
    violation (unless a `# flag-arity-ok:` marker on that line or the one above
    opts out — an empty reason is itself reported)."""
    label = _arm_label(item)
    if label is None or not _is_flag_label(label):
        return
    body_start, body_end = _arm_body_bytes(item)
    holes = [(n.start_byte, n.end_byte) for n in iter_nodes(item, "case_statement")]
    masked = _masked_lines(src, body_start, body_end, holes)

    start_row = item.start_point[0]
    end_row = min(len(masked), len(lines)) - 1
    for row in range(start_row, end_row + 1):
        arm_line = masked[row]
        code = _strip_comment(arm_line)
        if (
            _has_arity_guard(code)
            or _calls_allowlisted_helper(code)
            or _reads_self_guarded(code)
        ):
            return  # a guard before any unguarded read resolves the arm as safe
        if not _reads_bare_positional(code) and not _shifts_past_first(code):
            continue
        prev = lines[row - 1] if row - 1 >= 0 else ""
        marker = _OPTOUT_RE.search(arm_line) or _OPTOUT_RE.search(prev)
        if marker:
            if not marker.group("reason").strip():
                found.append((row + 1, _MSG_EMPTY_OPTOUT))
            return  # a marker resolves the arm either way
        found.append((row + 1, _MSG_UNGUARDED))
        return  # one report per arm is enough


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for every value-taking flag arm in TEXT that
    consumes ``$2`` / ``shift 2`` without an arity guard. One report per arm.

    Structure comes from a real bash parse: ``case_item`` nodes (at any nesting
    depth) give exact arm boundaries and labels, so subcommand arms (``read)``),
    catch-alls (``*)``), nested ``case``s, and value reads in ordinary function
    bodies are excluded by construction rather than by an approximate line walker."""
    lines = text.split("\n")
    src = text.encode("utf-8")
    found: list[tuple[int, str]] = []
    for item in sorted(
        iter_nodes(parse(text), "case_item"), key=lambda n: n.start_byte
    ):
        _scan_arm(item, lines, src, found)
    return found


def _shell_files() -> list[str]:
    """Tracked *.sh / *.bash files, plus tracked extensionless files whose
    shebang names bash/sh (git hooks under .hooks/, bin scripts)."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    out: list[str] = []
    for f in tracked:
        if not f:
            continue
        if re.search(r"\.(?:sh|bash)$", f):
            out.append(f)
            continue
        base = f.rsplit("/", 1)[-1]
        if "." in base:
            continue
        try:
            first = Path(f).read_text(encoding="utf-8").split("\n", 1)[0]
        except (OSError, UnicodeDecodeError):
            continue
        if re.match(r"^#!.*\b(?:bash|sh)\b", first):
            out.append(f)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    files = (
        _shell_files()
        if "--all" in argv
        else [a for a in argv if not a.startswith("--")]
    )

    rc = 0
    for path in files:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        for line_no, message in violations(text):
            print(f"{path}:{line_no}: {message}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
