#!/usr/bin/env python3
"""Ban an all-shaped verdict over one unpaged page of a GitHub listing.

PROBLEM CLASS — GitHub answers a listing endpoint with an envelope,
``{"total_count": 137, "jobs": [ … ]}``, and one page holds at most 100 rows.
Code that asks "did EVERY job pass?" of that page reads a run with more rows as
a pass, because the rows that failed are on page two. The verdict is a green
that nobody earned, and no check anywhere goes red.

One worked case. A pull request's decide step picked a memo base from the newest
commit whose jobs all concluded ``success``. The run it read had 137 jobs, the
answer carried 100, and every one of those 100 had passed. The step took that
commit as verified, so the expensive checks did not run again on the later
commits — a skipped check that reads on the pull request as an inherited green.
A second site made the same read one week later.

The lint fires on a scope that holds all four of:

  * a GitHub API read — an ``api.github.com`` or ``/repos/…`` path, ``gh api``,
    ``octokit``, or a call this pack names (``github_request``);
  * a read of a listing envelope key (``jobs``, ``check_runs``, …);
  * an all-shaped verdict over it — ``all(…)``, ``not any(…)``, ``.every(…)``,
    ``!….some(…)``;
  * no evidence of paging.

Paging evidence is generous by design, because a false finding here costs a
reader more than a missed one: a mention of ``total_count``, ``--paginate``, a
``per_page``-and-``page`` walk, a ``link`` header, a call whose name carries
``paginate`` or ``pages``, or any loop in the scope. A ``per_page=100`` alone is
not paging — it is the parameter that makes the truncated answer look complete.

The scope is the FILE, and the four signals need not share a function. The
defect this lint was written from is split across two: one function fetches the
page, a second takes the list as an argument and reduces it, and neither one
holds all four signals. Containment therefore stands in for data flow, so a file
that reads the API, names a listing key and reduces some list with ``all`` is
judged even when a reader can see the two lists are different. That is the one
approximation here, and it is why the opt-out takes a reason.

Each language is read through its own grammar (``_cts_py_ast``, ``_cts_js_ast``). A text
scan would count a key inside a string literal or a comment as a read, and an
opt-out spelled inside a string would disarm the lint that reads it.

Shell is not scanned. There the same defect spans two statements — ``gh api``
captures a document, and a later ``jq`` call reduces it — which is a data-flow
question rather than a structural one, so this lint would answer it by guess.

A site that must reduce one page opts out with a same-line or
preceding-line ``# allow-unpaged-all: <reason>`` (``// allow-unpaged-all:`` in
JavaScript). Invoked by pre-commit with the staged Python and JavaScript paths.
"""

import ast
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
# `iter_nodes` walks any tree-sitter tree, whichever grammar built it — the same
# reason `check_replacement_expansion` reaches into `_cts_bash_ast` for it.
from _cts_bash_ast import iter_nodes  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_js_ast import is_js_source, parse  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import annotated_near, is_python_source  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import run_file_cli, run_source_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_py_ast import lines, name_of, trees  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-unpaged-all"

MESSAGE = (
    "reduces a GitHub listing with an all-shaped verdict and never pages; "
    "one page holds 100 rows, so a longer listing reads as a pass — page to "
    f"`total_count`, or annotate `# {OPT_OUT}: <reason>`"
)

# The keys GitHub answers a `total_count` envelope under. Each one is the array
# a caller reduces, and each one is truncated to the page size.
ENVELOPE_KEYS = frozenset(
    {
        "artifacts",
        "check_runs",
        "check_suites",
        "jobs",
        "workflow_runs",
        "workflows",
    }
)

# A name or string anywhere in the scope that shows the author knows the answer
# arrives in pages. `per_page` is absent on purpose: it sets the page size and
# leaves the truncation exactly where it was.
PAGING_EVIDENCE = (
    "total_count",
    "paged",
    "paginate",
    "pagination",
    "next_page",
    "has_next",
    "page_count",
    "last_page",
    "all_pages",
    "pages",
    "link",
)

# The scope must also SHOW a GitHub API read. Without this, `jobs` matched every
# test that walks a workflow YAML document, whose top-level key is also `jobs`:
# that shape was all 17 findings of the first dogfood run over this repo and
# agent-glovebox, and none of them touched the API.
API_EVIDENCE = (
    "api.github.com",
    "repos/",
    "actions/runs",
    "gh api",
    "octokit",
    "github_request",
    "gh_json",
    "graphql",
)

_PY_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module)
# Only a `while` counts as a pager's loop. A `for` is what the caller writes to
# walk the ONE page it already holds, so counting it would excuse every finding
# this lint exists to make.
_PY_LOOPS = (ast.While,)
_JS_FUNCTIONS = frozenset(
    {
        "arrow_function",
        "function_declaration",
        "function_expression",
        "generator_function",
        "generator_function_declaration",
        "method_definition",
        "program",
    }
)
_JS_LOOPS = frozenset({"do_statement", "while_statement"})
_JS_WORDS = frozenset(
    {"identifier", "property_identifier", "string", "string_fragment"}
)


def _pages(words: set[str], has_loop: bool) -> bool:
    """True when WORDS or a loop shows the scope walks more than one page."""
    if has_loop:
        return True
    return _mentions(words, PAGING_EVIDENCE)


