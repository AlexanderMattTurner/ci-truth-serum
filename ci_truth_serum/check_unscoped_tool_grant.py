#!/usr/bin/env python3
"""
Ban a file-tool rule that is not really path-scoped in a Claude Code tool grant.

Two classes, both of which read as a scoped grant and are not one.

BARE TOOL NAME — a rule naming a tool with NO path argument is a whole-tool
grant. The CLI runs the per-path working-directory check FIRST (a path outside
the checkout and outside every `--add-dir` comes back as `ask`, and a headless
`-p` run has no prompt handler, so `ask` is a hard denial), and THEN looks up the
whole-tool allow rule and returns allow regardless of that verdict. Only `deny`
and an explicit `ask` rule survive the override. So a bare rule does not widen
access slightly; it removes the boundary, and an `--add-dir` written beside one is
prompt suppression rather than a jail.

`--allowedTools "Read,Grep,Glob"` therefore reads every path on the runner:
`/proc/self/environ` (every secret the job exported), the runner's artifact JWT,
`$GITHUB_EVENT_PATH`, the agent's own stored credentials. `--allowedTools
"Write,Edit"` is the write half, and it is worse than it looks in a job that runs
sibling shell steps: the model can overwrite a script in the checkout that a
later, credentialed step then EXECUTES, so a token confined to that step's `env:`
is not out of reach of a prompt injection after all.

INERT PATH RULE — the two remedies are NOT symmetric, because the file-permission
check keys path rules under exactly two tool names:

  reads   `Read(./**)`   — that one rule governs Read, Grep and Glob
  writes  `Edit(<path>)` — that one rule governs Edit, Write, MultiEdit and
                           NotebookEdit

A rule written `Write(<path>)`, `Grep(<path>)`, `Glob(<path>)`,
`MultiEdit(<path>)` or `NotebookEdit(<path>)` parses, is accepted, and is then
never consulted, so it scopes nothing — and on the write side it does not even
fail open, it fails SHUT: the grant looks scoped and the tool call is denied. An
absolute path needs TWO leading slashes (`Edit(//tmp/x)`); a single leading slash
anchors the pattern at the CLI's own cwd, so `Edit(/tmp/x)` means `<cwd>/tmp/x`
and matches nothing.

Reads and writes inside the checkout and inside `--add-dir` need no rule at all
when the run is in `acceptEdits` mode, so scoping usually costs nothing.

A deny is not an equivalent remedy: an unparseable or unknown-tool rule in
`--disallowedTools` is discarded silently, so a deny that never took effect is
indistinguishable from one that did.

PROVENANCE. None of the above is documented CLI behaviour; all of it is
internals, established empirically against Claude Code **2.1.220** by driving the
CLI headless against a stub API and reading the permission verdict it recorded
(each rule spelling tested against a matched control that denies), and
corroborated by reading the permission-resolution path in the shipped bundle. A
consumer on a much newer CLI should re-probe before trusting the remedy this
message prescribes; the enumerated tool names are the surface most likely to move.

Opt out of the BARE class with `# allow-unscoped-read-grant: <reason>` or
`# allow-unscoped-write-grant: <reason>` on the grant line or the one above. The
honest reason is a grant that already reaches every path some other way (a bare
`Bash`, which spells the same read or write as `cat` and `>`), or an agent whose
product IS an arbitrary change to the checkout; it should name what would close
it. The INERT class has NO opt-out, deliberately: a rule nothing consults has no
reading under which it is the right thing to write.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    strip_yaml_comments,
    workflow_files as _workflow_files,
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

READ_OPT_OUT = "allow-unscoped-read-grant"
WRITE_OPT_OUT = "allow-unscoped-write-grant"

# Where a tool grant is written: the CLI flag (both spellings the CLI accepts), or
# a workflow `env:` key holding the string a later step passes to it. Both must be
# covered, or moving the list into an env var silently escapes the check.
# `ALLOWED_TOOLS` must not be preceded by a letter, so `DISALLOWED_TOOLS:` is not a
# match — that key holds a DENY list, where a bare tool name is the desired thing
# and "scoping" it would narrow the denial.
_GRANT = re.compile(
    r"--allowed-?[Tt]ools\b|(?<![A-Za-z])ALLOWED_TOOLS\s*:|\ballowed_tools\s*:"
)

# A whole-tool grant: the bare name with no `(...)` path argument after it. Bounded
# by list separators (comma, space, quote, line end) so `Read(./**)` and `ReadFile`
# never match, and by a lookbehind that also rejects a `(` so a tool name appearing
# as the first token INSIDE another rule's parens is not read as a rule of its own.
_BARE_READ = re.compile(r"(?<![\w(])(?P<tool>Read|Grep|Glob)(?![\w(])")
_BARE_WRITE = re.compile(
    r"(?<![\w(])(?P<tool>NotebookEdit|MultiEdit|Write|Edit)(?![\w(])"
)

# A rule carrying a path under a tool name the file-permission check never keys on.
# It reads as a scoping fix and is the second live footgun: nothing consults it, so
# the grant it appears to narrow is either still wide (the other rules decide) or
# shut (no rule matches at all).
_INERT_PATH_RULE = re.compile(
    r"(?<![\w(])(?P<tool>NotebookEdit|MultiEdit|Write|Grep|Glob)\("
)

# Each bare-grant half answers to its OWN slug, so clearing the read half never
# clears the write half.
_BARE_CLASSES = ((_BARE_READ, READ_OPT_OUT), (_BARE_WRITE, WRITE_OPT_OUT))

# The absolute-path footgun, stated once and quoted into both messages: it is the
# same trap whichever class sent the reader here.
_TWO_SLASHES = (
    "an absolute path needs TWO leading slashes (`Edit(//home/runner/work/x)`), "
    "since a single one anchors the pattern at the CLI's own cwd"
)


def _bare_message(tool: str, opt_out: str) -> str:
    """The diagnostic for one bare tool name, naming only ITS half's opt-out slug —
    the other half's would be the wrong annotation to write on this finding."""
    return (
        f"this tool grant names `{tool}` with no path argument. A rule with no "
        "path is a WHOLE-TOOL grant that the CLI applies AFTER the per-path "
        "working-directory check and that OVERRIDES its verdict, so an "
        "`--add-dir` beside it suppresses prompts instead of confining anything: "
        "a bare `Read`/`Grep`/`Glob` reads every path on the runner "
        "(`/proc/self/environ`, the artifact JWT, `$GITHUB_EVENT_PATH`), and a "
        "bare `Write`/`Edit`/`MultiEdit`/`NotebookEdit` overwrites any file — "
        "including a checked-out script a later, credentialed step executes. "
        "Scope reads as `Read(./**)` (that ONE rule governs Read, Grep and Glob) "
        "and writes as `Edit(<path>)` (that ONE rule governs Edit, Write, "
        f"MultiEdit and NotebookEdit); {_TWO_SLASHES}. Otherwise annotate "
        f"`# {opt_out}: <reason>` — the "
        "honest reason is a grant that already reaches every path another way (a "
        "bare `Bash` spells the same read or write as `cat` and `>`), which makes "
        "scoping the file tools beside it theatre; name what would close it."
    )


