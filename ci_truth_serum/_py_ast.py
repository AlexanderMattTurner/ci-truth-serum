"""Python source as a tree, for the lints whose question is about Python STRUCTURE.

The shell lints ask ``_bash_ast`` "is this a command or a string a command
prints?"; the Python lints ask the same shape of question — "is this name an
assignment TARGET, or a value on the right?", "where does this call's argument
list end?", "is this ``shutil.which`` executed, or spelled inside a ``reason=``
string?" — and the text answers are wrong in the same two directions. A regex
locating a plain ``=`` cannot see a multi-line assignment or an ``=`` inside a
string literal; a hand-written balanced-paren matcher with its own quote state is
the "character walk tracking whether you are inside ``'`` or ``\\"``" that
`.claude/rules/shell-lint-parsing.md` names as the tell. Python ships its own
grammar in the stdlib, so both of those are one ``ast.parse`` away.

``trees`` is the one entry point. It hands back parsed fragments whose node line
numbers already index the SOURCE's physical lines, so a caller walks nodes and
reports ``node.lineno`` with no offset bookkeeping of its own.

Line numbers count ``\\n`` — what ``ast`` itself reports. ``str.splitlines`` also
breaks on ``\\v``, ``\\f`` and U+2028, which the grammar does not, so a caller
enumerating with it would slide every later line number off by the difference.
"""

import ast
import re
import warnings

# Every line ending Python's parser counts as one. `str.split("\n")` counts only
# the last of them, so a file written with CR or CRLF endings would have `ast`
# line numbers that index a DIFFERENT set of lines than a caller enumerating the
# raw text — which is how an opt-out annotation gets read off the wrong line, or a
# finding lands past the end of the file. `lines` below is the one enumeration
# that agrees with what `trees` reports.
_LINE_ENDING = re.compile(r"\r\n?")

# A line that OPENS a block (`with redirect_stdout(buf):`, `if x:`) is not a
# statement on its own, so the per-line recovery below re-tries it with the
# smallest body that completes it. Only reached when the whole file already
# failed to parse.
_BLOCK_BODY = "\n    pass"


def _parse(source: str) -> ast.Module:
    """SOURCE as a tree, with ``ast``'s advisory warnings kept off stderr.

    A lint's stderr carries its findings — `path:line: message` lines a reader
    and CI parse — so a ``SyntaxWarning: invalid decimal literal`` emitted while
    merely INSPECTING a file would interleave a compiler diagnostic with them,
    for a file the caller is not compiling. The warning belongs to whatever runs
    the code; the syntax ERRORS this can raise are handled by the callers below.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source)


def _fragment(line: str) -> "ast.Module | None":
    """LINE parsed on its own — as a statement, or as a block header completed
    with a ``pass`` body — or None when it is not Python at all."""
    for candidate in (line, line + _BLOCK_BODY):
        try:
            return _parse(candidate)
        except (SyntaxError, ValueError):
            continue
    return None


def lines(source: str) -> list[str]:
    """SOURCE's physical lines, numbered the way ``trees`` numbers its nodes — so
    ``lines(source)[node.lineno - 1]`` is the line the node is on, whatever line
    endings the file was written with. Every caller that reads a line by number
    (an opt-out annotation, a range check) enumerates through here."""
    return _LINE_ENDING.sub("\n", source).split("\n")


def _line_trees(source: str) -> list[ast.Module]:
    """Every line of SOURCE that parses in isolation, as its own tree, line
    numbers shifted to the line it came from."""
    out: list[ast.Module] = []
    for lineno, line in enumerate(lines(source), 1):
        stripped = line.strip()
        if not stripped:
            continue
        tree = _fragment(stripped)
        if tree is None:
            continue
        ast.increment_lineno(tree, lineno - 1)
        out.append(tree)
    return out


def trees(source: str) -> list[ast.Module]:
    """SOURCE as parsed trees whose line numbers index SOURCE's own lines.

    Normally one tree — the whole module. SOURCE that does not parse (a syntax
    error, a half-written edit, a snippet that is a fragment rather than a
    module) falls back to parsing each line in isolation rather than reporting
    nothing at all: "no findings" on an unparseable file is the false green this
    pack exists to catch, and the syntax error itself belongs to other tooling.

    A ``ValueError`` (source carrying a NUL byte, which ``ast`` refuses outright)
    is handled the same way as a syntax error — it says the same thing about the
    text, and the per-line pass still reaches every line that is real Python.
    """
    normalized = _LINE_ENDING.sub("\n", source)
    try:
        return [_parse(normalized)]
    except (SyntaxError, ValueError):
        return _line_trees(normalized)


def name_of(node: ast.AST) -> str | None:
    """The dotted spelling of NODE when it names something — ``sys.stdout`` for an
    attribute chain, ``which`` for a bare name — else None.

    An expression that is neither (a call's result, a subscript, a literal) has no
    stable spelling to match against, and answering None keeps a caller from
    guessing one out of the source text."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = name_of(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def re_bindings(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """How TREE's module reaches ``re``, so an aliased import cannot hide a call.

    Returns (module_names, func_names). ``module_names`` are the local names bound
    to the module itself (``import re`` -> ``re``; ``import re as x`` -> ``x``).
    ``func_names`` maps a locally-bound name to the ``re`` function it names
    (``from re import compile`` -> ``compile: compile``; ``... import sub as s``
    -> ``s: sub``). Bare ``re`` is always a module name, so a file that calls
    ``re.sub`` with no visible ``import re`` is still inspected.

    Shared by every lint that matches ``re.*`` calls: one resolver means two lints
    cannot disagree about which spellings count.
    """
    module_names = {"re"}
    func_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "re":
                    module_names.add(alias.asname or "re")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "re" and node.level == 0:
                for alias in node.names:
                    func_names[alias.asname or alias.name] = alias.name
    return module_names, func_names


def re_call_target(
    func: ast.expr,
    module_names: set[str],
    func_names: dict[str, str],
    wanted: frozenset[str],
) -> str | None:
    """The ``re`` function FUNC invokes, when it is one of WANTED, else None.

    Both spellings: the attribute form (``re.sub``, or an alias ``x.sub`` where
    ``x`` is bound to the module) and the bare-name form (``sub`` imported with
    ``from re import sub``)."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in module_names and func.attr in wanted:
            return func.attr
    elif isinstance(func, ast.Name):
        mapped = func_names.get(func.id)
        if mapped in wanted:
            return mapped
    return None
