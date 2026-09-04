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
``workflow_files()`` discovery glob; it lives here too. The required-check-shape
probes (``decide_gate_names``, ``has_always_reporter``, ``has_fail_closed_twin``,
``required_check_shape``) are shared by ``check_always_reporter``,
``check_required_reporter`` and the two concurrency lints, and live here too. One
copy is the point: a lint that grew its own matcher for the twin shape would
answer "does this workflow back a required check?" differently from its siblings.

Imported as a sibling: the scripts run as ``python3 ci_truth_serum/check_*.py`` (or
``python -m ci_truth_serum.check_*``), so each script prepends its own dir to ``sys.path``
before importing this module; the tests load each script by path.
"""

import ast
import itertools
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_fastyaml import SafeLoader, safe_load, scan  # noqa: E402,I001  # pylint: disable=wrong-import-position

# Lines whose first word only prints text — a command quoted inside them is an
# example or hint, not executed code. Shared by the stderr- and download-pinning
# checks; check_exit_suppression extends it (it also excuses status helpers).
MESSAGE_PREFIX = re.compile(r"^(?:echo|printf|warn|status|die|log|:)\b")


class LineLoader(SafeLoader):
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
# The same characters as a set, for the code that walks text rather than matching
# it. `test_line_boundary_spellings_agree` pins the two spellings together.
_LINE_BOUNDARY = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


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
# the structural question `_cts_comments` answers with each language's own grammar.
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
    full ``_cts_bash_ast`` grammar)."""
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
    line-oriented lints do; a lint that must pick a parser (see ``_cts_comments``)
    needs the path and calls the two-argument form directly."""
    return run_source_checks(argv, lambda text, _path: find_violations(text), message)


def unparseable_shell_reason(path: str, text: str) -> str | None:
    """The refusal message when PATH names shell the grammar cannot read, else None.

    The bash grammar is imported HERE rather than at module scope, and only once
    the path is known to be shell. Around fifty checks import this module, and
    most of them read YAML or Python and are handed no shell at all; a module-
    scope import would put `tree_sitter_bash` in every one of their pre-commit
    environments. A check that IS handed shell still gets the loud ImportError
    `_cts_bash_ast` raises, so the deferral costs no fail-closed behaviour.
    """
    if not is_shell_source(path, text.split("\n", 1)[0]):
        return None
    # This module is loaded BY PATH (the check scripts, and tests/_helpers.load_hook),
    # so a sibling import needs this directory on the path first — the same prelude
    # every check script carries.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _cts_bash_ast import (  # pylint: disable=import-outside-toplevel
        UnparseableShellError,
        assert_parseable,
    )

    try:
        assert_parseable(text)
    except UnparseableShellError as err:
        return str(err)
    return None


def unparseable_python_reason(path: str, text: str) -> str | None:
    """The refusal message when PATH names Python this interpreter cannot parse,
    else None.

    A detector that reads the tree never saw the constructs it matches on, so its
    empty result is not a pass — the same reasoning as the shell refusal above.
    The usual cause is an interpreter OLDER than the tree: a PEP 701 f-string
    (nested quotes) is a syntax error before Python 3.12, and a scope-dependent
    lint then reads a function-local statement as a module-level one. Naming the
    running version is what points the reader at their hook environment.
    """
    if not is_python_source(path):
        return None
    try:
        ast.parse(text)
    except (SyntaxError, ValueError) as err:
        version = ".".join(str(n) for n in sys.version_info[:3])
        return (
            f"Python {version} cannot parse this file ({err}), so this check "
            f"never read it. Its result here is not a pass. Run the hook on the "
            f"interpreter the tree targets — pre-commit takes it from "
            f"`default_language_version:`."
        )
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
        reason = unparseable_shell_reason(path, text) or unparseable_python_reason(
            path, text
        )
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


def decide_gate_names(jobs: dict) -> set[str]:
    """The names of the decide gates a workflow declares.

    Two spellings name a gate. A job that calls `decide-reusable.yaml` IS one,
    under its own job name. A job conditioned on `needs.decide.outputs.*` names
    the gate `decide` in the expression itself, so that name counts even when
    the workflow declares no job by that name — a twin pointed at a gate job
    that does not exist is a fail-open this set is what makes visible.
    """
    names: set[str] = set()
    for name, job_cfg in jobs.items():
        if not isinstance(job_cfg, dict):
            continue
        if "decide-reusable.yaml" in str(job_cfg.get("uses", "")):
            names.add(str(name))
        if "needs.decide.outputs" in str(job_cfg.get("if", "")):
            names.add("decide")
    return names


def has_decide_gate(jobs: dict) -> bool:
    """True if any job uses decide-reusable.yaml or conditions on needs.decide.outputs.*"""
    return bool(decide_gate_names(jobs))


def job_needs(job_cfg: dict) -> list[str]:
    """A job's declared dependencies. GitHub accepts a scalar (`needs: decide`)
    and a list (`needs: [decide, lint]`); both read the same here."""
    needs = job_cfg.get("needs")
    if isinstance(needs, str):
        return [needs]
    if isinstance(needs, list):
        return [str(item) for item in needs]
    return []


def gated_work_jobs(jobs: dict, gate_names: Iterable[str]) -> list[str]:
    """The jobs a decide gate switches on and off — every job whose `if:` reads
    `needs.<gate>.outputs`, excluding the gates themselves."""
    gates = set(gate_names)
    tokens = {f"needs.{gate}.outputs" for gate in gates}
    return [
        str(name)
        for name, cfg in jobs.items()
        if isinstance(cfg, dict)
        and str(name) not in gates
        and any(token in str(cfg.get("if", "")) for token in tokens)
    ]


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


# A fail-closed twin's condition tail: one `needs.<gate>.result != 'success'`
# disjunct per gate, `||`-joined, optionally in one balanced paren group.
_TWIN_DISJUNCTS = (
    r"needs\.[A-Za-z0-9_-]+\.result\s*!=\s*'success'"
    r"(?:\s*\|\|\s*needs\.[A-Za-z0-9_-]+\.result\s*!=\s*'success')*"
)
_TWIN_TAIL = re.compile(rf"(?:{_TWIN_DISJUNCTS}|\(\s*{_TWIN_DISJUNCTS}\s*\))")
# One disjunct, with the gate name it reads.
_TWIN_REF = re.compile(r"needs\.(?P<gate>[A-Za-z0-9_-]+)\.result\s*!=\s*'success'")


def twin_gate_refs(if_value: object) -> list[str] | None:
    """The gate names a twin-shaped `if:` reads, or None when it is not twin-shaped.

    Shape: `always() && needs.<gate>.result != 'success'`, with `||` between
    disjuncts when several gates share the twin.
    """
    expression = unwrap_expression(if_value)
    prefix, sep, rest = expression.partition("&&")
    if not sep or prefix.strip() != "always()":
        return None
    tail = rest.strip()
    if _TWIN_TAIL.fullmatch(tail) is None:
        return None
    return [match.group("gate") for match in _TWIN_REF.finditer(tail)]


def is_fail_closed_twin(
    if_value: object, gate_names: Iterable[str] | None = None
) -> bool:
    """True if a job `if:` runs exactly when a decide gate did not succeed.

    The zero-boot counterpart of a bare always() reporter. On a healthy run the
    job is SKIPPED, which boots no runner and still satisfies a required check;
    it runs (and must fail) only when a gate itself did not succeed — the one
    case where every gated work job skips and a merge would otherwise green
    over a broken gate.

    With GATE_NAMES the disjuncts must cover exactly that set. Shape alone is
    not enough to accept a twin: a twin naming only one of two gates leaves the
    other free to fail unreported, and a twin naming a job that is no gate reds
    every healthy run. Pass None to ask the shape question on its own, which is
    what a caller that only CLASSIFIES the job wants.
    """
    refs = twin_gate_refs(if_value)
    if refs is None:
        return False
    return gate_names is None or set(refs) == set(gate_names)


def has_fail_closed_twin(jobs: dict, gate_names: Iterable[str] | None = None) -> bool:
    """True if any job is a fail-closed twin — the other required-check shape.

    With GATE_NAMES the twin must also `needs:` every gate it names. A gate a
    job does not depend on has an empty `result` in that job's expression, so
    the disjunct never fires and the twin stays skipped when that gate breaks.
    """
    gates = None if gate_names is None else set(gate_names)
    return any(
        isinstance(job_cfg, dict)
        and is_fail_closed_twin(job_cfg.get("if", ""), gates)
        and (gates is None or gates <= set(job_needs(job_cfg)))
        for job_cfg in jobs.values()
    )


ALWAYS_REPORTER_SHAPE = "decide gate + always() reporter"
TWIN_SHAPE = "decide gate + fail-closed twin"


def required_check_shape(jobs: dict, workflow: dict) -> str | None:
    """Which required-check shape a workflow's jobs form, or None for neither.

    A gated workflow reports through a job that runs when the gate breaks. That
    job is either an always() reporter or a fail-closed twin, and every lint
    that asks "does this workflow back a required check?" must accept both — a
    lint that knows only the reporter mis-reads a twin-shaped workflow as
    ordinary and drops its protection.

    A twin counts only when it is VALID. A twin that omits a gate, names a job
    that is no gate, or exits 0 protects nothing, so reading it as a shape would
    have every caller here defend a required check the workflow does not have.
    """
    if not has_decide_gate(jobs):
        return None
    if has_always_reporter(jobs):
        return ALWAYS_REPORTER_SHAPE
    return TWIN_SHAPE if valid_twin_names(jobs, workflow) else None


def valid_twin_names(jobs: dict, workflow: dict) -> list[str]:
    """The names of the fail-closed twins that really do fail closed."""
    gate_names = decide_gate_names(jobs)
    if not gate_names:
        return []
    return [
        name
        for name, cfg in jobs.items()
        if isinstance(cfg, dict)
        and is_fail_closed_twin(cfg.get("if", ""))
        and not twin_defects(cfg, gate_names, workflow)
    ]


def twin_defects(cfg: dict, gate_names: set[str], workflow: dict) -> list[str]:
    """Why this twin-shaped job does not in fact fail closed. Empty means it does."""
    refs = set(twin_gate_refs(cfg.get("if", "")) or [])
    defects = []
    uncovered = sorted(gate_names - refs)
    if uncovered:
        defects.append(
            f"its condition reads no result for the decide gate(s) {', '.join(uncovered)}, "
            "so each of those can fail with this job skipped"
        )
    foreign = sorted(refs - gate_names)
    if foreign:
        defects.append(
            f"its condition reads {', '.join(foreign)}, which is no decide gate of this "
            "workflow — the job then reds on healthy runs and skips on broken gates"
        )
    unneeded = sorted((refs & gate_names) - set(job_needs(cfg)))
    if unneeded:
        defects.append(
            f"it does not `needs:` {', '.join(unneeded)}, so that gate's result reads as "
            "empty here and the disjunct never fires"
        )
    if continues_on_error(cfg.get("continue-on-error")):
        defects.append(
            "it declares `continue-on-error`, so every step's failure is reported as "
            "success and the job concludes green on the run that needs it red"
        )
    step = last_run_step(cfg)
    if step is None:
        defects.append(
            "it runs no `run:` step whose failure can fail the job, so it succeeds on "
            "the run that needs it red"
        )
        return defects
    template = resolved_shell(cfg, step, workflow)
    shell = shell_program(template)
    if shell is None:
        defects.append(
            "a `${{ }}` expression hides its last `run:` step's shell, so whether "
            "that step exits nonzero cannot be verified and the twin is not accepted"
        )
    elif shell not in READABLE_SHELLS:
        defects.append(
            f"its last `run:` step declares `shell: {shell}`, which this check reads "
            "as bash — whether it exits nonzero cannot be verified, so the twin is "
            "not accepted"
        )
    elif not template_runs_the_script(template):
        defects.append(
            f"its last `run:` step declares `shell: {template}`, which passes `-c` — "
            "the shell then runs that command string and leaves the step's own script "
            "as `$0`, so the script never executes and its failure never fires"
        )
    elif unreadable_run_script(str(step["run"])):
        defects.append(
            "the bash grammar cannot read its last `run:` step, so whether that step "
            "exits nonzero cannot be verified and the twin is not accepted"
        )
    elif not script_ends_in_failure(str(step["run"])):
        defects.append(
            "its last `run:` step does not end in a failing command, so the job exits 0 "
            "on the run that needs it red"
        )
    return defects


def continues_on_error(value: object) -> bool:
    """True when a `continue-on-error:` value can divorce a status from the job's.

    GitHub accepts an expression here, which no offline check can resolve, so
    anything but a literal false counts — the twin is then refused rather than
    accepted on a guess about what the expression evaluates to.
    """
    if value is None or value is False:
        return False
    return str(value).strip().lower() != "false"


def last_run_step(job_cfg: dict) -> dict | None:
    """A job's last `run:` step whose failure is GUARANTEED to fail the job.

    Two keys break that guarantee, so a step carrying either can never be what
    makes a twin red. `continue-on-error` reports the step's failure as success.
    A skippable `if:` can drop the step entirely, and a skipped step fails
    nothing — so an earlier `run:` step has to decide, or the job runs none that
    can.
    """
    steps = job_cfg.get("steps")
    if not isinstance(steps, list):
        return None
    runs = [
        step
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("run"), str)
        and not continues_on_error(step.get("continue-on-error"))
        and step_always_runs(step)
    ]
    return runs[-1] if runs else None


# The step `if:` conditions that cannot skip the step. `always()` runs it
# whatever the earlier steps did, and `true` is the constant. Every other
# condition can skip — `success()` and `!cancelled()` included — and so can any
# expression this check cannot resolve offline.
_UNSKIPPABLE_STEP_CONDITIONS = frozenset({"always()", "true"})


def step_always_runs(step: dict) -> bool:
    """True when nothing in STEP's `if:` can skip it."""
    if "if" not in step:
        return True
    text = str(step["if"]).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text.lower() in _UNSKIPPABLE_STEP_CONDITIONS