def _inert_message(tool: str) -> str:
    return (
        f"this tool grant scopes `{tool}(<path>)`, a rule NOTHING CONSULTS: the "
        "CLI's file-permission check keys path rules under exactly two tool names "
        "— `Read` (governing Read, Grep and Glob) and `Edit` (governing Edit, "
        "Write, MultiEdit and NotebookEdit) — so a path rule under any other name "
        "parses, is accepted, and is then never matched. It does not fail open, "
        "it fails SHUT: the grant looks scoped and the tool call is denied. "
        f"Rewrite it as `Read(<path>)` or `Edit(<path>)`; {_TWO_SLASHES}. There "
        "is deliberately NO opt-out for this class — a rule nothing consults has "
        "no reading under which it is the right thing to write."
    )


def _opted_out(source_lines: list[str], index: int, token: str) -> bool:
    """True when the annotation TOKEN (with a reason) annotates the grant.

    Placement is `annotation_window`'s call, not this check's: the grant line
    plus the unbroken comment block above it. One grant can trip BOTH the read
    and the write class, and only one annotation fits the single line directly
    above, so a narrower window leaves such a grant with no way to opt out of
    both at once.

    Reads the ORIGINAL lines, not the comment-stripped scan text: the annotation
    lives in a comment, which is exactly what the scan text has blanked out.
    """
    return annotated_near(source_lines, index + 1, token)


def findings(text: str) -> list[tuple[int, str]]:
    """Every (1-based line, message) for a tool grant on that line that names a
    file tool with no path argument (and carries no matching opt-out annotation),
    or scopes one with a rule the file-permission check never consults.

    Both classes are reported when both fire on one line, so fixing the wider hole
    cannot reveal the other only on a later CI cycle.
    """
    source_lines = text.splitlines()
    # Scanned with YAML comments blanked (offsets preserved) so a grant merely
    # TALKED ABOUT in a comment is not linted as a live grant.
    scan_lines = strip_yaml_comments(text).splitlines()

    out: list[tuple[int, str]] = []
    for index, scanned in enumerate(scan_lines):
        # A `#` opening the line survives comment-blanking only inside a block
        # scalar (a shell comment under `run: |`), where it is still a commented-out
        # invocation rather than one the job runs.
        if scanned.lstrip().startswith("#") or not _GRANT.search(scanned):
            continue
        for bare, token in _BARE_CLASSES:
            hit = bare.search(scanned)
            if hit and not _opted_out(source_lines, index, token):
                out.append((index + 1, _bare_message(hit.group("tool"), token)))
        inert = _INERT_PATH_RULE.search(scanned)
        if inert:
            out.append((index + 1, _inert_message(inert.group("tool"))))
    return out


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every unscoped or inert file-tool rule in the file.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.). The parse is load-bearing and
    not merely a syntax gate: the scan below relies on YAML comment spans to tell
    a live grant from one quoted in a comment, and those spans are exactly what an
    untokenizable file cannot supply.
    """
    text = path.read_text()
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify Claude "
                "Code tool-grant scoping — fix the syntax (or run actionlint) and "
                "re-check.",
            )
        ]
    return findings(text)


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        print(
            "A file-tool rule with no path is a whole-tool grant that overrides "
            "the working-directory check; a path rule under any name but Read or "
            "Edit is never consulted at all."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
