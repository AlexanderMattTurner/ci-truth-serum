#!/usr/bin/env python3
"""Flag a secret file created world-readable and only ``chmod``'d private AFTERWARD.

A credential/secret file created with the process umask (typically 0644 —
world-readable) and only tightened to 0600/0400 on a LATER line leaves a window
between the create and the ``chmod`` in which a co-tenant on the host can read
the secret. The correct idiom creates the file private from the start —
``(umask 077; …)``, ``install -m 600 …``, an ``O_EXCL`` mode-0600 open, or a
``printf … >file`` run under a standing ``umask 077`` — so no readable window
exists.

The heuristic, kept deliberately narrow so the false-positive rate is ~zero:
  * A CREATE line (after quote-aware comment stripping) writes/creates a file at
    a SECRET-NAMED path — the target's text matches a case-insensitive secret
    keyword (token, secret, cred, key, passwd, password, npmrc, auth, pem,
    refresh, cookie, id_rsa) — via a ``>``/``>>`` redirect, ``touch``, ``tee``,
    or ``install`` WITHOUT a private ``-m 0?[46]00`` mode.
  * The create is UNGUARDED: no standalone ``umask 0?77`` statement precedes it
    in the file, no inline ``umask 0?77`` sits on the create line, and it is not
    an ``install -m 0?[46]00``.
  * A VIOLATION is such an unguarded secret create FOLLOWED, within the next ~3
    non-blank lines (same-or-later lines), by a ``chmod 0?[46]00`` on the SAME
    target path. The later-chmod is the strong signal that the author knew the
    file must be private but created it readable first; requiring the
    create+chmod PAIR on a secret-named path is what keeps this near
    zero-false-positive. A create with no nearby chmod is NOT flagged — an
    unguarded secret create that is never tightened is a different, non-decidable
    class this lint does not attempt.
  * EXEMPT: a create line whose RAW text carries ``# secret-perms-ok: <reason>``
    (reason required).

Invoked by pre-commit with the changed shell files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotation_re,
    logical_lines,
    run_line_checks,
)

# A path token is secret-named when its text contains one of these (case-insensitive).
# Substrings on purpose: `.credentials.json` matches `cred`, `gateway-key.pem`
# matches both `key` and `pem`, `refresh-token` matches `refresh` and `token`.
_SECRET_RE = re.compile(
    r"token|secret|cred|key|passwd|password|npmrc|auth|pem|refresh|cookie|id_rsa",
    re.IGNORECASE,
)

# A standing `umask 0?77` statement on its own line (persists for the rest of the
# file's execution), vs. an inline `umask 0?77` anywhere on the create line (the
# `(umask 077 && … >file)` subshell form).
_STANDALONE_UMASK_RE = re.compile(r"^\s*umask\s+0*77\s*$")
_INLINE_UMASK_RE = re.compile(r"\bumask\s+0*77\b")

# A stdout redirect to a file target: `>file`, `>> file`, `>"$dir/$name"`. The
# lookbehind rejects an fd-numbered / doubled redirect (`2>`, `>>` inner `>`) so a
# `2>/dev/null` is not read as a create; the target char class excludes `&` and `(`
# so `>&2` and `>(cmd)` process substitution never capture a target.
_REDIRECT_RE = re.compile(r"(?<![\d&<>])>>?\s*(?P<target>[\"']?[^\s;|&<>()]+)")

_TOUCH_RE = re.compile(r"(?<![\w./-])touch\b")
_TEE_RE = re.compile(r"(?<![\w./-])tee\b")
_INSTALL_RE = re.compile(r"(?<![\w./-])install\b")

# A chmod tightening a file to owner-only: `chmod 600`, `chmod 0400`, `chmod 0600`.
_CHMOD_RE = re.compile(r"(?<![\w./-])chmod\s+0?[46]00\b(?P<rest>[^;|&]*)")

# `install` mode flags: a private mode makes the create safe; `-d` makes a directory.
_INSTALL_PRIVATE_MODE_RE = re.compile(r"-m\s*0?[46]00\b")
_INSTALL_DIR_RE = re.compile(r"(?:^|\s)-\w*d(?:\w*)?(?:\s|$)")
# Flags that consume the following token as their value (so it is not a path arg).
_VALUE_FLAGS = frozenset({"-m", "--mode", "-o", "--owner", "-g", "--group", "-t"})

_ANNOTATION_RE = annotation_re("secret-perms-ok")

_LOOKAHEAD = 3  # non-blank lines after a create to search for its chmod


def strip_comment(line: str) -> str:
    """Return ``line`` with a trailing ``#``-comment removed, quote-aware.

    A ``#`` starts a comment only at the start of a word — preceded by
    start-of-line or whitespace and not inside a single/double-quoted string. A
    ``#`` glued to a preceding non-space char (``${x#y}``, ``$#``, ``a#b``) or
    sitting inside quotes is literal and kept.
    """
    out: list[str] = []
    quote: str | None = None
    prev = ""
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if quote is not None:
            if quote == '"' and c == "\\" and i + 1 < n:
                out.append(c)
                out.append(line[i + 1])
                prev = line[i + 1]
                i += 2
                continue
            out.append(c)
            if c == quote:
                quote = None
            prev = c
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
            prev = c
            i += 1
            continue
        if c == "#" and (prev == "" or prev.isspace()):
            break
        out.append(c)
        prev = c
        i += 1
    return "".join(out)


def _unquote(tok: str) -> str:
    """Strip one layer of matching surrounding quotes for path comparison."""
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        return tok[1:-1]
    return tok.strip("\"'")


def _command_file_args(stripped: str, cmd_re: re.Pattern[str]) -> list[str]:
    """Non-flag path arguments to the first invocation of ``cmd_re`` on the line,
    up to the next command separator or redirect. ``-m 600``-style value flags
    consume their following token so it is not misread as a path."""
    m = cmd_re.search(stripped)
    if not m:
        return []
    rest = re.split(r"[;|&<>]", stripped[m.end() :], maxsplit=1)[0]
    args: list[str] = []
    tokens = rest.split()
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in _VALUE_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        args.append(tok)
    return args


def _install_targets(stripped: str) -> list[str]:
    """Path args of an ``install`` that is NOT a private create: skip a directory
    install (``-d``) and one that already sets a private ``-m 0?[46]00`` mode."""
    m = _INSTALL_RE.search(stripped)
    if not m:
        return []
    rest = re.split(r"[;|&]", stripped[m.end() :], maxsplit=1)[0]
    if _INSTALL_PRIVATE_MODE_RE.search(rest) or _INSTALL_DIR_RE.search(rest):
        return []
    return _command_file_args(stripped, _INSTALL_RE)


def _secret_create_targets(stripped: str) -> list[str]:
    """Unquoted target paths of every file-CREATION on the line whose path is
    secret-named: ``>``/``>>`` redirects, ``touch``, ``tee``, and non-private
    ``install``. Guarding (umask / private install mode) is decided by the
    caller."""
    raw: list[str] = [m.group("target") for m in _REDIRECT_RE.finditer(stripped)]
    raw += _command_file_args(stripped, _TOUCH_RE)
    raw += _command_file_args(stripped, _TEE_RE)
    raw += _install_targets(stripped)
    targets: list[str] = []
    for tok in raw:
        path = _unquote(tok)
        if _SECRET_RE.search(path):
            targets.append(path)
    return targets


def _chmod_targets(stripped: str) -> list[str]:
    """Unquoted path args of a private ``chmod 0?[46]00`` on the line (empty if none)."""
    m = _CHMOD_RE.search(stripped)
    if not m:
        return []
    return [_unquote(tok) for tok in m.group("rest").split() if not tok.startswith("-")]


def _chmod_follows(stripped_lines: list[str], start: int, target: str) -> bool:
    """True when one of the next ``_LOOKAHEAD`` non-blank lines at/after 0-based
    ``start`` contains a private chmod on ``target``. The create's own line is
    included so a ``printf … >f && chmod 600 f`` one-liner still counts."""
    seen = 0
    for line in stripped_lines[start:]:
        if not line.strip():
            continue
        if target in _chmod_targets(line):
            return True
        seen += 1
        if seen >= _LOOKAHEAD:
            break
    return False


def violations(text: str) -> list[int]:
    """1-based line numbers in TEXT of unguarded secret creates that are tightened
    by a nearby later chmod, without a ``# secret-perms-ok:`` opt-out. Scanned per
    LOGICAL line (continuations joined), so a create wrapped across physical
    lines is analyzed as one command."""
    logicals = logical_lines(text)
    stripped = [strip_comment(ln) for _, ln in logicals]
    hits: list[int] = []
    standing_umask = False
    for idx, (start, raw) in enumerate(logicals):
        line = stripped[idx]
        if _STANDALONE_UMASK_RE.match(line):
            standing_umask = True
        if _ANNOTATION_RE.search(raw):
            continue
        if standing_umask or _INLINE_UMASK_RE.search(line):
            continue
        for target in _secret_create_targets(line):
            if _chmod_follows(stripped, idx, target):
                hits.append(start)
                break
    return hits


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "creates a secret file world-readable and only chmods it private "
        "afterward — a co-tenant can read it in the window between; create it "
        "private from the start (`(umask 077; …)`, `install -m 600 …`, or an "
        "O_EXCL 0600 open), or annotate `# secret-perms-ok: <reason>`",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