# The shells `script_ends_in_failure` can read. The bash grammar covers both;
# any other value names a language this check would misparse as bash.
READABLE_SHELLS = ("bash", "sh")

# The only two commands `script_ends_in_failure` reads as unconditional
# failures, so the only two a shadowing definition can take away from it.
_SHADOWABLE = frozenset({"false", "exit"})


def resolved_shell(job_cfg: dict, step: dict, workflow: dict) -> str:
    """The `shell:` value that applies to STEP, at whichever scope sets it.

    `default_run_shell` owns the precedence: the step's own `shell:`, then the
    job's `defaults.run.shell`, then the workflow's. GitHub's default on a runner
    is bash, which is what an absent key means here.
    """
    return str(
        step.get("shell") or default_run_shell(job_cfg, workflow) or "bash"
    ).strip()


def step_shell(job_cfg: dict, step: dict, workflow: dict) -> str | None:
    """Which shell runs STEP, named by its program alone, or None when a
    `${{ }}` expression hides that program."""
    return shell_program(resolved_shell(job_cfg, step, workflow))


def template_runs_the_script(shell: str) -> bool:
    """True when a custom `shell:` template hands GitHub's script to the program.

    `{0}` is the path of the script GitHub writes from the step's `run:` body. A
    template that passes `-c` runs its own command string instead and leaves
    `{0}` as `$0`, so `bash -c true {0}` starts bash, runs `true`, and never
    executes the step's `exit 1`. A value carrying no `{0}` is a keyword such as
    `bash`, and GitHub always runs the script under those.
    """
    words = shell.split()
    if "{0}" not in words:
        return True
    for word in words[: words.index("{0}")]:
        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            return False
    return True


