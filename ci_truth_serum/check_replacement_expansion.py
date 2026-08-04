#!/usr/bin/env python3
"""Ban an assembled string in a replacement position.

``String.prototype.replace``/``replaceAll`` and ``re.sub``/``re.subn`` do not
insert their replacement argument literally. Each one re-parses it for
substitution patterns first: JavaScript expands ``$$``, ``$&``, ``$'``, "$`" and
``$1``; Python expands ``\\1``, ``\\g<name>`` and the other backslash escapes. A
replacement written as a literal at the call site is deliberate. A replacement
ASSEMBLED from other values is not: whatever those values hold reaches the
pattern parser, and one of these characters silently rewrites the output.

The concrete failure this pack was built from: ``assemble-changelog.mjs`` inserted
a release section with ``changelog.replace(MARKER, `${MARKER}\\n\\n${body}`)``. One
changelog fragment quoted a shell ``$'…'`` string. In a replacement, ``$'`` means
"the text after the match", so the assembler copied the rest of CHANGELOG.md into
the middle of the new section. The release PR that carried it could not pass any
markdown lint, and the release stopped.

The fix is a FUNCTION replacer, which inserts its return value verbatim:
``replace(marker, () => insert)`` in JavaScript, ``re.sub(pattern, lambda _:
insert, text)`` in Python.

Scope is deliberately NARROW, so a finding is always a real one:

- The replacement must be a string this file builds — a template literal with an
  interpolation, an f-string, or a concatenation with a string literal in it.
- A name counts when the same file binds it to one of those. A name bound
  somewhere this file cannot see stays clean: in both languages a replacement may
  legitimately BE a function, and nothing here can tell the two apart.
- A plain literal is never reported. The author can read every character of it.

A call that must pass an assembled string (the replacement is known to hold no
pattern character, or the expansion is the point) opts out with a same-line or
immediately-preceding-line ``allow-replacement-expansion: <reason>`` comment.

Structure is read with each language's own grammar — ``_js_ast`` (tree-sitter)
and ``_py_ast`` (stdlib ``ast``) — never with a text scan, per
``.claude/rules/shell-lint-parsing.md``. "Is this argument the replacement?" and
"does this template interpolate?" are questions about the tree.

Invoked by pre-commit with the staged JavaScript/TypeScript/Python files.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# `iter_nodes` walks any tree-sitter tree, whichever grammar built it — the same
# reason `_comments` reaches into `_bash_ast` for it.
from _bash_ast import iter_nodes  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _js_ast import is_js_source, parse  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import annotated_near, is_python_source, run_file_cli, run_source_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _py_ast import lines, name_of, re_bindings, re_call_target, trees  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-replacement-expansion"

MESSAGE = (
    "passes an assembled string as a replacement, which is re-parsed for "
    "substitution patterns ($& $' $1 in JS, \\1 \\g<> in Python); pass a "
    f"function replacer, or annotate `{OPT_OUT}: <reason>`"
)

# The JavaScript methods whose SECOND argument is the replacement.
_JS_METHODS = frozenset({"replace", "replaceAll"})
# The `re` substitution functions, and where the replacement sits in a positional
# call: `re.sub(pattern, repl, string)` holds it second, a compiled pattern's
# `pattern.sub(repl, string)` first. Both spellings reach the same code in CPython.
_RE_FUNCS = frozenset({"sub", "subn"})
_MODULE_POSITION = 1
_METHOD_POSITION = 0


# ── JavaScript / TypeScript ──────────────────────────────────────────────
def _js_assembled(node) -> bool:
    """True when NODE builds a string out of other values.

    A template literal qualifies only when it INTERPOLATES: `` `plain` `` is a
    literal the author reads in full, `` `a${b}` `` carries whatever `b` holds
    into the pattern parser. A `+` chain qualifies when a string literal or a
    template sits anywhere in it, which is what makes the result a string rather
    than a number."""
    if node.type == "template_string":
        return any(iter_nodes(node, "template_substitution"))
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        if operator is None or operator.text != b"+":
            return False
        return any(
            iter_nodes(node, "string", "template_string")
        ) and not _js_literal_only(node)
    return False


def _js_literal_only(node) -> bool:
    """True when NODE is a literal string, or a `+` chain of nothing else.

    `"a" + "b"` is one literal the author split over two tokens. The walk
    RECURSES, because a chain nests to the left: in `("a" + b) + "c"` the outer
    node's own children are a group and a literal, and only the inner one carries
    the name that makes the whole expression an assembly."""
    if node.type == "string":
        return True
    if node.type == "template_string":
        return not any(iter_nodes(node, "template_substitution"))
    if node.type == "binary_expression":
        return all(_js_literal_only(child) for child in node.named_children)
    return False


def _js_assembled_names(root) -> set[str]:
    """Every identifier this file binds to an assembled string.

    Both binding forms: a declaration (`const insert = `…${x}`;`) and a plain
    assignment (`insert = …`). One level only — a name bound to another name is
    not followed, because the second hop is where a function reference becomes
    indistinguishable from a string."""
    names = set()
    for node in iter_nodes(root, "variable_declarator", "assignment_expression"):
        target = node.child_by_field_name("name") or node.child_by_field_name("left")
        value = node.child_by_field_name("value") or node.child_by_field_name("right")
        if target is not None and target.type == "identifier" and value is not None:
            if _js_assembled(value):
                names.add(target.text.decode("utf-8", "replace"))
    return names


def _js_violations(source: str, path: str) -> list[int]:
    """1-based lines in SOURCE whose `replace`/`replaceAll` takes an assembled
    replacement."""
    root = parse(source, path)
    assembled = _js_assembled_names(root)
    hits = []
    for call in iter_nodes(root, "call_expression"):
        function = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if function is None or arguments is None:
            continue
        if function.type != "member_expression":
            continue
        method = function.child_by_field_name("property")
        if method is None or method.text.decode("utf-8", "replace") not in _JS_METHODS:
            continue
        args = [arg for arg in arguments.named_children if arg.type != "comment"]
        if len(args) < 2:
            continue
        replacement = args[1]
        named = replacement.type == "identifier" and (
            replacement.text.decode("utf-8", "replace") in assembled
        )
        if named or _js_assembled(replacement):
            hits.append(call.start_point[0] + 1)
    return hits


# ── Python ───────────────────────────────────────────────────────────────
def _py_assembled(node: ast.expr) -> bool:
    """True when NODE builds a string out of other values — an f-string, a `%`
    format, or a `+` concatenation with a string literal in it."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _py_has_str_leaf(node) and not _py_literal_only(node)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in {"format", "join"}:
            return True
    return False


