#!/usr/bin/env python3
"""Ban unjustified exit-status suppression (``|| true`` / ``|| :``).

Tacking ``|| true`` onto a command discards its exit status: a real failure
(a teardown that left a volume pinned, a verification that returned non-zero, a
readiness wait that timed out) becomes a silent success. In a security tool that
must *fail loud*, every such suppression should be a conscious, reviewed choice.

The script is parsed with the real bash grammar (``_bash_ast``), so what the lint
sees is what bash would run: a suppressor is a ``||`` ``list`` node whose right
operand is the ``true``/``:`` builtin, never a ``|| true`` found in the text. That
is what answers the questions a text scan can only guess at — a ``|| true`` inside
a quoted message (``gb_warn "never write cmd || true"``) is ``string`` content of
the command that prints it and holds no commands at all; one inside a heredoc body
is data; a ``;`` or ``|`` inside quotes separates nothing; a value capture is a
``command_substitution`` ancestor rather than an unbalanced ``$(`` in a prefix; and
``>/dev/null`` counts only as the ``file_redirect`` of the command whose status is
actually suppressed, not as three characters somewhere to its left.

This is deliberately NARROW — it does not ban the many legitimate best-effort
idioms, only the cases where an exit code is dropped while the command's output
is kept (so a failure leaves no trace at all). Auto-allowed without annotation:

  * a value capture, where the ``|| true`` sits inside ``$(…)`` / ``<(…)`` /
    backticks — failure yields an empty string the caller already handles —
    including the ``var=$(cmd) || true`` spelling, whose whole right-hand side is
    that same substitution;
  * a command that also discards its output (``>/dev/null`` / ``2>/dev/null`` /
    ``&>/dev/null``) — already marked fully best-effort, with nothing left to
    surface.

The output-discard allowance has ONE exception: a git command that MUTATES the
worktree, index or history (``merge``, ``commit``, ``add``, ``reset``, …). For
those the "nothing left to surface" reasoning inverts — the stdout is noise, but
the exit status is the only evidence the mutation ran, and the next command reads
the tree it was supposed to produce. A suppressed failure there does not vanish;
it becomes a WRONG ANSWER computed from an unmutated tree. The worked case:
``git merge --no-commit --no-ff "$base" >/dev/null || true`` in a CI checkout
with no committer identity, where git refuses to merge at all, the following
``diff --diff-filter=U`` reports no conflicts, and a resolution-confinement check
built on it accuses every correctly-merged path of being an out-of-scope edit.
Read-only verbs (``git grep``, ``git log``, ``git rev-parse``, ``git fetch``) keep
the allowance — suppressing those really does only drop a diagnostic.

A shell's own ``-c`` argument is a SCRIPT, not a datum, so a quoted body
(``bash -c "cleanup || true"``) is re-parsed and judged by these same rules —
allowances included — with its findings reported on the enclosing file's line.
Only a body whose literal text is certain is read: a single-quoted string, or a
double-quoted one carrying no backslash escape. The shell must also BE the
command word — behind a wrapper (``timeout 5 bash -c '…'``) the same tokens can
equally be a printing command's arguments, which the grammar cannot tell apart.

Everything else — ``some_func || true`` with its output intact — must opt out
with a same-line or immediately-preceding-line ``# allow-exit-suppress: <reason>``
stating why the failure is safe to ignore (e.g. "best-effort GC reaper; the
callee warns internally on a real failure"). The reason is REQUIRED — a bare
``# allow-exit-suppress`` with no colon-and-reason does not suppress, matching the
sibling ``check_substitution_exit_swallow`` / ``check_flag_arity`` contract.

Invoked by pre-commit with the staged shell files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ARGUMENT_TYPES,
    PathologicalInputError,
    command_name,
    iter_nodes,
    node_text,
    parse,
    unquote,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_line_checks,
)

OPT_OUT = "allow-exit-suppress"

MESSAGE = (
    "exit status suppressed with `|| true` while the command's output is kept, or "
    "on a git command that mutates the worktree/index/history — a real failure "
    "would vanish, and a later command would read a tree the mutation never "
    f"produced. Capture the output, or annotate `# {OPT_OUT}: <reason>`."
)

# The no-op suppressors, as command NAMES rather than a text pattern: a `||` right
# operand named exactly `true` or `:` drops the status and does nothing else. Node
# identity is what makes `|| truelove` a different command — no word-boundary
# lookahead needed, and none possible to get wrong.
_NOOP_COMMANDS = frozenset({"true", ":"})

# The operator tokens the grammar puts between a `list`'s / `pipeline`'s branches.
# Filtering them out leaves the operands themselves.
_OPERATORS = frozenset({"||", "&&", "|", "|&", ";", ";;", "&", "\n"})

# A run-time value capture: failure yields an empty string the caller already
# handles, so a `|| true` inside one is not a dropped status. `$(…)` and a backtick
# capture are both `command_substitution`; `<(…)` is `process_substitution`.
_SUBSTITUTIONS = frozenset({"command_substitution", "process_substitution"})

_DEVNULL = "/dev/null"

# The git subcommands that change the worktree, the index or history — the ones a
# LATER command's correctness depends on having run. Read-only verbs are absent on
# purpose (see the module docstring).
_GIT_MUTATION_VERBS = frozenset(
    {
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "am",
        "apply",
        "commit",
        "add",
        "reset",
        "restore",
        "checkout",
        "switch",
        "update-ref",
        "stash",
        "clean",
        "rm",
        "mv",
    }
)
# `git`, or a thin wrapper around it (`git_as_bot -C … merge …`), which suppresses
# the same status for the same cost.
_GIT_COMMAND = re.compile(r"^git(?:_\w+)?$")
# The git global flags whose value is the NEXT token, so the subcommand search does
# not read `user.name=b` (from `git -c user.name=b commit`) as the subcommand.
_GIT_VALUE_FLAGS = frozenset({"-C", "-c"})

# A shell invoked with `-c` runs its argument as a SCRIPT, so a `|| true` written
# there drops an exit status exactly as an inline one does — `bash -c "cleanup ||
# true"` is the same defect one quoting layer down. The body is re-parsed and
# judged by the same rules, which is what keeps the allowances intact too
# (`bash -c "cmd >/dev/null || true"` stays allowed).
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh", "ash"})
# `-c`, or a short cluster ending in it (`sh -ec '…'`) — the value-taking short
# must end its cluster, so the next argument is the script.
_DASH_C = re.compile(r"^-[A-Za-z]*c$")


def _operands(node) -> list:
    """NODE's branches, with the operator tokens between them dropped."""
    return [child for child in node.children if child.type not in _OPERATORS]