def script_ends_in_failure(script: str) -> bool:
    """True when SCRIPT's last statement exits nonzero however its earlier
    commands went.

    A fail-closed twin runs only on a broken gate, so it has to FAIL there. A
    twin whose script ends in `echo` exits 0 and greens the merge over the very
    gate it exists to redden. The bash grammar answers this, never a regex:
    `cmd && exit 1` always fails, `cmd || exit 1` fails only when `cmd` does
    too, and only the parse tree tells the two apart
    (.claude/rules/shell-lint-parsing.md).

    A `${{ … }}` expression is GitHub Actions syntax, not bash, so each span is
    replaced by an inert word of the same length before the parse. tree-sitter
    recovers from a construct it cannot read by dropping nodes for the REST of
    the input, so one un-neutralized expression on line 1 hides the `exit 1` on
    line 2 and reports a compliant twin as failing open.
    """
    # Imported here, not at module scope: around fifty checks load this module
    # and most are handed no shell at all — the same deferral
    # `unparseable_shell_reason` above documents.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _cts_bash_ast import (  # pylint: disable=import-outside-toplevel
        command_words,
        unquote,
    )
    from _cts_bash_ast import (
        parse as bash_parse,
    )

    def defined_functions(node) -> set[str]:
        """The function names defined in NODE's OWN shell process, at any depth.

        A script that defines `false` or `exit` redefines the only two commands
        this analysis reads as unconditional failures, so neither is trusted
        where the definition reaches. A definition inside a function body or a
        `{ … }` group does reach, because neither forks. A `( … )` subshell does
        fork, so its definitions die with it — those are collected again where
        the subshell itself is judged.

        An explicit stack, never recursion: a generated `run:` body nests deeply
        enough to pass Python's recursion limit, and the callers handle only
        `PathologicalInputError`.
        """
        found: set[str] = set()
        stack = list(node.children)
        while stack:
            current = stack.pop()
            if current.type == "subshell":
                continue
            if current.type == "function_definition":
                names = [child for child in current.children if child.type == "word"]
                if names:
                    found.add(names[0].text.decode())
            stack.extend(current.children)
        return found

    def command_always_fails(command) -> bool:
        words = command_words(command)
        if not words:
            return False
        name, args = words[0], [unquote(word) for word in words[1:]]
        if name in shadowed:
            return False
        if name == "false":
            return True
        # Bare `exit` re-raises the previous command's status, so it fails only
        # when that command did. `exit 256` wraps to 0 and is no failure either.
        if name != "exit" or len(args) != 1 or not args[0].isdigit():
            return False
        return int(args[0]) % 256 != 0

    def always_fails(node) -> bool:
        if node.type == "command":
            return command_always_fails(node)
        if node.type == "list":
            operands = [child for child in node.children if child.is_named]
            if len(operands) != 2:
                return False
            left, right = operands
            if any(child.type == "&&" for child in node.children):
                # A failing left short-circuits to its own status; otherwise the
                # right one decides. So either side failing always is enough.
                return always_fails(left) or always_fails(right)
            return always_fails(left) and always_fails(right)
        if node.type == "subshell" and defined_functions(node) & _SHADOWABLE:
            # Its own definitions shadow the commands this reads as failures,
            # and they do not reach the enclosing script's `shadowed` set.
            return False
        if node.type in ("subshell", "compound_statement"):
            inner = [child for child in node.children if child.is_named]
            return bool(inner) and always_fails(inner[-1])
        return False

    root = bash_parse(_neutralized(script))
    shadowed = defined_functions(root) & _SHADOWABLE
    statements = [
        node for node in root.children if node.is_named and node.type != "comment"
    ]
    return bool(statements) and always_fails(statements[-1])


