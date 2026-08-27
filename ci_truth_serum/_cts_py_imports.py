"""What a Python entry point imports, resolved the way `python3 script.py` does.

PROBLEM CLASS — "which files on disk does this Python entry point need". A
script run through the plain interpreter (no package, no virtualenv) reaches a
sibling module through its own directory, and reaches any other module only
through a directory it puts on `sys.path` itself. A caller that resolves
imports against the repo root instead (the way `pytest` does) reads
`from _lockfiles import x` as a third-party name and drops the file — the
false negative `check_sparse_checkout_closure` exists to close.

The walk counts every import, including one inside a function body: the file
must exist on disk whenever that code path runs. Nothing here imports or
executes what it reads — parsing is enough, so a caller may run this against a
tree with no dependencies installed.
"""

import ast
import functools
import re
import sys
from collections.abc import Sequence
from pathlib import Path

# Any spelling of the interpreter, by the basename of the word: `python3`,
# `python3.12`, `/usr/bin/python3`, and `.venv/bin/python` all run the same file.
_PY_INTERPRETER = re.compile(r"python[0-9.]*")


def interpreter_scripts(words: Sequence[str | None]) -> list[str]:
    """The `.py` paths a command's WORDS hand to a Python interpreter.

    The script is the first `.py` word AFTER the interpreter word, not the
    next word: `python3 -I x.py` puts an option between the two. A `-m`
    before it names a MODULE to run, so the `.py` that follows is that
    module's own argument rather than a file the interpreter imports
    (`python3 -m pytest tests/x.py` runs pytest). A word an expansion decides
    reads as None and ends the search, since the text names no real file.
    """
    found = []
    for index, word in enumerate(words):
        if word is None or not _PY_INTERPRETER.fullmatch(word.rsplit("/", 1)[-1]):
            continue
        for candidate in words[index + 1 :]:
            if candidate is None or candidate == "-m":
                break
            if candidate.endswith(".py"):
                found.append(candidate)
                break
    return found


def _fold_path(node: ast.expr, env: dict[str, Path], importer: Path) -> Path | None:
    """NODE as a directory, over the `sys.path` expressions a script writes.

    `__file__` folds to IMPORTER, and a bare name folds to whatever an
    earlier assignment bound it to. `.parents[N]` folds like a run of
    `.parent`. Anything else folds to None, so an expression this cannot
    read drops a directory instead of inventing one.
    """
    if isinstance(node, ast.Name):
        return importer if node.id == "__file__" else env.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _fold_path(node.value, env, importer)
        return base.parent if base else None
    if isinstance(node, ast.Subscript):
        index = node.slice
        base = (
            _fold_path(node.value.value, env, importer)
            if isinstance(node.value, ast.Attribute) and node.value.attr == "parents"
            else None
        )
        # Bounded at both ends by the folded directory's own depth: `parents`
        # raises past it, so an index this cannot place drops the directory
        # like any other expression it cannot read.
        if base and isinstance(index, ast.Constant) and type(index.value) is int:
            if 0 <= index.value < len(base.parents):
                return base.parents[index.value]
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _fold_path(node.left, env, importer)
        right = node.right
        if base and isinstance(right, ast.Constant) and isinstance(right.value, str):
            return base / right.value
        return None
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "dirname":
                base = _fold_path(node.args[0], env, importer) if node.args else None
                return base.parent if base else None
            if func.attr in ("resolve", "abspath", "realpath", "normpath"):
                target = node.args[0] if node.args else func.value
                return _fold_path(target, env, importer)
            if func.attr == "join" and len(node.args) >= 2:
                base = _fold_path(node.args[0], env, importer)
                for part in node.args[1:]:
                    if not (base and isinstance(part, ast.Constant)):
                        return None
                    if not isinstance(part.value, str):
                        return None
                    base = base / part.value
                return base
            return None
        called = func.id if isinstance(func, ast.Name) else ""
        if called in ("str", "Path") and node.args:
            return _fold_path(node.args[0], env, importer)
    return None


def _is_sys_path_call(node: ast.expr) -> bool:
    """True for a `sys.path.insert(...)` or `sys.path.append(...)` call."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ("insert", "append"):
        return False
    path_attr = node.func.value
    return (
        isinstance(path_attr, ast.Attribute)
        and path_attr.attr == "path"
        and isinstance(path_attr.value, ast.Name)
        and path_attr.value.id == "sys"
    )


@functools.cache
def sys_path_roots(importer: Path) -> tuple[Path, ...]:
    """The directories IMPORTER itself puts on `sys.path`, in source order.

    A plain-interpreter script reaches a module outside its own directory by
    inserting that directory first, so a resolver that knows only the
    siblings reads such a module as a third-party distribution and demands a
    pin nobody can add. Pure in its argument (it reads only IMPORTER), which
    is what makes the cache sound.
    """
    tree = ast.parse(importer.read_text(encoding="utf-8"), filename=str(importer))
    env: dict[str, Path] = {}
    roots: list[Path] = []
    for statement in tree.body:
        if not (isinstance(statement, ast.Assign) and len(statement.targets) == 1):
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        folded = _fold_path(statement.value, env, importer)
        # A rebinding this cannot read CLEARS the name — leaving the earlier
        # binding standing would resolve a later insert against a directory
        # the current expression never named.
        env.pop(target.id, None)
        if folded:
            env[target.id] = folded
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_sys_path_call(node)):
            continue
        folded = _fold_path(node.args[-1], env, importer) if node.args else None
        if folded:
            roots.append(folded)
    return tuple(roots)


def local_files(name: str, search: tuple[Path, ...]) -> list[Path]:
    """NAME's files when the walk can reach it without installing a distribution.

    SEARCH is the directory list to look in, nearest first. A package
    resolves to every module under it, not just its `__init__.py`: an
    importer reaching one submodule reaches whatever that submodule imports.
    """
    for parent in search:
        if (parent / f"{name}.py").is_file():
            return [parent / f"{name}.py"]
        if (parent / name / "__init__.py").is_file():
            return sorted((parent / name).rglob("*.py"))
    return []


def _imported_names(path: Path) -> list[str]:
    """The top-level module names PATH imports, including one inside a
    function body — the interpreter must satisfy it whenever that path runs."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names += [alias.name for alias in node.names]
            if node.module:
                names.append(node.module)
    return [name.split(".")[0] for name in names]


def walk_imports(roots: list[Path]) -> tuple[set[str], set[Path]]:
    """The non-stdlib top-level modules ROOTS import, and every file the walk read.

    `sys.path` is one list per PROCESS, not one per module: whatever the
    entry point inserts stays in effect for everything it imports, so the
    search path accumulates as the walk runs rather than resetting per file.
    A name unresolved early is retried once the path stops growing; what is
    still unresolved then is a real third-party name — the walk's second
    return value is what lets a caller confirm the walk followed a local
    import rather than merely stopping.
    """
    seen: set[Path] = set()
    search: list[Path] = []
    queue = list(roots)
    unresolved: list[str] = []
    while True:
        while queue:
            path = queue.pop()
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            for directory in (path.parent, *sys_path_roots(path)):
                if directory not in search:
                    search.append(directory)
            unresolved += _imported_names(path)
        retry, unresolved = unresolved, []
        for name in retry:
            found = local_files(name, tuple(search))
            if found:
                queue.extend(found)
            else:
                unresolved.append(name)
        if not queue:
            return {
                name for name in unresolved if name not in sys.stdlib_module_names
            }, seen