def _py_has_str_leaf(node: ast.expr) -> bool:
    """True when a `+`/`%` chain has a string somewhere in it, which is what makes
    the whole expression a string rather than a number."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _py_has_str_leaf(node.left) or _py_has_str_leaf(node.right)
    return False


def _py_literal_only(node: ast.expr) -> bool:
    """True when NODE is a constant, or a `+`/`%` chain of nothing else — the
    author reads every character of it. The walk RECURSES for the reason
    ``_js_literal_only`` gives: the chain nests, and only an inner node carries
    the name that makes the whole expression an assembly."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _py_literal_only(node.left) and _py_literal_only(node.right)
    return False


def _py_assembled_names(tree: ast.Module) -> set[str]:
    """Every plain name this module binds to an assembled string (see
    ``_js_assembled_names`` for why the walk stops at one hop)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            targets, value = [node.target], node.value
        else:
            continue
        if value is not None and _py_assembled(value):
            names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def _compiled_names(tree: ast.Module) -> set[str]:
    """Every name this module binds to a compiled pattern, so `NAME.sub(…)` can be
    told from any other object's `.sub`."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):  # `SPLIT: Pattern[str] = re.compile(…)`
            targets, value = [node.target], node.value
        else:
            continue
        if not isinstance(value, ast.Call):
            continue
        if (name_of(value.func) or "").split(".")[-1] != "compile":
            continue
        names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def _repl_argument(
    node: ast.Call,
    modules: set[str],
    functions: dict[str, str],
    compiled: set[str],
) -> ast.expr | None:
    """The replacement argument of a substitution call, or None when NODE is not
    one.

    Three spellings, all the same call: the module function however it is imported
    (``re.sub``, an aliased ``x.sub``, a bare ``sub`` from ``from re import sub``),
    and a compiled pattern's method. MODULES/FUNCTIONS come from ``re_bindings``;
    COMPILED names the module's own ``re.compile`` results, which is what tells
    ``PATTERN.sub`` apart from any other object's ``.sub``."""
    dotted = (name_of(node.func) or "").split(".")
    if re_call_target(node.func, modules, functions, _RE_FUNCS) is not None:
        position = _MODULE_POSITION
    elif len(dotted) == 2 and dotted[0] in compiled and dotted[1] in _RE_FUNCS:
        position = _METHOD_POSITION
    else:
        return None
    for keyword in node.keywords:
        if keyword.arg == "repl":
            return keyword.value
    return node.args[position] if len(node.args) > position else None


def _py_violations(source: str) -> list[int]:
    """1-based lines in SOURCE whose `re.sub`/`re.subn` takes an assembled
    replacement."""
    hits = []
    for tree in trees(source):
        assembled = _py_assembled_names(tree)
        compiled = _compiled_names(tree)
        modules, functions = re_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            replacement = _repl_argument(node, modules, functions, compiled)
            if replacement is None:
                continue
            named = isinstance(replacement, ast.Name) and replacement.id in assembled
            if named or _py_assembled(replacement):
                hits.append(node.lineno)
    return hits


def violations(text: str, path: str) -> list[int]:
    """1-based lines in TEXT that pass an assembled replacement, without an
    opt-out. PATH picks the grammar; a path of neither language has none.

    Each language brings its own line enumeration, because each parser counts
    line endings its own way. ``trees`` normalizes CR and CRLF first and reports
    against that, which is what ``lines`` reproduces. tree-sitter parses the raw
    bytes and breaks on ``\\n`` alone. Enumerating either one with the other's
    rule slides the annotation lookup onto the wrong line.
    """
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
