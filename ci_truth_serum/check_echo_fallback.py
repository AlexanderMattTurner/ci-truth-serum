#!/usr/bin/env python3
"""Ban `|| echo "fallback"` — a failure converted into a parseable string.

`$(cmd || echo "fallback")` turns a non-zero exit into a benign-looking value:
the caller receives a well-formed string, parses it, and proceeds as if the
command had succeeded. Real incidents: a `|| echo "error"` and a
`|| echo "Unable to get diff"` each fed a release-version decision — the
literal fallback text became the input the release logic ranked.

The scan runs on the REAL bash grammar (``_bash_ast``): the vice is a ``list``
node whose operator is ``||`` and whose right operand is a ``command`` named
``echo``/``printf``. Asking the grammar instead of the text is what keeps the
lint off everything that merely CONTAINS the idiom — a quoted message
(``string_content``, which holds no commands whatever the printing command is
called), a quoted-delimiter heredoc body (``heredoc_body``, data), a comment
(``comment``) — and what lets a ``\\``-continued fallback be one node rather
than two half-read lines. The grammar is also what tells the two heredoc forms
apart: an UNQUOTED delimiter really expands its body, and the substitution shows
up as a node there, so that fallback is judged like any other.

Flagged:

  * the fallback's value is CAPTURED — the ``list`` sits inside a
    ``command_substitution`` / ``process_substitution`` whose value the script
    then keeps, branches on, runs, or funnels as data: a ``variable_assignment``,
    a ``test_command``, an arithmetic expansion, a command name, a redirect
    (``read v < <(cmd || echo x)``, ``jq . <<< "$(cmd || echo {})"``). The
    fallback text IS that value.
  * the bare-statement form ``cmd || echo "…"`` where the echo is the whole
    recovery — the failure is narrated but not acted on, so the script continues
    as if nothing happened.

NOT flagged (each is a real recovery, or a value this lint cannot judge):

  * the fallback's output is redirected to stderr (``>&2``, ``1>&2``,
    ``/dev/stderr``) — diagnostics, not a value. The redirect is read as a
    ``file_redirect`` applying to the fallback, so a ``>&2`` is never mistaken
    for an argument or missed for sitting on the enclosing statement.
  * the same statement aborts after the echo (``cmd || { echo "…"; exit 1; }``,
    ``cmd || echo "…" && exit 1``, ``cmd || echo "…"; exit 1``). An abort inside
    a substitution does NOT count: it exits only the subshell, and the capture
    still yields the fallback text.
  * the captured value becomes an ARGUMENT of another command
    (``echo "usage: v=$(cmd || echo fallback)"``, ``diff <(cmd || echo x) f``).
    What that program makes of its own argv is its business, and deciding it
    here would mean enumerating every printing command name — the list this lint
    deliberately does not keep, because a project's own logger names are
    unenumerable.

Opt out with `# echo-fallback-ok: <reason>` on any physical line of the flagged
command or the line above (e.g. a documented sentinel value the caller
explicitly branches on).

Sibling of check-exit-suppression: same file discovery (pre-commit passes the
staged shell files as arguments), different vice — that one drops an exit code,
this one replaces the VALUE.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    iter_nodes,
    parse,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
)

OPT_OUT = "echo-fallback-ok"

MESSAGE = (
    "`|| echo`/`|| printf` converts a failure into a benign parseable string "
    '(a literal `"error"` fed a release-version decision this way). Let the '
    "failure propagate, redirect the message to stderr and abort, or "
    f"annotate `# {OPT_OUT}: <reason>`."
)

# The commands whose whole job is to emit their arguments, so a `||` right
# operand naming one produces a VALUE rather than performing a recovery.
_FALLBACK_COMMANDS = frozenset({"echo", "printf"})

# The nodes that capture a command's output as a value: `$(…)`/backticks, and
# `<(…)`, whose value is a path fed to a reader.
_CAPTURES = frozenset({"command_substitution", "process_substitution"})

# Nodes that carry a value up without deciding anything about it, so the
# destination question is answered by what encloses THEM.
_TRANSPARENT = frozenset({"string", "raw_string", "concatenation"})

# Node types that join commands into ONE statement, so an `exit`/`return`
# anywhere further along them aborts this same statement.
_JOINED_STATEMENT = frozenset(
    {"list", "pipeline", "redirected_statement", "negated_command"}
)

# The enclosing nodes a trailing redirect reaches back through to the fallback:
# the statement-joining ones, plus a block or subshell whose own redirect
# (`{ cmd || echo x; } >&2`) redirects everything inside it. A capture is
# deliberately absent — `$(…)` bounds what a redirect outside it can touch.
_REDIRECT_SCOPES = _JOINED_STATEMENT | {"compound_statement", "subshell"}

_ABORTS = frozenset({"exit", "return"})


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _command_name(command) -> str:
    """A `command` node's name (`echo`), or "" when it has none (a bare
    assignment prefix)."""
    for child in command.children:
        if child.type == "command_name":
            return _text(child)
    return ""


def _fallbacks(root):
    """(list node, fallback command) for every `cmd || echo/printf …` under ROOT.

    A chain (`a || b || echo x`) nests one `list` per operator, so the node
    yielded is the innermost one whose own right operand is the printer."""
    for node in iter_nodes(root, "list"):
        for operator, right in zip(node.children, node.children[1:], strict=False):
            if (
                operator.type == "||"
                and right.type == "command"
                and _command_name(right) in _FALLBACK_COMMANDS
            ):
                yield node, right


def _applied_redirects(fallback) -> list:
    """The `file_redirect` nodes that redirect FALLBACK's output.

    A redirect written after the fallback binds to it in bash, but the grammar
    hangs it on the enclosing `redirected_statement` (`cmd || echo x >&2` is one
    such statement wrapping the whole list) — so the enclosing statements are
    walked too, taking only redirects positioned after the fallback ends."""
    redirects = [c for c in fallback.children if c.type == "file_redirect"]
    node = fallback
    while node.parent is not None and node.parent.type in _REDIRECT_SCOPES:
        node = node.parent
        redirects += [
            child
            for child in node.children
            if child.type == "file_redirect" and child.start_byte >= fallback.end_byte
        ]
    return redirects


def _writes_to_stderr(fallback) -> bool:
    """True when FALLBACK's output goes to stderr — narration, never a value.

    Both spellings are read off the redirect's own children: a duplication whose
    target descriptor is 2 (`>&2`, `1>&2`, `>& 2`), or a write to
    `/dev/stderr`."""
    for redirect in _applied_redirects(fallback):
        parts = [_text(child).strip() for child in redirect.children]
        if ">&" in parts and parts[-1] == "2":
            return True
        if "/dev/stderr" in parts:
            return True
    return False


def _enclosing_capture(node):
    """The innermost `$(…)` / `<(…)` NODE sits inside, or None when it stands at
    statement level."""
    while node is not None and node.type not in _CAPTURES:
        node = node.parent
    return node


def _value_becomes_an_argument(capture) -> bool:
    """True when CAPTURE's value ends up in another command's argv, so what is
    made of the text belongs to that program.

    Everywhere else the script itself takes the value — an assignment keeps it, a
    test branches on it, a command name runs it, a redirect funnels it as data —
    and a fake value there is the vice this lint exists for."""
    node = capture
    while node.parent is not None and node.parent.type in _TRANSPARENT:
        node = node.parent
    return node.parent is not None and node.parent.type == "command"


def _aborts_after(fallback) -> bool:
    """True when an `exit`/`return` follows FALLBACK closely enough to be its
    recovery: anywhere further along the same statement (`&&`, `|`, a wrapping
    redirect), or in a following statement on the same physical line
    (`cmd || echo x; exit 1`)."""
    line = fallback.start_point[0]
    node = fallback
    while node.parent is not None:
        parent = node.parent
        one_statement = parent.type in _JOINED_STATEMENT
        for sibling in parent.children:
            if sibling.start_byte < node.end_byte:
                continue
            if not one_statement and sibling.start_point[0] != line:
                continue
            for command in iter_nodes(sibling, "command"):
                if _command_name(command) in _ABORTS and (
                    one_statement or command.start_point[0] == line
                ):
                    return True
        node = parent
    return False


def violations(text: str) -> list[int]:
    """1-based line numbers whose `|| echo`/`|| printf` converts a failure into
    a parseable value (no stderr redirect, no abort, no annotation).

    A fallback is reported at the first line of its own `||` list, so a
    `\\`-continued or multi-line construct is one finding at the line it starts
    on. The raw physical lines are kept for the `# echo-fallback-ok:` opt-out,
    which by definition lives in a comment (accepted on any physical line of the
    flagged construct, or the line directly above it)."""
    raw = text.splitlines()
    # One finding per line, carrying the WIDEST span that starts there: two
    # fallbacks can begin on the same row, and a line reported twice would be a
    # duplicate finding — plus the wider span gives the opt-out lookup every
    # physical line a reader would put the annotation on.
    widest: dict[int, int] = {}
    for node, fallback in _fallbacks(parse(text)):
        if _writes_to_stderr(fallback):
            continue
        capture = _enclosing_capture(node)
        if capture is None and _aborts_after(fallback):
            continue
        if capture is not None and _value_becomes_an_argument(capture):
            continue
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        widest[start] = max(widest.get(start, end), end)

    hits = []
    for start, end in sorted(widest.items()):
        if any(annotated(line, OPT_OUT) for line in raw[start - 1 : end]) or (
            start >= 2 and annotated(raw[start - 2], OPT_OUT)
        ):
            continue
        hits.append(start)
    return hits


def main(argv: list[str]) -> int:
    status = 0
    for path in argv:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        try:
            hits = violations(text)
        except PathologicalInputError as err:
            # A shape the grammar cannot parse safely fails the check LOUDLY
            # (the same posture as check_untrusted_exec): skipping it would
            # false-green exactly the input an adversary controls.
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
            continue
        for lineno in hits:
            print(f"{path}:{lineno}: {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
