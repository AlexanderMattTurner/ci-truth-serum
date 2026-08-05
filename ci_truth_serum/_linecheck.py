"""Shared machinery for the line-oriented pre-commit lints under this directory.

The ``check_{stderr_suppression,pinned_base_images}`` scripts each scan a list of
paths given on argv, read
each file as UTF-8 (skipping anything unreadable), run a per-script detector over
the text, and print ``<path>:<lineno>: <message>`` to stderr for every hit —
returning 1 if any fired. Only the detector and the message differ; the read
loop, the skip-on-OSError/UnicodeDecodeError, the print loop, and the exit code
are identical, and live here. A lint that parses the bash grammar owns its own
read loop instead, because ``run_line_checks`` cannot surface
``PathologicalInputError`` as a per-path failure.

The workflow lints (``check_pr_paths``, ``check_workflow_pipefail``,
``check_inline_run_length``, ``check_always_reporter``) share a byte-identical
``workflow_files()`` discovery glob; it lives here too. The two
required-check-shape probes (``has_decide_gate``, ``has_always_reporter``) are
shared by ``check_always_reporter`` and ``check_concurrency`` and live here too.

Imported as a sibling: the scripts run as ``python3 ci_truth_serum/check_*.py`` (or
``python -m ci_truth_serum.check_*``), so each script prepends its own dir to ``sys.path``
before importing this module; the tests load each script by path.
"""

import itertools
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import yaml

# Lines whose first word only prints text — a command quoted inside them is an
# example or hint, not executed code. Shared by the stderr- and download-pinning
# checks; check_exit_suppression extends it (it also excuses status helpers).
MESSAGE_PREFIX = re.compile(r"^(?:echo|printf|warn|status|die|log|:)\b")


class LineLoader(yaml.SafeLoader):
    """SafeLoader that tags every mapping with `__line__` (the 1-based source line
    of its first key) so a flagged step can be reported with a navigable
    file/line annotation instead of a bare, unclickable `::error::`. Shared by the
    workflow lints that want line-anchored findings (check_inline_run_length,
    check_externalized_markers)."""


def _mapping_with_line(loader: LineLoader, node: yaml.MappingNode) -> dict:
    mapping = loader.construct_mapping(node, deep=True)
    mapping["__line__"] = node.start_mark.line + 1
    return mapping


LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_with_line
)

# The two extensions a GitHub workflow file may carry. One SSOT so the reporter
# lint's discovery (`workflow_files`) and the apply step's (`desired_contexts`)
# can never diverge on which files they read.
WORKFLOW_GLOBS = ("*.yaml", "*.yml")

# `# required-check: true` on a job key line or one of its direct-child lines —
# the SSOT marker both check_required_reporter (which *requires* every always()
# reporter to be classified) and the sync_required_checks apply step (which reads
# the marker from ANY job) consume from the same scoped lines.
REQUIRED_MARKER = re.compile(r"#\s*required-check\s*:\s*true\b")
# A `${{ matrix.KEY }}` reference inside a job `name:`.
MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.(?P<key>[A-Za-z_][\w-]*)\s*\}\}")

# A whole-value `${{ … }}` expression wrapper around a job `if:`. GitHub evaluates
# `if: always()` and `if: ${{ always() }}` identically, so the reporter probe must
# see through the wrapper. Only a wrapper spanning the ENTIRE value is stripped — a
# compound like `always() && cond` (wrapped or not) is left intact so it stays a
# non-reporter (it does not unconditionally run).
_IF_WRAPPER = re.compile(r"^\$\{\{\s*(?P<inner>.*?)\s*\}\}$", re.DOTALL)


def unwrap_expression(value: object) -> str:
    """A workflow `if:` value as the bare expression GitHub evaluates: a whole-
    value `${{ … }}` wrapper is stripped (the two forms are identical to GitHub),
    a partial one is left intact so a compound stays compound."""
    text = str(value if value is not None else "").strip()
    wrapped = _IF_WRAPPER.match(text)
    return wrapped.group("inner").strip() if wrapped else text


# A comment introducer: `#` (shell/YAML/Python), `<!--` (Markdown/HTML), or `//`
# (JS/TS). An annotation token counts only AFTER one of these on its line, so a
# token smuggled into live data (a `group: "<token>"` string value, a printed
# message, a URL fragment) can never silently disable a lint — that would be a
# fail-open. One SSOT for every annotation-matching hook in this package.
_COMMENT_INTRO = r"(?:#|<!--|//)"
# Every character Python treats as a line boundary, as a regex class body. The
# reason-required tail must not cross one: a plain `\s*` there would let the gap
# after `<token>:` swallow the newline and accept the NEXT line's first character
# as the "reason", so a bare `# <token>:` at the end of a line would suppress the
# lint — a fail-open on every hook whose opt-out scan runs over multi-line text
# (a job block, a whole file) rather than one line at a time.
_LINE_BOUNDARY_CLASS = r"\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