def _neutralized(script: str) -> str:
    """SCRIPT with each `${{ … }}` span replaced by an inert word of its length.

    A function replacer, never a built string: `re.sub` reads `\\1` and
    `\\g<name>` in a string replacement (the check-replacement-expansion rule).
    """
    return _EXPR_SPAN.sub(lambda match: "_" * len(match.group(0)), script)


def unreadable_run_script(script: str) -> bool:
    """True when the bash grammar cannot read this `run:` body.

    A twin whose script the grammar cannot read is refused as unverifiable, not
    reported as ending in a passing command: those are different faults with
    different fixes, and the misdiagnosis is what `unparseable_shell_reason`
    exists to prevent for a whole file.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _cts_bash_ast import (  # pylint: disable=import-outside-toplevel
        parse as bash_parse,
    )

    return bash_parse(_neutralized(script)).has_error


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

# A single-quoted string literal inside an expression; GitHub escapes a quote
# inside one by doubling it. `${{ 'github.ref' }}` is the fixed TEXT
# "github.ref", not a reference to the ref, so a key name found here names
# nothing and the group is static.
_LITERAL_SPAN = re.compile(r"'(?:[^']|'')*'")

# The events that dispatch a run against the ref the run is FOR, so `github.ref`
# and `github.ref_name` name that ref and tell one run from another.
REF_VARYING_EVENTS = frozenset(
    {
        "push",
        "pull_request",  # refs/pull/<n>/merge — one per PR
        "create",
        "workflow_dispatch",  # the ref the caller chose
        "merge_group",
        "release",  # the release tag
    }
)

# The events that pin `github.ref` to ONE string for every run. Most of them get
# the default branch, because the event names no ref of its own.
# `pull_request_target` is the trap in this set: it runs in the BASE repo, so
# `github.ref` is the base branch. Every open PR onto `main` shares
# `refs/heads/main` there, exactly as two scheduled runs do — `github.head_ref`
# and the PR number are the only per-PR keys that event offers.
REF_CONSTANT_EVENTS = frozenset(
    {
        "pull_request_target",
        "issues",
        "issue_comment",
        "schedule",
        "workflow_run",
        "repository_dispatch",
        "check_run",
        "check_suite",
        "status",
        "label",
        "milestone",
        "discussion",
        "discussion_comment",
        "fork",
        "watch",
        "gollum",
        "public",
        "registry_package",
        "page_build",
        "branch_protection_rule",
        "member",
    }
)

# `github.head_ref` is set on a pull-request event and is EMPTY on every other
# event.
HEAD_REF_EVENTS = frozenset({"pull_request", "pull_request_target"})

# The events whose payload carries the pull request, so `…pull_request.number`
# is set. `github.event.number` is the top-level PR number, which only the two
# pull-request events themselves carry.
PR_PAYLOAD_EVENTS = HEAD_REF_EVENTS | {
    "pull_request_review",
    "pull_request_review_comment",
}

# The events this table models at all. An event outside it — a new GitHub event,
# or a `workflow_call` whose context belongs to the CALLER — leaves every key
# UNKNOWN, so the group stays unflagged. An event INSIDE it can still leave one
# key unknown: `deployment` and `deployment_status` are absent because their
# `github.ref` is whatever the deployment named (a branch, a tag, or nothing for
# a raw SHA), and the review events are here for their PR number while their
# `github.ref` falls through to UNKNOWN.
_KNOWN_EVENTS = REF_VARYING_EVENTS | REF_CONSTANT_EVENTS | PR_PAYLOAD_EVENTS

# Contexts that hold one value for the whole workflow. A group made only of
# these is static, whatever the event. The list is short on purpose: an
# unlisted context reads as UNKNOWN below, which keeps the group unflagged.
_CONSTANT_CONTEXTS = frozenset(
    {
        "github.workflow",
        "github.workflow_ref",
        "github.repository",
        "github.repository_owner",
        "github.repository_id",
        "github.event_name",
        "github.job",
        "github.actor",
        "github.action",
    }
)

# How a context behaves on one event: VARYING gives each ref its own group,
# CONSTANT gives every run of that event the same group, EMPTY drops out of an
# `||` chain, and UNKNOWN means this code cannot tell.
_VARYING, _CONSTANT, _EMPTY, _UNKNOWN = "varying", "constant", "empty", "unknown"

# One operand of an `||` chain: a dotted context path, or a single-quoted
# literal (GitHub escapes a quote inside a literal by doubling it).
_CONTEXT_ATOM = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_LITERAL_ATOM = re.compile(r"^'(?:[^']|'')*'$")


def declared_events(doc: object) -> frozenset[str]:
    """The event names a workflow's `on:` declares, for every `on:` spelling: a
    scalar (`on: push`), a list (`on: [push, schedule]`), and a mapping. An
    empty set means the triggers could not be read."""
    triggers = workflow_triggers(doc)
    if isinstance(triggers, str):
        return frozenset({triggers})
    if isinstance(triggers, (list, dict)):
        return frozenset(str(name) for name in triggers)
    return frozenset()


def _atom_state(atom: str, event: str) -> str:
    """How one `||` operand behaves on EVENT."""
    if _LITERAL_ATOM.match(atom):
        return _EMPTY if atom == "''" else _CONSTANT
    if not _CONTEXT_ATOM.match(atom):
        return _UNKNOWN
    if atom in _CONSTANT_CONTEXTS:
        return _CONSTANT
    if atom in ("github.run_id", "github.run_number"):
        return _VARYING  # a fresh value per run, on every event
    if event not in _KNOWN_EVENTS:
        return _UNKNOWN  # an event this table does not model — judge nothing
    if atom in ("github.ref", "github.ref_name"):
        if event in REF_VARYING_EVENTS:
            return _VARYING
        # A known event can still leave the ref unknown: the review events are
        # modelled for their PR number alone. Decline rather than guess.
        return _CONSTANT if event in REF_CONSTANT_EVENTS else _UNKNOWN
    if atom == "github.head_ref":
        return _VARYING if event in HEAD_REF_EVENTS else _EMPTY
    if atom == "github.event.number":
        return _VARYING if event in HEAD_REF_EVENTS else _EMPTY
    if atom.endswith("pull_request.number"):
        return _VARYING if event in PR_PAYLOAD_EVENTS else _EMPTY
    return _UNKNOWN


def _or_atoms(expr: str) -> list[str] | None:
    """The operands of EXPR when it is a plain `||` chain, else None.

    Only the chain shape is recognized. Any other expression — a comparison, a
    function call, `&&`, a parenthesis — returns None, which the caller reads as
    UNKNOWN and leaves alone. This is a recognizer, not a parser of GitHub's
    expression grammar: it either sees the one shape it knows or it declines.
    """
    atoms: list[str] = []
    current: list[str] = []
    quoted = False
    index = 0
    while index < len(expr):
        char = expr[index]
        if char == "'":
            quoted = not quoted
        if not quoted and expr.startswith("||", index):
            atoms.append("".join(current))
            current = []
            index += 2
            continue
        current.append(char)
        index += 1
    if quoted:
        return None  # an unterminated literal — the shape is not what it seems
    atoms.append("".join(current))
    stripped = [atom.strip() for atom in atoms]
    if any(not atom for atom in stripped):
        return None
    return stripped


def _span_state(expr: str, event: str) -> str:
    """How one `${{ … }}` span behaves on EVENT. An `||` chain takes the value of
    its FIRST operand that is not empty, so the walk stops on the first operand
    that is set."""
    atoms = _or_atoms(expr)
    if atoms is None:
        return _UNKNOWN
    for atom in atoms:
        state = _atom_state(atom, event)
        if state != _EMPTY:
            return state
    return _CONSTANT  # every operand is empty — the span contributes nothing


def _group_varies_on(group: str, event: str) -> bool:
    """True if GROUP still gives each ref its own value on EVENT."""
    return any(
        _span_state(span.group("expr"), event) in (_VARYING, _UNKNOWN)
        for span in _EXPR_SPAN.finditer(group)
    )


def group_has_per_ref_key(group: str) -> bool:
    """True if GROUP names a per-ref/per-PR/per-run key INSIDE a `${{ … }}`
    expression span. Outside a span the key is a literal: a group named
    `"github.ref-shared"` is one static string for every ref, so a bare
    substring match would fail open exactly on the workflows this guard exists
    to flag."""
    return any(
        key in _LITERAL_SPAN.sub(" ", span.group("expr"))
        for span in _EXPR_SPAN.finditer(group)
        for key in PER_REF_CONCURRENCY_KEYS
    )


def group_collapse_event(group: str, events: Iterable[str]) -> str | None:
    """The first declared event under which GROUP stops naming the ref, else None.

    A key inside the group is not the same as a key that HOLDS a value. GitHub
    leaves `github.head_ref` empty off a pull-request event. GitHub sets
    `github.ref` to the default branch on an event that carries no ref of its
    own, such as `workflow_run`, `issue_comment`, or `schedule`.

    Take the house group `${{ github.workflow }}-${{ github.head_ref ||
    github.ref }}` on a workflow that fires on `pull_request` and on
    `workflow_run`. A pull-request run puts the PR branch in the group. A
    `workflow_run` run puts `refs/heads/main` there, and so does every other
    `workflow_run` run of that workflow. Those runs all share one slot, which is
    the static-group hazard the caller reports.

    Three cases count as varying, so a group is only reported when its collapse
    is certain: an operand this code cannot classify, an expression that is not
    a plain `||` chain, and an event outside the table above.
    """
    return next(iter(group_collapse_events(group, events)), None)


def group_collapse_events(group: str, events: Iterable[str]) -> list[str]:
    """Every declared event under which GROUP stops naming the ref, sorted.

    `group_collapse_event` answers the same question for the first such event.
    A lint that must intersect the collapse set with the events a job can still
    RUN on needs all of them: a group keyed on `${{ github.ref }}` collapses on
    `pull_request_target` and on `schedule` alike, and a job whose `if:` admits
    only the second is still exposed.
    """
    return [event for event in sorted(events) if not _group_varies_on(group, event)]


# The events whose payload carries no `action` field, out of the set the table
# above models. A condition reading `github.event.action` is therefore false on
# each of them, so the condition restricts the job to the complement.
# `merge_group` is NOT one of them: its payload carries `checks_requested`, and
# reading it as actionless would let a per-PR group pass unreported on the merge
# queue, which is the one place a merge-queue run has no PR number at all.
_ACTIONLESS_EVENTS = frozenset(
    {
        "push",
        "create",
        "schedule",
        "workflow_dispatch",
        "fork",
        "gollum",
        "page_build",
        "public",
        "status",
    }
)

# A payload path, and the events whose payload carries its first segment. A
# condition that demands a value from one of these paths is false on every other
# event, which is how a job's `if:` excludes an event without naming it.
_PAYLOAD_EVENTS = (
    ("github.event.pull_request.", PR_PAYLOAD_EVENTS),
    ("github.event.workflow_run.", frozenset({"workflow_run"})),
    ("github.event.action", _KNOWN_EVENTS - _ACTIONLESS_EVENTS),
)

# `github.event_name` compared against a single-quoted literal, either order.
# Case-insensitive on both halves: GitHub compares two strings without regard to
# case, and it reads a context path the same way, so `GITHUB.EVENT_NAME ==
# 'SCHEDULE'` is true on a cron run.
_EVENT_NAME_CMP = re.compile(
    r"^(?:github\.event_name\s*(?P<op1>==|!=)\s*'(?P<lit1>[^']*)'"
    r"|'(?P<lit2>[^']*)'\s*(?P<op2>==|!=)\s*github\.event_name)$",
    re.IGNORECASE,
)

# `contains(fromJSON('["a", "b"]'), <term>)` — the list membership form.
_CONTAINS_FROMJSON = re.compile(
    r"^contains\s*\(\s*fromJSON\s*\(\s*'(?P<json>[^']*)'\s*\)\s*,"
    r"\s*(?P<term>[^,()]+?)\s*\)$",
    re.IGNORECASE,
)

# A payload path demanded to hold a value: `<path> == '<non-empty>'` or
# `<path> == true`. The polarity matters. GitHub reads an absent path as null,
# and `null == false` and `null != 'Bot'` are both TRUE, so only the positive
# form proves the payload is present.
# `github.event.action` compared against a single-quoted literal, either order.
_ACTION_CMP = re.compile(
    r"^(?:github\.event\.action\s*(?P<op1>==|!=)\s*'(?P<lit1>[^']*)'"
    r"|'(?P<lit2>[^']*)'\s*(?P<op2>==|!=)\s*github\.event\.action)$",
    re.IGNORECASE,
)

_PAYLOAD_DEMAND = re.compile(
    r"^(?P<path>github\.event\.[A-Za-z0-9_.-]+)\s*==\s*"
    r"(?:'(?P<lit>[^']+)'|(?P<true>true))$",
    re.IGNORECASE,
)


def _split_top_level(expr: str, operator: str) -> list[str]:
    """EXPR split on OPERATOR, ignoring occurrences inside quotes or parentheses."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    depth = 0
    index = 0
    while index < len(expr):
        char = expr[index]
        if char == "'":
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
        if not quoted and depth == 0 and expr.startswith(operator, index):
            parts.append("".join(current))
            current = []
            index += len(operator)
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return [part.strip() for part in parts]