def _is_noop(node) -> bool:
    """True when NODE — a ``||``'s right operand — is the no-op ``true``/``:``
    builtin. A trailing pipe (`cmd || true |`, a file ending mid-continuation)
    parses the operand as a pipeline whose first stage is still the suppressor."""
    while node.type == "pipeline" and _operands(node):
        node = _operands(node)[0]
    return command_name(node) in _NOOP_COMMANDS


def _inside_substitution(node) -> bool:
    """True when NODE sits inside a ``$(…)`` / ``<(…)`` / backtick capture."""
    parent = node.parent
    while parent is not None:
        if parent.type in _SUBSTITUTIONS:
            return True
        parent = parent.parent
    return False


def _is_capture_assignment(node) -> bool:
    """True when NODE is an assignment whose WHOLE value is a substitution.

    `var=$(cmd) || true` is the same empty-on-failure capture as
    `var=$(cmd || true)`, so it carries the same safety. A value that merely
    CONTAINS one (`var="pre$(cmd)"`) does not qualify — its failure leaves a
    partial string the caller has no reason to expect."""
    if node.type != "variable_assignment":
        return False
    value = node.children[-1]
    if value.type in _SUBSTITUTIONS:
        return True
    if value.type not in ("string", "raw_string"):
        return False
    content = [child for child in value.children if child.type not in ('"', "'")]
    return len(content) == 1 and content[0].type in _SUBSTITUTIONS


