"""Example-based tests (mutation oracle) for ci_truth_serum/_cts_py_imports.py — the
helper that walks a Python entry point's own local imports the way the plain
interpreter would resolve them.

Pins the two contracts `check_sparse_checkout_closure` depends on: a script
reachable only through a `sys.path` insert the entry point makes itself is still
found, and a name that is not a local file — stdlib or genuinely third-party —
never appears in the unresolved set as a false claim of a missing dependency
(stdlib) or as a resolved local file (third-party).
"""

from pathlib import Path

import pytest

from tests._helpers import load_hook

py_imports = load_hook("_cts_py_imports.py", "check_py_imports")


def write(root: Path, rel: str, body: str) -> Path:
    """Write BODY to ROOT/REL, creating parent directories, and return the path."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ── interpreter_scripts ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "words, expected",
    [
        (["python3", "script.py"], ["script.py"]),
        (["/usr/bin/python3.12", "script.py"], ["script.py"]),
        # An option before the script does not end the search for one.
        (["python3", "-I", "script.py"], ["script.py"]),
        # `-m` names a MODULE to run; the `.py` after it is that module's own
        # argument, not a file the interpreter imports.
        (["python3", "-m", "pytest", "tests/x.py"], []),
        # A word an expansion decided reads as None and ends the search — a
        # candidate this cannot read must not be guessed at.
        (["python3", None, "script.py"], []),
        # No interpreter word at all.
        (["bash", "script.py"], []),
    ],
)
def test_interpreter_scripts(words, expected) -> None:
    assert py_imports.interpreter_scripts(words) == expected


# ── local_files ──────────────────────────────────────────────────────────
def test_local_files_resolves_a_sibling_module(tmp_path: Path) -> None:
    mod = write(tmp_path, "sibling.py", "x = 1\n")
    assert py_imports.local_files("sibling", (tmp_path,)) == [mod]


def test_local_files_resolves_every_module_under_a_package(tmp_path: Path) -> None:
    write(tmp_path, "pkg/__init__.py", "")
    a = write(tmp_path, "pkg/a.py", "")
    b = write(tmp_path, "pkg/b.py", "")
    init = tmp_path / "pkg" / "__init__.py"
    assert py_imports.local_files("pkg", (tmp_path,)) == sorted([init, a, b])


def test_local_files_returns_nothing_for_a_name_absent_from_every_root(
    tmp_path: Path,
) -> None:
    assert py_imports.local_files("nope", (tmp_path,)) == []


# ── walk_imports: resolvable local imports ────────────────────────────────
def test_a_plain_sibling_import_is_resolved_and_visited(tmp_path: Path) -> None:
    entry = write(tmp_path, "entry.py", "import sibling\n")
    sibling = write(tmp_path, "sibling.py", "x = 1\n")
    unresolved, visited = py_imports.walk_imports([entry])
    assert unresolved == set()
    assert visited == {entry, sibling}


def test_a_relative_import_resolves_the_sibling_it_names(tmp_path: Path) -> None:
    entry = write(tmp_path, "entry.py", "from . import helper\n")
    helper = write(tmp_path, "helper.py", "x = 1\n")
    unresolved, visited = py_imports.walk_imports([entry])
    assert unresolved == set()
    assert visited == {entry, helper}


def test_from_a_package_import_walks_every_file_the_package_holds(
    tmp_path: Path,
) -> None:
    """`from mypkg import thing` at module level (level 0) names only the
    MODULE `mypkg`, never the imported attribute `thing` — `thing` is not a
    submodule, so treating it as one would demand a file that does not exist."""
    entry = write(tmp_path, "entry.py", "from mypkg import thing\n")
    init = write(tmp_path, "mypkg/__init__.py", "")
    sub = write(tmp_path, "mypkg/sub.py", "y = 2\n")
    unresolved, visited = py_imports.walk_imports([entry])
    assert unresolved == set()
    assert visited == {entry, init, sub}


def test_an_import_reached_only_through_a_sys_path_insert_is_still_found(
    tmp_path: Path,
) -> None:
    """A script that puts a directory OUTSIDE its own tree on `sys.path` (the
    plain-interpreter idiom `sys.path.insert(0, str(Path(__file__).resolve()
    .parent.parent / "libs"))`) must still have that directory searched."""
    entry = write(
        tmp_path,
        "app/entry.py",
        "import sys\n"
        "from pathlib import Path\n"
        'sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "libs"))\n'
        "import helper2\n",
    )
    helper2 = write(tmp_path, "libs/helper2.py", "z = 3\n")
    unresolved, visited = py_imports.walk_imports([entry])
    # `sys` and `pathlib` are stdlib, so neither is a missing dependency.
    assert unresolved == set()
    assert visited == {entry, helper2}


def test_an_os_path_join_dirname_insert_is_folded_too(tmp_path: Path) -> None:
    """The `os.path` idiom for the same insert, exercised separately from the
    `pathlib` one above so a mutant that breaks only one fold path is caught."""
    entry = write(
        tmp_path,
        "entry.py",
        "import os\n"
        "import sys\n"
        'sys.path.insert(0, os.path.join(os.path.dirname(__file__), "shared"))\n'
        "import util\n",
    )
    util = write(tmp_path, "shared/util.py", "a = 1\n")
    unresolved, visited = py_imports.walk_imports([entry])
    assert unresolved == set()
    assert visited == {entry, util}


def test_a_parents_index_insert_is_folded(tmp_path: Path) -> None:
    """`Path(__file__).resolve().parents[N]` is a distinct fold from a run of
    `.parent`, and must not raise when N is out of range for a shallow tree."""
    entry = write(
        tmp_path,
        "a/b/entry.py",
        "from pathlib import Path\n"
        "import sys\n"
        'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared2"))\n'
        "import mod\n",
    )
    mod_file = write(tmp_path, "a/shared2/mod.py", "b = 2\n")
    unresolved, visited = py_imports.walk_imports([entry])
    assert unresolved == set()
    assert visited == {entry, mod_file}


# ── walk_imports: names that stay unresolved ──────────────────────────────
def test_a_missing_module_stays_unresolved_and_unvisited(tmp_path: Path) -> None:
    entry = write(tmp_path, "entry.py", "import totally_not_a_real_package_xyz\n")
    unresolved, visited = py_imports.walk_imports([entry])
    assert unresolved == {"totally_not_a_real_package_xyz"}
    assert visited == {entry}


def test_an_entrypoint_with_no_imports_visits_only_itself(tmp_path: Path) -> None:
    entry = write(tmp_path, "entry.py", "x = 1\n")
    unresolved, visited = py_imports.walk_imports([entry])
    assert unresolved == set()
    assert visited == {entry}


# ── walk_imports: unreadable / unparseable input ──────────────────────────
def test_a_missing_entrypoint_is_silently_skipped(tmp_path: Path) -> None:
    """A root that names no real file is not an error — `check_sparse_checkout_
    closure` may pass a candidate entry point that does not exist on disk."""
    unresolved, visited = py_imports.walk_imports([tmp_path / "missing.py"])
    assert unresolved == set()
    assert visited == set()


def test_an_unparseable_entrypoint_raises(tmp_path: Path) -> None:
    """Nothing here catches a syntax error — the repo's fail-loud convention —
    so a half-written file surfaces as a crash, never as a silent empty walk."""
    entry = write(tmp_path, "entry.py", "def broken(:\n")
    with pytest.raises(SyntaxError):
        py_imports.walk_imports([entry])