def _strip_parens(term: str) -> str:
    """TERM with a wrapping pair of parentheses removed, repeatedly.

    `(a) && (b)` opens and closes with a parenthesis but is not wrapped by one
    pair, so the walk below refuses it: the leading `(` must close on the LAST
    character for the pair to wrap the whole term."""
    while term.startswith("(") and term.endswith(")"):
        quoted = False
        depth = 0
        for index, char in enumerate(term):
            if char == "'":
                quoted = not quoted
            elif not quoted and char == "(":
                depth += 1
            elif not quoted and char == ")":
                depth -= 1
                if depth == 0 and index != len(term) - 1:
                    return term  # the opening parenthesis closes early
        if depth != 0:
            return term  # unbalanced — not a shape to strip
        term = term[1:-1].strip()
    return term


def _named_match(literals: Iterable[str], value: str | None) -> bool | None:
    """Whether VALUE is one of LITERALS, matched the way GitHub matches: without
    regard to case. None when VALUE is unknown on this trigger."""
    if value is None:
        return None
    return value.casefold() in {literal.casefold() for literal in literals}


def _term_truth(term: str, trigger: "Trigger") -> bool | None:
    """Whether TERM is true on TRIGGER, or None when this reader cannot tell.

    Three shapes are read: a `github.event_name` or `github.event.action`
    comparison, `contains(fromJSON('[…]'), …)` membership over either, and a
    demand that a payload path hold a value. The payload demand can only prove
    FALSE — an event whose payload has no such path cannot satisfy it — because
    whether the value MATCHES is a runtime fact no static read holds.
    """
    match = _EVENT_NAME_CMP.match(term)
    if match:
        literal = match.group("lit1") or match.group("lit2") or ""
        equal = _named_match([literal], trigger.event)
        return (
            equal if (match.group("op1") or match.group("op2")) == "==" else not equal
        )

    match = _ACTION_CMP.match(term)
    if match:
        literal = match.group("lit1") or match.group("lit2") or ""
        if trigger.event.casefold() in _ACTIONLESS_EVENTS:
            return (match.group("op1") or match.group("op2")) != "=="
        equal = _named_match([literal], trigger.action)
        if equal is None:
            return None
        return (
            equal if (match.group("op1") or match.group("op2")) == "==" else not equal
        )

    match = _CONTAINS_FROMJSON.match(term)
    if match:
        try:
            members = json.loads(match.group("json"))
        except ValueError:
            return None
        if not isinstance(members, list) or not all(
            isinstance(item, str) and item for item in members
        ):
            return None
        inner = match.group("term").strip().casefold()
        if inner == "github.event_name":
            return _named_match(members, trigger.event)
        if inner == "github.event.action":
            if trigger.event.casefold() in _ACTIONLESS_EVENTS:
                return False
            return _named_match(members, trigger.action)
        return _payload_truth(inner, trigger)

    match = _PAYLOAD_DEMAND.match(term)
    if match:
        return _payload_truth(match.group("path").casefold(), trigger)
    return None