def _suppressed_command(node) -> tuple[object, list]:
    """(the simple command whose exit status NODE reports, its output redirects).

    A `list`/`pipeline` reports its LAST branch's status and a
    `redirected_statement` its body's, so the walk descends to that command while
    collecting the redirects it passes — which are the ones applying to it. Taking
    only the redirects on this path is what keeps `a >/dev/null && b || true`
    flagged: that discard belongs to `a`, whose status nothing is suppressing."""
    redirects: list = []
    while True:
        children = node.children
        operands = _operands(node)
        if node.type in ("list", "pipeline") and operands:
            node = operands[-1]
            continue
        if node.type == "negated_command" and children:
            node = children[-1]
            continue
        # A `command` carries its own redirects (`cmd >/dev/null`, `>/dev/null cmd`);
        # a `redirected_statement` carries them beside its body. Only DIRECT children
        # count: a redirect nested deeper belongs to another command, and a recursive
        # search would read `diff <(a >/dev/null) || true` as discarding `diff`'s
        # output. The one nesting that is still this command's own is a
        # `heredoc_redirect`, which parks the trailing `> file` of `cat <<EOF >f`.
        body = None
        for child in children:
            if child.type == "file_redirect":
                redirects.append(child)
            elif child.type == "heredoc_redirect":
                redirects += [c for c in child.children if c.type == "file_redirect"]
            elif body is None:
                body = child
        if node.type != "redirected_statement" or body is None:
            return node, redirects
        node = body


def _discards_output(redirect) -> bool:
    """True when REDIRECT sends a stream to /dev/null (`>/dev/null`,
    `2>/dev/null`, `&>/dev/null`).

    The destination is the token right after the operator, NOT the node's last
    child: the grammar folds a following argument into the same `file_redirect`
    (`foo >/dev/null "a;b"` parks the string there), so reading the last child
    would miss the discard on exactly the commands that also pass arguments. A `<`
    operator READS, discarding nothing, so the operator must be a `>` form."""
    children = redirect.children
    operator = next(
        (
            index
            for index, child in enumerate(children)
            if ">" in child.type and not child.type.startswith("<")
        ),
        None,
    )
    if operator is None or operator + 1 >= len(children):
        return False
    return unquote(node_text(children[operator + 1])) == _DEVNULL


def _mutates_git(command) -> bool:
    """True when COMMAND is a git invocation whose subcommand changes the worktree,
    index or history — where a suppressed status becomes a wrong answer rather than
    a lost diagnostic."""
    name = command_name(command)
    if name is None or not _GIT_COMMAND.match(name.rsplit("/", 1)[-1]):
        return False
    skip_next = False
    for child in command.children:
        if child.type not in ARGUMENT_TYPES:
            continue
        arg = node_text(child)
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            skip_next = arg in _GIT_VALUE_FLAGS
            continue
        return arg in _GIT_MUTATION_VERBS  # the first positional IS the subcommand
    return False


def _literal_script(node) -> str | None:
    """The exact script a quoted `-c` argument runs, or None when its literal text
    cannot be read off the source with certainty.

    A `raw_string` is literal by definition. A double-quoted `string` is the script
    verbatim only while it carries no backslash escape — with one, the text between
    the quotes is not what bash runs, and a mis-read body would invent a
    suppression that is not there. Every other argument shape (a bare word, a
    `concatenation`, `$'…'`, a lone expansion) is skipped for the same reason."""
    raw = node_text(node)
    if node.type == "raw_string":
        return raw[1:-1]
    if node.type == "string" and "\\" not in raw:
        return raw[1:-1]
    return None


