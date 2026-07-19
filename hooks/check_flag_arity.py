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

_CASE_RE = re.compile(r"(?P<lead>^|[\s;])case\s+.*?\s+in(?:\s|;|$)")
_IN_RE = re.compile(r"\s+in(?:\s|;|$)")
_ARMEND_RE = re.compile(r";;&|;&|;;")
_ESAC_RE = re.compile(r"(?P<lead>^|[\s;])esac(?:\s|;|$)")

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


def _arm_label(rest: str) -> str | None:
    """The case-arm label from the text before the first ``)``, or None when the
    line does not open an arm."""
    trimmed = rest.strip()
    if not trimmed or trimmed.startswith("("):
        return None
    close = trimmed.find(")")
    if close <= 0:
        return None
    return trimmed[:close].strip()


def _is_flag_label(label: str) -> bool:
    alts = [a.strip() for a in label.split("|")]
    alts = [a for a in alts if a]
    return bool(alts) and all(_FLAG_ALT_RE.match(a) for a in alts)


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for every value-taking flag arm in TEXT that
    consumes ``$2`` / ``shift 2`` without an arity guard. One report per arm."""
    lines = text.split("\n")
    found: list[tuple[int, str]] = []
    # Stack of case frames; each tracks the arm currently being scanned so a
    # nested `case … esac` inside an arm never confuses the outer arm's state.
    stack: list[dict] = []

    def top() -> dict | None:
        return stack[-1] if stack else None

    def consume(raw_frag: str, code: str, line_no: int) -> None:
        """Fold one code fragment into the current arm's guard/consumption state,
        recording a violation for an unguarded read."""
        frame = top()
        if not frame or not frame["arm"] or not frame["arm"]["is_flag"]:
            return
        if frame["arm"]["guarded"]:
            return
        if (
            _has_arity_guard(code)
            or _calls_allowlisted_helper(code)
            or _reads_self_guarded(code)
        ):
            frame["arm"]["guarded"] = True
            return
        if not _reads_bare_positional(code) and not _shifts_past_first(code):
            return

        prev = lines[line_no - 2] if line_no - 2 >= 0 else ""
        marker = _OPTOUT_RE.search(raw_frag) or _OPTOUT_RE.search(prev)
        if marker:
            frame["arm"]["guarded"] = True  # a marker resolves the arm either way
            if not marker.group("reason").strip():
                found.append((line_no, _MSG_EMPTY_OPTOUT))
            return
        found.append((line_no, _MSG_UNGUARDED))
        frame["arm"]["guarded"] = True  # one report per arm is enough

    for i, raw in enumerate(lines):
        line_no = i + 1
        rest = _strip_comment(raw)
        rest_raw = raw
        # Walk the code left-to-right so a label and its inline body on one line
        # (`--flag) x=1 ;;`) are handled in structural order.
        while True:
            frame = top()
            case_m = _CASE_RE.search(rest)
            arm_end_m = _ARMEND_RE.search(rest) if frame and frame["arm"] else None
            esac_m = _ESAC_RE.search(rest)
            label = _arm_label(rest) if frame and not frame["arm"] else None
            label_pos = rest.find(")") if label is not None else -1

            candidates: list[tuple[str, int]] = []
            if case_m:
                candidates.append(("case", case_m.start() + len(case_m.group("lead"))))
            if arm_end_m:
                candidates.append(("armend", arm_end_m.start()))
            if esac_m:
                candidates.append(("esac", esac_m.start() + len(esac_m.group("lead"))))
            if label is not None:
                candidates.append(("label", len(rest) - len(rest.lstrip())))

            if not candidates:
                consume(rest_raw, rest, line_no)
                break
            candidates.sort(key=lambda c: c[1])
            kind, pos = candidates[0]
            # Body text preceding the structural token still belongs to the arm.
            consume(rest_raw, rest[:pos], line_no)

            if kind == "case":
                stack.append({"arm": None})
                after = rest[case_m.start() :]
                in_m = _IN_RE.search(after)
                advance = case_m.start() + in_m.start() + len(in_m.group(0))
                rest_raw = raw[min(advance, len(raw)) :]
                rest = rest[advance:]
                continue
            if kind == "esac":
                if stack:
                    stack.pop()
                rest_raw = raw[min(pos + 4, len(raw)) :]
                rest = rest[pos + 4 :]
                continue
            if kind == "armend":
                frame["arm"] = None
                adv = pos + len(arm_end_m.group(0))
                rest_raw = raw[min(adv, len(raw)) :]
                rest = rest[adv:]
                continue
            # kind == "label": open the arm and continue past `)` for inline body.
            frame["arm"] = {"is_flag": _is_flag_label(label), "guarded": False}
            rest_raw = raw[label_pos + 1 :] if len(raw) > label_pos else ""
            rest = rest[label_pos + 1 :]

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