def _payload_truth(path: str, trigger: "Trigger") -> bool | None:
    """False when TRIGGER's payload cannot hold PATH at all, else None.

    Never True: the path being present says nothing about the value a demand
    compares it against, so this reader proves absence and nothing else.
    """
    for prefix, events in _PAYLOAD_EVENTS:
        if path == prefix or path.startswith(prefix):
            return None if trigger.event in events else False
    return None


def _expression_truth(expr: str, trigger: "Trigger") -> bool | None:
    """Whether EXPR is true on TRIGGER, in three-valued logic.

    `&&` is false as soon as one arm is false, whatever the others say; `||` is
    true as soon as one arm is true. Only when no arm settles it does an
    unreadable arm make the whole answer unknown. That asymmetry is the whole
    point: a caller asking "does this job SKIP here" must accept only a definite
    false, and gets one from a single readable arm.
    """
    expr = _strip_parens(expr.strip())
    for operator, decisive in (("||", True), ("&&", False)):
        arms = _split_top_level(expr, operator)
        if len(arms) > 1:
            values = [_expression_truth(arm, trigger) for arm in arms]
            if decisive in values:
                return decisive
            return None if None in values else not decisive
    return _term_truth(expr, trigger)


class Trigger(NamedTuple):
    """One way a workflow can start: an event, and the activity type when the
    workflow declares them. `action` is None when the event carries none, or
    when the workflow names no `types:` filter and GitHub's defaults apply."""

    event: str
    action: str | None = None