# The characters an annotation token may not be glued to on either side. `\b`
# will not do: nearly every token in this package is hyphenated, and `\b` matches
# at a word/`-` transition, so a `\b` HEAD is satisfied by any longer slug that
# merely ENDS with the token (`# really-allow-unbounded:` suppressing
# `allow-unbounded`) and a `\b` TAIL by any longer slug that merely STARTS with
# it (`# pin-comment-ok-ish` suppressing `pin-comment-ok`). Either way one hook's
# opt-out silently disarms another's — a fail-open. The guarantee is stated on
# BOTH ends so it is symmetric: only the characters a token is spelled from
# (`[\w-]`) disqualify a neighbour, so `# nope.allow-x:` — where the `.` cannot
# be part of any token — still reads as the annotation it looks like.
_TOKEN_EDGE = r"[\w-]"


def annotation_re(token: str, require_reason: bool = True) -> "re.Pattern[str]":
    """The compiled matcher for an opt-out/annotation TOKEN on one line.

    Comment-scoped: the token must follow a comment introducer. With
    REQUIRE_REASON (the default), the token must also carry `: <non-empty
    reason>` ON THE SAME LINE — a bare marker states nothing and does not
    suppress. Either way the token must stand ALONE: a longer slug that merely
    contains it, at either end, is a DIFFERENT annotation and never satisfies
    this one. Every hook that recognizes a per-line annotation builds its matcher
    here; the meta-test in tests/cts/test_annotation_predicates.py bans the bare
    `token in line` substring predicate this replaces."""
    same_line = f"[^{_LINE_BOUNDARY_CLASS}]"
    # With a reason required, the literal `:` already supplies the right edge (it
    # is not a `[\w-]` character); without one, a lookahead has to supply it.
    tail = rf":{same_line}*?[^\s]" if require_reason else rf"(?!{_TOKEN_EDGE})"
    # The left edge, spelled as an alternation rather than a lookbehind, because
    # `<!--` and `//` END in characters a lookbehind would reject: the token may
    # either follow the comment introducer directly (`<!--allow-x:`) or follow
    # same-line text whose last character is not part of a token.
    lead = rf"(?:{same_line}*[^{_LINE_BOUNDARY_CLASS}\w-])?"
    return re.compile(rf"{_COMMENT_INTRO}{lead}{re.escape(token)}{tail}")


def annotated(line: str, token: str, require_reason: bool = True) -> bool:
    """True when LINE carries the comment-scoped annotation TOKEN (see
    ``annotation_re``)."""
    return bool(annotation_re(token, require_reason).search(line))


# A line whose first non-blank character opens a comment in one of the languages
# this pack reads. Used only to walk the run of comment lines attached to a
# construct — never to decide whether a `#` inside code is a comment, which is
# the structural question `_comments` answers with each language's own grammar.
_COMMENT_ONLY = re.compile(r"^[ \t]*(?:#|//|/\*|\*(?!/)|<!--)")


def comment_block_above(lines: list[str], lineno: int) -> range:
    """The 1-based line numbers of the unbroken run of comment-only lines that
    sits directly above 1-based LINENO.

    A construct's reason is written as prose, and prose wraps. Stopping at the
    single line above would accept a one-line reason and reject the same reason
    split over two, so an author who explains WHY at any length is told to
    reformat rather than to explain. The run stops at the first line that is not
    a comment, so the block belongs to this construct and not to the one before
    it. A blank line ends the run too: a comment separated by whitespace reads
    as narration about the section, not about this line.
    """
    start = lineno
    while start >= 2 and _COMMENT_ONLY.match(lines[start - 2]):
        start -= 1
    return range(start, lineno)


def annotation_window(
    lines: list[str], start: int, end: int | None = None
) -> list[int]:
    """The 1-based lines that may carry the opt-out for a construct spanning
    START..END (1-based, inclusive; END defaults to START).

    **This is the pack's ONE answer to "where may the reason go".** Every check
    asks it, so an author learns the rule once instead of per hook. What a check
    still owns is its SPAN, which is genuinely per-language and cannot be
    shared: a shell logical line, a Python call node, a single comment line. It
    passes that span here and this decides the rest.

    The window is the span itself plus the unbroken comment block directly above
    it. A reason is prose, prose wraps, and an author who explains WHY over two
    lines means the same thing as one who fits it on one. Before this was one
    definition, the pack held roughly twenty open-coded ones — some accepting
    the line above, some the whole span, some only the flagged line — so the
    same annotation was honoured by one check and rejected by its neighbour.

    Widening is bounded, not a fail-open: the token must still sit in a real
    comment and still carry a reason (``annotation_re``), and the block stops at
    the first line that is not a comment. A blank line therefore ends it, so an
    annotation written about something else cannot drift down onto a later
    construct.
    """
    end = start if end is None else end
    span = range(start, min(end, len(lines)) + 1)
    # The line DIRECTLY above always counts, whether or not it is comment-only.
    # A trailing comment on a code line is where a reason goes when the construct
    # opens on the line before it (`if  # pin-exempt: …` above a `curl`), and
    # `comment_block_above` stops at that line because it carries code.
    direct = [start - 1] if start >= 2 else []
    candidates = {*comment_block_above(lines, start), *direct, *span}
    return sorted(n for n in candidates if 1 <= n <= len(lines))


def annotated_near(
    lines: list[str],
    lineno: int,
    token: str,
    require_reason: bool = True,
    span_end: int | None = None,
) -> bool:
    """True when TOKEN annotates the construct at 1-based LINENO (see
    ``annotation_window``, which owns the placement rule)."""
    return any(
        annotated(lines[n - 1], token, require_reason)
        for n in annotation_window(lines, lineno, span_end)
    )