def _shell_scripts(root) -> list[tuple[int, str]]:
    """(0-based row of the body, script) for every `sh -c '<script>'` under ROOT.

    A call inside a substitution is skipped: the capture allowance the caller
    relies on is decided on the OUTER tree, which the re-parse of the body cannot
    see, so judging the body there would report a suppression the outer rules
    exempt."""
    scripts: list[tuple[int, str]] = []
    for command in iter_nodes(root, "command"):
        name = command_name(command)
        if name is None or name.rsplit("/", 1)[-1] not in _SHELLS:
            continue
        if _inside_substitution(command):
            continue
        arguments = [c for c in command.children if c.type in ARGUMENT_TYPES]
        flag = next(
            (i for i, arg in enumerate(arguments) if _DASH_C.match(node_text(arg))),
            None,
        )
        if flag is None:
            continue
        # The script is the argument IMMEDIATELY after `-c`; a later one is
        # `$0`/`$1…` for the body, not the body itself.
        body = arguments[flag + 1] if flag + 1 < len(arguments) else None
        script = None if body is None else _literal_script(body)
        if script is not None:
            scripts.append((body.start_point[0], script))
    return scripts


def _suppression_spans(root, row_offset: int = 0) -> list[tuple[int, int]]:
    """(first line, last line) of every unjustified suppression under ROOT, both
    1-based and shifted by ROW_OFFSET.

    The offset is what lets a `sh -c` body be judged by re-parsing it while the
    findings still land on the enclosing file's physical lines — which is where a
    reader writes the annotation."""
    spans: list[tuple[int, int]] = []
    for node in iter_nodes(root, "list"):
        # A `list` carries exactly one operator between two operands (a chain nests),
        # so this is the `||` whose right operand may be the suppressor.
        if not any(child.type == "||" for child in node.children):
            continue
        operands = _operands(node)
        if len(operands) != 2 or not _is_noop(operands[1]):
            continue
        left = operands[0]
        if _inside_substitution(node) or _is_capture_assignment(left):
            continue  # value capture — empty-on-failure, handled by the caller
        command, redirects = _suppressed_command(left)
        if any(_discards_output(r) for r in redirects) and not _mutates_git(command):
            continue  # output already discarded — nothing left to surface
        spans.append(
            (node.start_point[0] + 1 + row_offset, node.end_point[0] + 1 + row_offset)
        )
    for row, script in _shell_scripts(root):
        spans += _suppression_spans(parse(script), row_offset + row)
    return spans


def violations(text: str) -> list[int]:
    """1-based physical line numbers that suppress an exit status without a
    capture, an output redirect, or an `# allow-exit-suppress:` annotation.

    A suppression spanning physical lines is ONE grammar node, reported at the line
    it starts on. The raw physical lines are kept for the opt-out, which by
    definition lives in a comment (accepted on any physical line of the flagged
    suppression, or the line directly above it)."""
    raw = text.splitlines()
    # One finding per line, carrying the WIDEST span that starts there: two
    # suppressions can begin on the same row (`a || true; b || true`), a line
    # reported twice would be a duplicate finding, and the wider span gives the
    # opt-out lookup every physical line the reader would put it on.
    widest: dict[int, int] = {}
    for start, end in _suppression_spans(parse(text)):
        widest[start] = max(widest.get(start, end), end)
    hits = []
    for start, end in sorted(widest.items()):
        if annotated_near(raw, start, OPT_OUT, span_end=end):
            continue
        hits.append(start)
    return hits


def main(argv: list[str]) -> int:
    """Run the detector over ARGV through the shared read/report loop, one path at a
    time so a file the grammar refuses to parse safely fails LOUDLY (naming the
    path, exit 1 — the same posture as check_untrusted_exec) instead of being
    silently skipped, while every remaining path is still checked."""
    status = 0
    for path in argv:
        try:
            status = max(status, run_line_checks([path], violations, MESSAGE))
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