# The activity types GitHub sends for a pull-request event when the workflow
# names no `types:` filter of its own.
DEFAULT_PR_ACTIONS = ("opened", "synchronize", "reopened")


def declared_triggers(doc: object) -> list[Trigger]:
    """Every (event, activity type) pair a workflow's `on:` block admits.

    A pull-request event expands to one trigger per activity type, because the
    types are what a job's `if:` most often gates on and what tells a run that
    carries new code from a run that only re-labels the same commit. Every other
    event stays one trigger.
    """
    triggers = workflow_triggers(doc)
    if isinstance(triggers, str):
        names: dict[str, object] = {triggers: None}
    elif isinstance(triggers, list):
        names = {str(name): None for name in triggers}
    elif isinstance(triggers, dict):
        names = {str(name): value for name, value in triggers.items()}
    else:
        return []

    out: list[Trigger] = []
    for event, config in names.items():
        if event not in HEAD_REF_EVENTS:
            out.append(Trigger(event))
            continue
        types = config.get("types") if isinstance(config, dict) else None
        if isinstance(types, str):
            types = [types]  # GitHub normalizes a scalar filter to a one-item list
        if not isinstance(types, list) or not types:
            types = list(DEFAULT_PR_ACTIONS)
        out.extend(Trigger(event, str(action)) for action in types)
    return out


def job_skipped_triggers(
    if_value: object, triggers: Iterable[Trigger]
) -> list[Trigger]:
    """The TRIGGERS on which a job's `if:` is DEFINITELY false, so GitHub skips
    the job.

    Definite is the contract. A condition this reader cannot classify yields no
    entry, so the answer is an under-estimate and a caller that reports on a
    non-empty answer reports only what it can show.
    """
    expression = unwrap_expression(str(if_value or "")).strip()
    if not expression:
        return []  # no condition — the job runs on every trigger
    return [
        trigger
        for trigger in triggers
        if _expression_truth(expression, trigger) is False
    ]


def job_admitted_events(if_value: object, declared: Iterable[str]) -> frozenset[str]:
    """The DECLARED events on which a job's `if:` can still let the job run.

    An event survives when at least one of its triggers is not a definite skip,
    so the answer is an OVER-estimate: a job gated through a
    `needs.<gate>.outputs` value reads as admitting every event.
    """
    events = frozenset(str(name) for name in declared)
    triggers = [
        trigger
        for trigger in declared_triggers({"on": {event: None for event in events}})
        if trigger.event in events
    ]
    skipped = set(job_skipped_triggers(if_value, triggers))
    return frozenset(
        event
        for event in events
        if any(t.event == event and t not in skipped for t in triggers)
    )