def _mentions(words: set[str], evidence: tuple[str, ...]) -> bool:
    """True when any word in WORDS carries one of the EVIDENCE substrings."""
    return any(token in word for word in words for token in evidence)


# ── Python ───────────────────────────────────────────────────────────────


def _py_reduction(node: ast.AST) -> bool:
    """True when NODE asks one question of a WHOLE list. `any` counts beside
    `all`: "did any job fail?" over one page misses the failures on page two,
    which is the same wrong answer in the other direction."""
    return isinstance(node, ast.Call) and name_of(node.func) in ("all", "any")


def _py_reads_envelope(node: ast.AST) -> bool:
    """True when NODE reads a listing envelope's array out of a response."""
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value in ENVELOPE_KEYS
    if isinstance(node, ast.Attribute):
        return node.attr in ENVELOPE_KEYS
    if isinstance(node, ast.Call) and (name_of(node.func) or "").endswith(".get"):
        first = node.args[0] if node.args else None
        return isinstance(first, ast.Constant) and first.value in ENVELOPE_KEYS
    return False


class PyWords(NamedTuple):
    """Every name, attribute, keyword and string literal a module holds, split
    by whether it lives in code or a docstring, plus whether it loops."""

    code: set[str]
    prose: set[str]
    has_loop: bool


def _py_words(tree: ast.Module) -> PyWords:
    """Every name, attribute, keyword and string literal in TREE, plus whether
    TREE holds a `while` loop, as (code words, docstring words, loop).

    The two sets are read for different questions. An API read is CODE, so a
    module that names `api.github.com` only in its own header made no read — one
    such file was a finding over agent-glovebox's 1751 files. Paging evidence
    reads both, because a docstring that explains the paging is still an author
    who knew, and this lint would rather miss than shout."""
    code: set[str] = set()
    prose: set[str] = set()
    has_loop = False
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        has_loop = has_loop or isinstance(node, _PY_LOOPS)
        if isinstance(node, ast.Name):
            code.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            code.add(node.attr.lower())
        elif isinstance(node, ast.keyword) and node.arg:
            code.add(node.arg.lower())
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            (prose if id(node) in docstrings else code).add(node.value.lower())
    return PyWords(code, prose, has_loop)


def _py_violations(source: str) -> list[int]:
    """1-based lines in SOURCE that reduce an unpaged listing."""
    hits = []
    for tree in trees(source):
        nodes = list(ast.walk(tree))
        if not any(_py_reads_envelope(node) for node in nodes):
            continue
        code, prose, has_loop = _py_words(tree)
        if not _mentions(code, API_EVIDENCE) or _pages(code | prose, has_loop):
            continue
        hits += [node.lineno for node in nodes if _py_reduction(node)]
    return hits


# ── JavaScript ───────────────────────────────────────────────────────────


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _walk(node):
    """Every descendant of NODE (inclusive). `iter_nodes` filters by type, and
    these two walks want every node."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _js_method(node) -> str | None:
    """The method name a call invokes, or None when it calls something else."""
    if node.type != "call_expression":
        return None
    function = node.child_by_field_name("function")
    if function is None or function.type != "member_expression":
        return None
    method = function.child_by_field_name("property")
    return _text(method) if method is not None else None


def _js_reads_envelope(node) -> bool:
    """True when NODE reads a listing envelope's array out of a response."""
    if node.type == "member_expression":
        member = node.child_by_field_name("property")
        return member is not None and _text(member) in ENVELOPE_KEYS
    if node.type == "subscript_expression":
        index = node.child_by_field_name("index")
        return index is not None and _text(index).strip("'\"`") in ENVELOPE_KEYS
    return False


def _js_words(root) -> tuple[set[str], bool]:
    """Every identifier, property and string in ROOT, plus whether ROOT holds a
    `while` loop."""
    words: set[str] = set()
    has_loop = False
    for node in _walk(root):
        if node.type in _JS_LOOPS:
            has_loop = True
        elif node.type in _JS_WORDS:
            words.add(_text(node).strip("'\"`").lower())
    return words, has_loop


def _js_violations(source: str, path: str) -> list[int]:
    """1-based lines in SOURCE that reduce an unpaged listing."""
    root = parse(source, path)
    if not any(_js_reads_envelope(node) for node in _walk(root)):
        return []
    words, has_loop = _js_words(root)
    if not _mentions(words, API_EVIDENCE) or _pages(words, has_loop):
        return []
    return [
        node.start_point[0] + 1
        for node in iter_nodes(root, "call_expression")
        if _js_method(node) in ("every", "some")
    ]


def violations(text: str, path: str) -> list[int]:
    """1-based lines in TEXT that reduce an unpaged GitHub listing, without an
    opt-out. PATH picks the grammar, and each grammar brings its own line
    enumeration: ``trees`` normalizes CR and CRLF, and tree-sitter breaks on
    ``\\n`` alone."""
    if is_js_source(path):
        hits, physical = _js_violations(text, path), text.split("\n")
    elif is_python_source(path):
        hits, physical = _py_violations(text), lines(text)
    else:
        return []
    return sorted(
        lineno
        for lineno in set(hits)
        if lineno <= len(physical) and not annotated_near(physical, lineno, OPT_OUT)
    )


def main(argv: list[str]) -> int:
    return run_source_checks(argv, violations, MESSAGE)


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))