# A line that ends in a backslash, a pipe, or a boolean operator is continued on
# the next line by the shell — join them so a command (and its `$(…)` / redirects)
# spanning lines is analyzed whole, not mis-split mid-capture.
_CONTINUES = re.compile(r"(?:\\|\||&&)\s*$")

# The only tokens that affect substitution nesting: an escaped char (`\x`, inert —
# so `\`` is a literal backtick and `\$` never opens `$(`), an opening `$(` / `<(`,
# a closing `)`, or a bare backtick. Walking these instead of indexing characters
# keeps `inside_substitution` a plain fold with no manual offset bookkeeping.
_SUBST_TOKEN = re.compile(r"\\.|\$\(|<\(|`|\)")


def inside_substitution(prefix: str) -> bool:
    """True if PREFIX has an unclosed ``$(`` / ``<(`` / backtick — i.e. text after
    it is still inside a command substitution (so the line continues, or a
    ``|| true`` after it is a value capture)."""
    depth = 0
    backtick = False
    for token in _SUBST_TOKEN.finditer(prefix):
        tok = token.group()
        if tok[0] == "\\":
            continue  # escaped character — inert
        if tok in ("$(", "<("):
            depth += 1
        elif tok == ")" and depth:
            depth -= 1
        elif tok == "`":
            backtick = not backtick
    return depth > 0 or backtick


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Join continued lines into one logical line, tagged with the 1-based
    physical line number where it STARTS.

    A line continues when it ends in ``\\`` / ``|`` / ``&&`` (shell line
    continuation) OR when a command substitution it opened (``$(`` / ``<(`` /
    backtick) is still unclosed. This is the ONE joiner every line-oriented shell
    lint in this package scans through, so a construct wrapped across physical
    lines cannot evade any of them; the meta-test in
    tests/cts/test_shell_hook_traversal.py holds each shell lint to it (or to the
    full ``_bash_ast`` grammar)."""
    out: list[tuple[int, str]] = []
    pending = ""
    start = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not pending:
            start = lineno
        joined = raw[:-1] if raw.endswith("\\") else raw
        if _CONTINUES.search(raw) or inside_substitution(pending + joined):
            pending += joined + " "
            continue
        out.append((start, pending + raw))
        pending = ""
    if pending:
        out.append((start, pending))
    return out


def comment_body(line: str) -> str | None:
    """The comment text of LINE, or None when LINE carries no comment.

    A full-line ``#`` / ``//`` / ``/*`` / ``*`` comment returns the whole
    stripped line; a trailing ``code  # ...`` / ``code  // ...`` comment returns
    the text from the delimiter on. A bare ``#`` / ``//`` inside code
    (``${#arr}``, ``https://``) is not a comment delimiter — the trailing form
    requires the surrounding whitespace a real inline comment has.

    Shared by every lint that reads narration rather than code: a string literal
    is a value the program builds, not a claim about the tree, so scanning only
    comment bodies is what keeps those lints off test fixtures and user copy.
    """
    stripped = line.lstrip()
    if stripped.startswith(("#", "//", "/*", "*")):
        return stripped
    starts = [i for i in (line.find(" # "), line.find(" // ")) if i != -1]
    return line[min(starts) + 1 :] if starts else None


def run_file_cli(check_main: Callable[[list[str]], int]) -> int:
    """The `__main__` body of every check that reads only the files on argv.

    PROBLEM CLASS — a check that scans a list of paths reports a clean pass when
    it got no paths. Its loop runs zero times and it returns 0, which is the
    exit code of a real pass, so a caller that forgot the file list reads it as
    "this repository is clean". That false green is what this pack refuses.

    An empty argv here is never a legitimate run: a content check has nothing to
    scan without files, unlike a workflow check, which finds its own. So this
    refuses, with exit code 2 to separate a usage error from the 1 that means
    violations found, and names the command that scans the whole tree.
    """
    argv = sys.argv[1:]
    if not argv:
        module = Path(sys.argv[0]).stem
        print(
            f"{module}: no files to scan. This check reads only the paths you "
            "give it, so an empty run would report a clean pass over nothing.",
            file=sys.stderr,
        )
        print(
            f"  to scan the whole tree: git ls-files -z | xargs -0 python -m "
            f"ci_truth_serum.{module}",
            file=sys.stderr,
        )
        return 2
    return check_main(argv)


def run_line_checks(
    argv: list[str],
    find_violations: Callable[[str], list[int]],
    message: str,
) -> int:
    """``run_source_checks`` for a detector that needs only the text. Most
    line-oriented lints do; a lint that must pick a parser (see ``_comments``)
    needs the path and calls the two-argument form directly."""
    return run_source_checks(argv, lambda text, _path: find_violations(text), message)


def unparseable_shell_reason(path: str, text: str) -> str | None:
    """The refusal message when PATH names shell the grammar cannot read, else None.

    The bash grammar is imported HERE rather than at module scope, and only once
    the path is known to be shell. Around fifty checks import this module, and
    most of them read YAML or Python and are handed no shell at all; a module-
    scope import would put `tree_sitter_bash` in every one of their pre-commit
    environments. A check that IS handed shell still gets the loud ImportError
    `_bash_ast` raises, so the deferral costs no fail-closed behaviour.
    """
    if not is_shell_source(path, text.split("\n", 1)[0]):
        return None
    # This module is loaded BY PATH (the check scripts, and tests/_helpers.load_hook),
    # so a sibling import needs this directory on the path first — the same prelude
    # every check script carries.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bash_ast import (  # pylint: disable=import-outside-toplevel
        UnparseableShellError,
        assert_parseable,
    )

    try:
        assert_parseable(text)
    except UnparseableShellError as err:
        return str(err)
    return None


def run_source_checks(
    argv: list[str],
    find_violations: Callable[[str, str], list[int]],
    message: str,
) -> int:
    """Drive a line-oriented lint over ARGV.

    For each readable path, FIND_VIOLATIONS(text, path) returns the 1-based line
    numbers that violate. Each hit prints ``<path>:<lineno>: <message>`` to stderr; an
    unreadable path (OSError / UnicodeDecodeError) is skipped. Returns 1 if any
    path produced a hit, else 0.

    This skip is a deliberate, narrow recovery action, not a silent-pass-on-bad-
    input escape hatch: ARGV here is pre-commit's own file list, already filtered
    to committed files of the right type (shell/python/Dockerfile) via ``identify``
    before this ever runs, so a read failure means the path vanished (a rename/
    delete race) or was mis-tagged as text (stray binary bytes) — not that this
    lint is blessing bad shell/Python/Dockerfile content as clean. That's the
    opposite of the YAML workflow lints (``check_workflow_pipefail`` &c.), whose
    one argument *is* the exact artifact under test: an unparseable workflow
    there is reported as a violation, since "no findings" would be a false-green
    on the very file being verified.

    A shell file the GRAMMAR cannot read gets that same treatment, and for the
    same reason: `unparseable_shell_reason` says the detector never saw the
    constructs it matches on, so its empty result is not a pass. Reporting it
    here rather than raising keeps one unparsed line from failing a whole
    pre-commit run with a traceback, while still refusing to call the file clean.
    """
    status = 0
    for path in argv:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        reason = unparseable_shell_reason(path, text)
        if reason is not None:
            print(f"{path}: {reason}", file=sys.stderr)
            status = 1
            continue
        for lineno in find_violations(text, path):
            print(f"{path}:{lineno}: {message}", file=sys.stderr)
            status = 1
    return status


# A path that names a test file: a tests/ (or __tests__/, specs/) directory
# component, a `test_*` or `conftest` module, or a `test.*` / `spec.*` /
# `*.test.*` / `*.spec.*` suite. One definition, so a lint that scopes itself to
# (or away from) tests classifies a path the same way as every other.
#
# Each alternative must accept the SHORTEST member of its class, not just the
# long ones. The two hand-rolled peers this replaced both demanded a separator
# after `test`, so `test.py`, `x/test.py` and `conftest.py` were all False — and
# a `skipif` hiding in `conftest.py`, where collection-time skips most naturally
# live, escaped check_toolchain_skips entirely. A scope filter's recall bug is
# invisible: it yields a green vacuous pass, never an error. Hence the basename
# alternative takes `^` or any of `/._-` as its left boundary; that boundary
# stays mandatory, so `latest.py`, `protest.mjs` and `spectrum.ts` stay out.
_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?)/"
    r"|(?:^|/)test_[^/]*$"
    r"|(?:^|[/._-])(?:conftest|test|spec)s?\.[^./]+$"
)
# A tracked file whose NAME says it is shell. An extensionless file is shell only
# if its shebang says so, which needs a read — hence the two-step in
# `tracked_shell_files`.
_SHELL_SUFFIX = re.compile(r"\.(?:sh|bash)$")
_SHELL_SHEBANG = re.compile(r"^#!.*\b(?:bash|sh)\b")
_PYTHON_SUFFIX = re.compile(r"\.pyi?$")


def is_shell_source(path: str, first_line: str) -> bool:
    """True when PATH names shell: a `.sh` / `.bash` suffix, or an EXTENSIONLESS
    file whose FIRST_LINE is a bash/sh shebang (git hooks under `.hooks/`, `bin/`
    scripts).

    One definition, so a lint choosing between the bash grammar and a text scan
    classifies a path the same way `tracked_shell_files` does. Getting this wrong
    is silent in the dangerous direction: a shell file misread as non-shell falls
    back to a text scan and quietly loses the grammar's heredoc/string precision.
    """
    path = path.replace("\\", "/")
    if _SHELL_SUFFIX.search(path):
        return True
    if "." in path.rsplit("/", 1)[-1]:
        return False
    return bool(_SHELL_SHEBANG.match(first_line))


def is_python_source(path: str) -> bool:
    """True when PATH names Python: a `.py` / `.pyi` suffix. Extensionless
    Python is not recognised — a lint choosing a grammar by path gets the text
    fallback there rather than feeding shell to `tokenize`."""
    return bool(_PYTHON_SUFFIX.search(path.replace("\\", "/")))


def is_test_path(path: str) -> bool:
    """True when PATH names a test file: a tests/ or spec/ directory component, a
    test_* or conftest module, or a test.* / spec.* / *.test.* / *.spec.* suite."""
    return bool(_TEST_PATH.search(path.replace("\\", "/")))


def tracked_shell_files() -> list[str]:
    """Every tracked `*.sh` / `*.bash` path, plus every tracked extensionless file
    whose shebang names bash/sh (git hooks under `.hooks/`, `bin/` scripts).

    Used by the shell lints that must reason about the WHOLE tracked surface
    rather than only the files pre-commit passes them. An unreadable path is
    skipped: `git ls-files` reports the index, so a path can be missing (a
    rename/delete race) or be binary despite a shell-ish name.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, text=True, check=True
    ).stdout.split("\0")
    out: list[str] = []
    for path in tracked:
        if not path:
            continue
        if _SHELL_SUFFIX.search(path):
            out.append(path)
            continue
        if "." in path.rsplit("/", 1)[-1]:
            continue
        try:
            first = Path(path).read_text(encoding="utf-8").split("\n", 1)[0]
        except (OSError, UnicodeDecodeError):
            continue
        if is_shell_source(path, first):
            out.append(path)
    if not out:
        # Same reason as the empty result in `workflow_files`: a tree with no
        # tracked shell file gives the caller a clean pass it cannot tell from
        # a real one.
        print(
            "note: no tracked shell file in this repository — "
            "this check scanned nothing.",
            file=sys.stderr,
        )
    return out


def workflow_files(workflows_dir: Path, actions_dir: Path | None = None) -> list[Path]:
    """Every workflow file, path-sorted, plus every composite-action definition
    when ACTIONS_DIR is given.

    Pass no ACTIONS_DIR for a check whose rule is about workflows alone — a
    concurrency group or a job timeout means nothing in a composite action.
    That case is a parameter here rather than a second glob in each caller,
    because the two differ only in which directories they read.

    The dirs are passed in (not read from this module) so a consumer's tests can
    monkeypatch its own ``WORKFLOWS_DIR`` / ``ACTIONS_DIR`` constants and still
    redirect discovery.

    PROBLEM CLASS — a check that scans what it discovers reports a clean pass
    when it discovers nothing. Exit 0 is true here, unlike the empty argv
    ``run_file_cli`` refuses: a repository with no workflow really has no
    workflow to violate. But the caller cannot tell that reading from an empty
    tree apart from a real pass, so the empty result says so on stderr.
    """
    files = [p for glob in WORKFLOW_GLOBS for p in workflows_dir.glob(glob)]
    if actions_dir is not None and actions_dir.exists():
        files += actions_dir.rglob("action.yaml")
        files += actions_dir.rglob("action.yml")
    if not files:
        where = f"{workflows_dir}" + (f" or {actions_dir}" if actions_dir else "")
        print(
            f"note: no workflow file under {where} — this check scanned nothing.",
            file=sys.stderr,
        )
    return sorted(files)


def workflow_triggers(doc: object) -> object:
    """A workflow's `on:` value, whatever key spelling reached the parser.

    PyYAML resolves the bareword key `on:` to the boolean True (YAML 1.1), so a
    lint reading `doc["on"]` alone silently sees no triggers on most real
    workflows. One SSOT for every lint that dispatches on trigger type.
    """
    if not isinstance(doc, dict):
        return None
    return doc.get("on", doc.get(True))


# What counts as routing a failure to a human. One SSOT because two lints ask the
# same question from opposite ends: check_cron_alert_coverage asks "does this
# scheduled workflow contain a sink?", check_failure_notifier_coverage asks "is
# this workflow_run workflow the failure notifier?". A sink taught to one is a
# sink to the other, so separate lists would let a repo's house sink be
# recognized on one side and invisible on the other.
NOTIFIER_PATTERNS = (
    r"notif",
    r"ntfy",
    r"slack",
    r"pagerduty",
    r"opsgenie",
    r"victorops",
    r"discord",
    r"telegram",
    r"mattermost",
    r"webhook",
    r"\bsms\b",
    r"smtp",
    r"send[-_]?e?mail",
    r"sendmail",
    r"gh\s+issue\s+create",
    # Any issue-management verb, not `create` alone: a tracker issue opened,
    # filed, or reopened on failure routes to a human exactly the same way, and
    # repos wrap the call in a house script whose name never carries the literal
    # `gh issue create` (`manage-release-failure-issue.sh open`). The leading verb
    # is what keeps this from matching every step that merely mentions an issue;
    # the gap is a bounded lazy run of one token class (no nested quantifier), so
    # a long non-matching identifier cannot backtrack exponentially.
    r"(?:create|open|file|manage|report)[\w.\- ]{0,40}?issues?\b",
    r"issue_write",
)


def notifier_matcher(extra_patterns: Iterable[str] = ()) -> "re.Pattern[str]":
    """The compiled alternation recognizing a notification sink. EXTRA_PATTERNS
    are added to (never substituted for) the defaults, so naming a house sink can
    never silently un-recognize the sinks already matched."""
    return re.compile(
        "|".join(f"(?:{p})" for p in (*NOTIFIER_PATTERNS, *extra_patterns)),
        re.IGNORECASE,
    )


def step_text(step: dict) -> str:
    """The fields of a workflow step that can name a notification sink."""
    return " ".join(str(step.get(key, "")) for key in ("uses", "name", "run"))


def has_trigger(doc: object, *names: str) -> bool:
    """True when the workflow fires on any of NAMES, for any shape `on:` takes:
    a scalar (`on: push`), a list (`on: [push, schedule]`), or the usual mapping.
    """
    triggers = workflow_triggers(doc)
    if isinstance(triggers, str):
        return triggers in names
    if isinstance(triggers, list):
        return any(t in names for t in triggers)
    if isinstance(triggers, dict):
        return any(t in triggers for t in names)
    return False


def key_block_lines(text: str, key: str) -> list[str]:
    """The source lines a per-key marker comment may live on: every `KEY:` line
    at any indent, plus that key's DIRECT-child lines (those at the block's
    shallowest child indent, which is where both a `- item` and a standalone
    comment beside it sit).

    Scoping a marker this way is what stops the token from being read out of an
    unrelated part of the file — the same rule `_classification_text` applies to
    `# required-check:` on a job. A bespoke line scanner is used rather than the
    YAML parser for the usual reason: PyYAML discards comments.
    """
    lines = text.splitlines()
    key_re = re.compile(rf"^(?P<indent>[ \t]*)(?P<k>{re.escape(key)})\s*:")
    eligible: list[str] = []
    for index, line in enumerate(lines):
        match = key_re.match(line)
        if not match:
            continue
        indent = len(match.group("indent"))
        block: list[str] = []
        for follow in lines[index + 1 :]:
            if not follow.strip():
                continue
            if len(follow) - len(follow.lstrip()) <= indent:
                break
            block.append(follow)
        child_indent = min(
            (len(b) - len(b.lstrip()) for b in block), default=None
        )  # the block's shallowest line — its direct children
        eligible.append(line)
        eligible += [b for b in block if len(b) - len(b.lstrip()) == child_indent]
    return eligible


# A reason that states nothing. Normalized (lowercased, non-alphanumerics folded
# to single spaces) before lookup, so "N/A", "n/a.", and "-- none --" all land
# here. An opt-out marker exists to record WHY the default obligation does not
# apply; a placeholder records only that someone wanted the check to stop.
MARKER_PLACEHOLDERS = frozenset(
    {
        "",
        "n a",
        "na",
        "nil",
        "no",
        "none",
        "nope",
        "none needed",
        "no reason",
        "not needed",
        "not applicable",
        "not required",
        "nothing",
        "nothing to do",
        "obvious",
        "see above",
        "self explanatory",
        "tbd",
        "todo",
        "unnecessary",
        "x",
    }
)


def is_placeholder_reason(reason: str) -> bool:
    """True when REASON is a negative placeholder rather than a stated reason."""
    normalized = re.sub(r"[^a-z0-9]+", " ", reason.lower()).strip()
    return normalized in MARKER_PLACEHOLDERS


def parse_optout_marker(lines: list[str], token: str) -> tuple[str | None, str | None]:
    """Read a `# TOKEN: false  # <reason>` opt-out from LINES.

    Returns `(reason, error)`: `(None, None)` when no marker is present,
    `(None, message)` when one is present but does not state a reason (or names
    an unrecognized value), and `(reason, None)` when it is well formed. The
    reason's `#` introducer is optional — the substance, not the punctuation, is
    what this enforces.
    """
    pattern = re.compile(
        rf"#\s*{re.escape(token)}\s*:\s*(?P<value>\S+)(?P<rest>.*)$", re.IGNORECASE
    )
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        value = match.group("value").strip(",;\"'").lower()
        if value != "false":
            return None, (
                f"`# {token}: {match.group('value')}` names an unrecognized value — "
                f"the only opt-out this marker accepts is `# {token}: false  "
                "# <reason>`."
            )
        reason = match.group("rest").strip().lstrip("#").strip()
        if is_placeholder_reason(reason):
            detail = f"states only {reason!r}" if reason else "carries no reason"
            return None, (
                f"`# {token}: false` {detail}. The reason IS the marker — write "
                f"`# {token}: false  # <why this workflow does not need it>` so a "
                "reviewer can check the argument instead of the annotation."
            )
        return reason, None
    return None, None


def has_decide_gate(jobs: dict) -> bool:
    """True if any job uses decide-reusable.yaml or conditions on needs.decide.outputs.*"""
    for job_cfg in jobs.values():
        if not isinstance(job_cfg, dict):
            continue
        if "decide-reusable.yaml" in str(job_cfg.get("uses", "")):
            return True
        if "needs.decide.outputs" in str(job_cfg.get("if", "")):
            return True
    return False


def is_always_reporter(if_value: object) -> bool:
    """True if a job `if:` value is an unconditional always() reporter.

    Accepts bare `always()` and the semantically identical `${{ always() }}`
    wrapper (any inner spacing). A compound condition such as `always() && cond`
    is intentionally rejected: it does not always run, so it is no reporter.
    """
    return unwrap_expression(if_value) == "always()"


def has_always_reporter(jobs: dict) -> bool:
    """True if any job has an always() reporter `if:` — the required-check shape."""
    return any(
        isinstance(job_cfg, dict) and is_always_reporter(job_cfg.get("if", ""))
        for job_cfg in jobs.values()
    )


# A concurrency group keyed by any of these is per-ref / per-PR / per-run, so a
# run is only ever superseded by a *newer run of the same ref* — whose own
# reporter then posts the check. Without one of these the group is static and a
# sibling ref's run can cancel this one with no replacement report. Shared by
# the concurrency lints (check_static_concurrency, which flags a static group on
# the decide+always() shape, and check_cancellable_required_check, which flags a
# static *cancellable* group on any required-check-marked workflow) so the
# per-ref definition is one SSOT, not two copies that could drift.
PER_REF_CONCURRENCY_KEYS = (
    "github.ref",
    "github.ref_name",
    "github.head_ref",
    "github.run_id",
    "github.run_number",
    "pull_request.number",
    "github.event.number",
)

# A `${{ … }}` expression span. Non-greedy: each span ends at its own `}}`.
_EXPR_SPAN = re.compile(r"\$\{\{(?P<expr>.*?)\}\}", re.DOTALL)


def group_is_per_ref(group: str) -> bool:
    """True if a concurrency `group:` expression carries a per-ref/per-PR/per-run
    key INSIDE a `${{ … }}` expression span — meaning a superseding run is always
    the same ref's newer run, which re-reports, so the group cannot strand a
    required check. Outside a span the key is a literal: a group named
    `"github.ref-shared"` is one static string for every ref, so a bare
    substring match would fail open exactly on the workflows this guard exists
    to flag."""
    return any(
        key in span.group("expr")
        for span in _EXPR_SPAN.finditer(group)
        for key in PER_REF_CONCURRENCY_KEYS
    )


def strip_yaml_comments(text: str) -> str:
    """TEXT with every YAML `#` comment blanked to spaces, line and column
    offsets preserved so a caller can still report `line=` annotations.

    A lint that hunts for a pattern in workflow *content* must not see the
    pattern when it is only being TALKED ABOUT in a comment: a step comment
    reading ``# not a `secrets.A || secrets.B` expression`` made the secret-name
    round-trip demand that A and B be added to the allowlist, and the natural
    way to silence that is to pad the allowlist with names no workflow uses —
    eroding the very guard that catches a misspelled secret.

    Comment detection is delegated to PyYAML's own scanner rather than a
    hand-rolled `split("#")`, because the naive cut is a FALSE NEGATIVE
    machine: a `#` inside a quoted scalar (`title: "#general"`) or inside a
    block scalar (a shell comment under `run: |`) is content, and cutting there
    would stop checking a real `secrets.TYPO` later on the same line. Every
    span PyYAML reports as a scalar token is protected; a `#` outside one, at
    line start or after whitespace, opens a comment that runs to end of line —
    which is exactly YAML's own rule, so what this keeps is exactly what GitHub
    parses.

    A file PyYAML cannot even tokenize is returned unchanged: nothing is known
    about where its scalars end, and blanking on a guess could hide a real
    finding.
    """
    try:
        spans = [
            (token.start_mark.index, token.end_mark.index)
            for token in yaml.scan(text, Loader=yaml.SafeLoader)
            if isinstance(token, yaml.tokens.ScalarToken)
        ]
    except yaml.YAMLError:
        return text

    protected = bytearray(len(text))
    for start, end in spans:
        protected[start:end] = b"\x01" * (end - start)

    out: list[str] = []
    in_comment = False
    for index, char in enumerate(text):
        if char == "\n":
            in_comment = False
        elif in_comment:
            char = " "
        elif (
            char == "#"
            and not protected[index]
            and (index == 0 or text[index - 1] in " \t\n")
        ):
            in_comment = True
            char = " "
        out.append(char)
    return "".join(out)


def opted_out(text: str, token: str) -> bool:
    """True only when the opt-out TOKEN appears inside an actual comment on some
    line of TEXT, not anywhere in the byte stream — a `group: "<token>"` string
    value must not silently disable a lint (that would be a fail-open). Shared by
    the concurrency lints, each of which passes its own token.

    Delegates to ``annotation_re`` rather than testing containment: a bare
    substring is open at BOTH ends, so `# no-<token>-here` — or a neighbouring
    lint's longer slug that happens to contain this one — would suppress. These
    tokens carry no reason by contract, hence ``require_reason=False``; the
    stand-alone-token guarantee is the same one every other annotation gets.
    """
    marker = annotation_re(token, require_reason=False)
    return any(marker.search(line) for line in text.splitlines())


def concurrency_line(text: str) -> int:
    """Return the 1-based line number of the top-level `concurrency:` key, or 1
    when the text has none (the fallback anchor). Shared by the concurrency
    lints so their `::error line=` annotations agree byte-for-byte."""
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(r"^concurrency\s*:", line):
            return num
    return 1


def job_concurrency_line(block: tuple[int, str] | None, fallback: int) -> int:
    """The 1-based line of a job's `concurrency:` key within its source BLOCK
    (from `_job_blocks`), else FALLBACK. Scoping the scan to the job's own block
    anchors the annotation on the offending job, not a sibling's block."""
    if block is None:
        return fallback
    start, body = block
    for offset, line in enumerate(body.splitlines()):
        if re.match(r"^\s+concurrency\s*:", line):
            return start + offset
    return fallback


def _job_blocks(text: str) -> dict[str, tuple[int, str]]:
    """Map each top-level job name to (1-based key line, its source block).

    A block is the job's key line plus every following body line indented deeper
    than the key — it stops at the next line dedented to the job-key indent or
    shallower (a sibling job, an inter-job comment, or the end of `jobs:`). Blank
    lines never terminate a block. Comments thus count as classification only
    when trailing the key line or living inside the indented body.

    Shared by the required-check lint and the apply step so both read the marker
    from byte-identical scoping; the comment-scope semantics are why a bespoke
    line scanner is used over a YAML parser (PyYAML discards comments).
    """
    lines = text.splitlines()
    jobs_idx = next(
        (i for i, line in enumerate(lines) if re.match(r"^jobs\s*:", line)), None
    )
    if jobs_idx is None:
        return {}

    job_indent = next(
        (
            len(line) - len(line.lstrip())
            for line in lines[jobs_idx + 1 :]
            if line.strip() and not line.lstrip().startswith("#")
        ),
        None,
    )
    if job_indent is None:
        return {}

    blocks: dict[str, tuple[int, str]] = {}
    key = re.compile(rf"^\s{{{job_indent}}}([^\s:#][^:]*?)\s*:")
    i = jobs_idx + 1
    while i < len(lines):
        stripped = lines[i].strip()
        indent = len(lines[i]) - len(lines[i].lstrip())
        if stripped and not stripped.startswith("#") and indent < job_indent:
            break
        match = key.match(lines[i])
        if not (match and indent == job_indent and not stripped.startswith("#")):
            i += 1
            continue
        end = i + 1
        while end < len(lines):
            body = lines[end]
            if body.strip() and len(body) - len(body.lstrip()) <= job_indent:
                break
            end += 1
        name = match.group(1).strip("'\"")  # align with PyYAML's unquoted key
        blocks[name] = (i + 1, "\n".join(lines[i:end]))
        i = end
    return blocks


def _classification_text(block: str) -> str:
    """The lines of a job block where a classification comment may live: the key
    line plus the job's direct-child lines (a trailing comment on a child, or a
    standalone comment at the child indent). Deeper step/run content is excluded
    so a `# required-check:` string buried in a step can't pass as a classification.
    """
    lines = block.splitlines()
    if not lines:
        return ""
    child_indent = next(
        (len(ln) - len(ln.lstrip()) for ln in lines[1:] if ln.strip()), None
    )
    eligible = [lines[0]]
    if child_indent is not None:
        eligible += [
            ln
            for ln in lines[1:]
            if ln.strip() and len(ln) - len(ln.lstrip()) == child_indent
        ]
    return "\n".join(eligible)


def matrix_combinations(matrix: dict) -> list[dict]:
    """Expand a job's `strategy.matrix` into the list of variable combinations
    GitHub schedules — the Cartesian product of the axis lists, then `exclude`
    removed and `include` entries extended-or-appended."""
    axes = {
        k: v
        for k, v in matrix.items()
        if k not in ("include", "exclude") and isinstance(v, list)
    }
    if axes:
        names = list(axes)
        combos = [
            dict(zip(names, vals, strict=True))
            for vals in itertools.product(*axes.values())
        ]
    else:
        combos = [{}]

    for ex in matrix.get("exclude", []) or []:
        combos = [c for c in combos if not all(c.get(k) == v for k, v in ex.items())]

    includes = matrix.get("include", []) or []
    if not axes:
        # No base matrix: each include entry is its own job (a bare matrix with
        # only `include` schedules exactly those entries).
        return [dict(inc) for inc in includes] if includes else combos

    for inc in includes:
        extendable = [
            c for c in combos if all(c.get(k) == v for k, v in inc.items() if k in axes)
        ]
        if extendable:
            for c in extendable:
                c.update(inc)
        else:
            combos.append(dict(inc))
    return combos


def expand_name(name: str, matrix: dict) -> list[str]:
    """Resolve a job's `name:` into every concrete check context it produces,
    substituting `${{ matrix.X }}` across the job's matrix."""
    refs = set(MATRIX_REF.findall(name))
    if not refs:
        return [name]

    resolved = []
    for combo in matrix_combinations(matrix):
        if not refs <= combo.keys():
            continue
        resolved.append(MATRIX_REF.sub(lambda m, c=combo: str(c[m.group("key")]), name))
    return sorted(set(resolved))


def _marked_jobs(blocks: dict[str, tuple[int, str]], jobs: dict) -> list[str]:
    """The keys of JOBS whose block carries a `# required-check: true` marker.

    PROBLEM CLASS — which jobs of this workflow declare a required status check?
    Every consumer asks it here. The marker counts only on a job's key line or a
    direct-child line (`_job_blocks` + `_classification_text`), so the same text
    inside a step body is no classification. A lint that re-scans the blocks
    itself gets a different subset of that scope rule wrong. Takes the blocks the
    caller already computed, so no consumer pays a second scan of one file.
    """
    return [
        name
        for name, cfg in jobs.items()
        if isinstance(cfg, dict)
        and REQUIRED_MARKER.search(_classification_text(blocks.get(name, (0, ""))[1]))
    ]


def required_check_contexts(text: str) -> list[str]:
    """Every required-check context declared by one workflow's source.

    Scans EVERY job (not only `always()` reporters) for a `# required-check: true`
    marker, then expands each such job's `name:` across its own `strategy.matrix`
    into concrete check contexts. This is the set a branch-protection ruleset must
    require; the reporter lint enforces the stricter obligation that reporters be
    classified, a superset of what is read here (a cheap always-run linter carries
    the marker but is no reporter).
    """
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        return []
    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return []

    contexts: list[str] = []
    for name in _marked_jobs(_job_blocks(text), jobs):
        cfg = jobs[name]
        matrix = (cfg.get("strategy") or {}).get("matrix") or {}
        contexts += expand_name(str(cfg.get("name", name)), matrix)
    return contexts