def group_separates_triggers(group: str, first: Trigger, second: Trigger) -> bool:
    """True when GROUP is certain to hold different values on the two triggers.

    A span separates them when it reads something the two triggers disagree
    about: the run id (never shared), the event name across two events, the
    activity type across two types, or a key whose STATE differs — empty on one
    event and naming the ref on the other.

    False is the answer that matters and it means "can share": two runs of the
    same pull request, one on `synchronize` and one on `labeled`, put the same
    branch in a `${{ github.head_ref }}` group, so the second run's job takes the
    slot the first one's job is queued in.
    """
    for span in _EXPR_SPAN.finditer(group):
        expr = _LITERAL_SPAN.sub(" ", span.group("expr")).casefold()
        if any(key in expr for key in ("github.run_id", "github.run_number")):
            return True
        if "github.event_name" in expr and first.event != second.event:
            return True
        if "github.event.action" in expr and first.action != second.action:
            return True
        states = (
            _span_state(span.group("expr"), first.event),
            _span_state(span.group("expr"), second.event),
        )
        if _UNKNOWN not in states and states[0] != states[1]:
            return True
    return False


def group_is_per_ref(group: str, events: Iterable[str] = ()) -> bool:
    """True if a concurrency `group:` expression names the ref on every event
    EVENTS lists — meaning a superseding run is always the same ref's newer run,
    which re-reports, so the group cannot strand a required check.

    Pass the workflow's declared events (see `declared_events`) to get the
    second half of the question answered: a key that is EMPTY or fixed on one of
    those events collapses the group to a static string for every run of that
    event. With no events the key's presence is all that is judged.
    """
    if not group_has_per_ref_key(group):
        return False
    return group_collapse_event(group, events) is None


def static_group_reason(group: str, events: Iterable[str]) -> str:
    """The opening sentence a concurrency lint reports for a static GROUP.

    One SSOT: both lints describe the same group with the same words, and a
    group that goes static only on one of its events names that event.
    """
    collapsed = (
        group_collapse_event(group, events) if group_has_per_ref_key(group) else None
    )
    if collapsed is None:
        return (
            "workflow-level concurrency.group is static (no github.ref / "
            "github.head_ref key)."
        )
    return (
        "workflow-level concurrency.group collapses to one static string on a "
        f"'{collapsed}' run. Its per-ref key is empty or fixed on that event, so "
        f"every '{collapsed}' run of this workflow shares one slot."
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
            for token in scan(text)
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


def yaml_comment_view(text: str) -> list[str]:
    """TEXT's lines with everything that is not a YAML COMMENT blanked out.

    An opt-out is a comment by contract, and a raw line scan cannot hold that
    line: `name: "# some-check-ok: example"` is a string VALUE, and honouring it
    would let any step turn a check off by naming it. The comment spans come from
    PyYAML's own scanner (`strip_yaml_comments`), so what counts as a comment
    here is what GitHub parses as one. Line boundaries survive, so a caller's
    line numbers still index this view.
    """
    blanked = strip_yaml_comments(text)
    view = "".join(
        original if original != stripped or original in _LINE_BOUNDARY else " "
        for original, stripped in zip(text, blanked)
    )
    return view.splitlines()


def default_run_shell(*scopes: object) -> str | None:
    """The first `defaults.run.shell` in SCOPES, or None when none sets one.

    GitHub resolves a step's shell in one order: the step's own `shell:`, then
    the job's `defaults.run.shell`, then the workflow's. The caller owns the
    step, and passes the remaining scopes here in that order. One definition,
    because two lints ask this and a second encoding of the precedence would
    drift. Tolerant of a null or non-mapping `defaults:`, which a workflow can
    hold and which is actionlint's finding, not this pack's.
    """
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        defaults = scope.get("defaults")
        run = defaults.get("run") if isinstance(defaults, dict) else None
        shell = run.get("shell") if isinstance(run, dict) else None
        if isinstance(shell, str):
            return shell
    return None


_SHELL_EXPRESSION = re.compile(r"\$\{\{")


def shell_program(shell: str) -> str | None:
    """The lower-case basename of the program SHELL starts, or None when a
    `${{ }}` expression hides it.

    Windows separators count, because a `cmd` template on a Windows runner spells
    its path with backslashes. One definition, for the reason `default_run_shell`
    above gives: three lints ask this question.
    """
    words = shell.strip().split()
    if not words or _SHELL_EXPRESSION.search(words[0]):
        return None
    name = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def step_span_ends(steps: list[dict], last_line: int) -> dict[int, int]:
    """The last line of each step's block, keyed by the step's own line.

    A step ends where the next one starts, and the final step ends at LAST_LINE.
    The caller supplies LAST_LINE as the end of the CONTAINER — the job's block
    in a workflow, the file in a composite action — never the end of the
    document. A span that ran to the next JOB's first step would swallow that
    job's header, so an opt-out written for one job's step would suppress the
    previous job's last step, which is a false green.

    The span is what an opt-out may be written inside (see ``annotation_window``).
    """
    starts = sorted(
        line for step in steps if isinstance(line := step.get("__line__"), int)
    )
    ends = {start: nxt - 1 for start, nxt in zip(starts, starts[1:])}
    if starts:
        ends[starts[-1]] = last_line
    return ends


def container_block_end(
    blocks: dict[str, tuple[int, str]], name: str, last_line: int
) -> int:
    """The last source line of the job named NAME, or LAST_LINE when BLOCKS has
    no entry for it — a composite action's `runs:` block, which is not a job."""
    key_line, block = blocks.get(name, (0, ""))
    return key_line + len(block.splitlines()) - 1 if block else last_line


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


def _matrix_entries(value: object) -> list[dict]:
    """The `include:`/`exclude:` entries that bind keys readable without a run.

    A dynamic `${{ fromJSON(...) }}` is a string, and a malformed entry is not
    a mapping. Neither states a key that can be read here, so both drop out —
    the same rule the axis filter applies to a dynamic axis value. Iterating
    either would walk it one character (or one scalar) at a time.
    """
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


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

    for ex in _matrix_entries(matrix.get("exclude")):
        combos = [c for c in combos if not all(c.get(k) == v for k, v in ex.items())]

    includes = _matrix_entries(matrix.get("include"))
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
    doc = safe_load(text)
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
