"""Tests for ci_truth_serum/check_duplicate_class_names.py — the lint that
reports a top-level class name a scanned file defines that another module in
scope also defines.

`top_level_classes` and `find_collisions` are pure detector tests. `scan_repo`
and `main` are driven over a throwaway git repo, since this check's whole
point is comparing an argv file against the rest of a tracked tree.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import commit_all, init_test_repo, load_hook

mod = load_hook("check_duplicate_class_names.py", "check_duplicate_class_names")

_GH = "class Gh:\n    pass\n"


# --------------------------------------------------------------------------- #
# top_level_classes — module level only, annotation exempts.
# --------------------------------------------------------------------------- #
def test_top_level_classes_reads_module_level_definitions() -> None:
    read = mod.top_level_classes("class Gh:\n    pass\n\n\nclass Row:\n    pass\n")
    assert read.defined == ("Gh", "Row")
    assert read.exempt == frozenset()
    assert read.lines == {"Gh": 1, "Row": 5}


def test_a_nested_class_never_collides() -> None:
    src = "def f():\n    class Gh:\n        pass\n\n\nclass Outer:\n    class Gh:\n        pass\n"
    assert mod.top_level_classes(src).defined == ("Outer",)


def test_an_annotated_class_is_recorded_as_defined_and_exempt() -> None:
    src = (
        "class Gh:  # allow-duplicate-class: distinct API, distinct module\n    pass\n"
    )
    read = mod.top_level_classes(src)
    assert (read.defined, read.exempt) == (("Gh",), frozenset({"Gh"}))


def test_an_annotation_on_the_line_above_does_not_exempt() -> None:
    """The annotation must sit where the check looks — on the `class` line."""
    src = "# allow-duplicate-class: wrong line\nclass Gh:\n    pass\n"
    read = mod.top_level_classes(src)
    assert (read.defined, read.exempt) == (("Gh",), frozenset())


def test_a_formatter_split_header_still_exempts() -> None:
    """`ruff format` splits a class header the annotation pushed past the line
    limit, leaving the comment on the closing `):` rather than the `class`
    line."""
    src = (
        "class Gh(\n"
        "    NamedTuple\n"
        "):  # allow-duplicate-class: distinct API, distinct module\n"
        "    pass\n"
    )
    read = mod.top_level_classes(src)
    assert (read.defined, read.exempt) == (("Gh",), frozenset({"Gh"}))


def test_an_annotation_inside_the_class_body_does_not_exempt() -> None:
    src = (
        "class Gh:\n    # allow-duplicate-class: too late, this is the body\n    pass\n"
    )
    read = mod.top_level_classes(src)
    assert (read.defined, read.exempt) == (("Gh",), frozenset())


def test_a_colon_inside_the_header_brackets_does_not_end_it() -> None:
    """Bracket depth tells the header's closing `:` from a `:` inside a
    subscript, so a split header whose bracketed `:` sits on an earlier row
    still exempts."""
    src = (
        "class Gh(\n"
        "    Base[x:y]\n"
        "):  # allow-duplicate-class: distinct API, distinct module\n"
        "    pass\n"
    )
    read = mod.top_level_classes(src)
    assert (read.defined, read.exempt) == (("Gh",), frozenset({"Gh"}))


def test_annotation_without_a_reason_does_not_exempt() -> None:
    src = "class Gh:  # allow-duplicate-class\n    pass\n"
    read = mod.top_level_classes(src)
    assert (read.defined, read.exempt) == (("Gh",), frozenset())


# --------------------------------------------------------------------------- #
# find_collisions — a name collides only across DIFFERENT files.
# --------------------------------------------------------------------------- #
_A = "a/one.py"
_B = "a/two.py"


def _classes(*defined: str, exempt: tuple[str, ...] = ()) -> "mod.ModuleClasses":
    return mod.ModuleClasses(defined, frozenset(exempt))


def test_find_collisions_reports_the_name_in_both_files() -> None:
    assert mod.find_collisions({_A: _classes("Gh", "Row"), _B: _classes("Gh")}) == {
        _A: ["Gh"],
        _B: ["Gh"],
    }


def test_a_unique_name_is_not_a_collision() -> None:
    assert mod.find_collisions({_A: _classes("Row"), _B: _classes("Gh")}) == {
        _A: [],
        _B: [],
    }


def test_a_scanned_file_with_no_classes_is_kept_as_empty() -> None:
    assert mod.find_collisions({_A: _classes()}) == {_A: []}


def test_annotating_one_of_two_files_leaves_the_other_reported() -> None:
    assert mod.find_collisions(
        {_A: _classes("Gh", exempt=("Gh",)), _B: _classes("Gh")}
    ) == {_A: [], _B: ["Gh"]}


def test_annotating_one_of_three_files_leaves_the_other_two_reported() -> None:
    third = "a/c.py"
    assert mod.find_collisions(
        {_A: _classes("Gh", exempt=("Gh",)), _B: _classes("Gh"), third: _classes("Gh")}
    ) == {_A: [], _B: ["Gh"], third: ["Gh"]}


def test_distinct_names_never_collide() -> None:
    assert mod.find_collisions({_A: _classes("Row"), _B: _classes("Row2")}) == {
        _A: [],
        _B: [],
    }


# --------------------------------------------------------------------------- #
# scan_repo / main over a throwaway repo.


def _scan_hits(paths, repo, scopes):
    """The collisions half of a scan, which is all these cases assert on."""
    return mod.scan_repo(paths, repo, scopes).hits


# --------------------------------------------------------------------------- #
@pytest.fixture
def repo(tmp_path: Path) -> Path:
    init_test_repo(tmp_path)
    return tmp_path


def _track(repo_dir: Path, rel: str, text: str) -> None:
    path = repo_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    commit_all(repo_dir, f"add {rel}")


def test_a_collision_between_two_argv_files_is_reported_on_both(repo: Path) -> None:
    _track(repo, "a/one.py", _GH)
    _track(repo, "a/two.py", _GH)
    hits = _scan_hits([str(repo / "a/one.py"), str(repo / "a/two.py")], repo, [])
    assert hits == {"a/one.py": ["Gh"], "a/two.py": ["Gh"]}


def test_a_collision_against_a_file_not_on_argv_is_reported_on_the_argv_file(
    repo: Path,
) -> None:
    """The default scope is the whole tracked tree, so an untouched sibling
    file still supplies the other half of the collision — only the argv file
    is reported."""
    _track(repo, "a/one.py", _GH)
    _track(repo, "a/two.py", _GH)
    hits = _scan_hits([str(repo / "a/one.py")], repo, [])
    assert hits == {"a/one.py": ["Gh"]}


def test_a_unique_class_reports_nothing(repo: Path) -> None:
    _track(repo, "a/one.py", "class Row:\n    pass\n")
    hits = _scan_hits([str(repo / "a/one.py")], repo, [])
    assert hits == {}


def test_a_scope_directory_that_excludes_the_collision_reports_nothing(
    repo: Path,
) -> None:
    """A `--scope` narrower than the whole tree can leave the colliding sibling
    out of comparison — the argv file still knows its own classes, but there
    is nothing else in scope to collide with."""
    _track(repo, "a/one.py", _GH)
    _track(repo, "b/two.py", _GH)
    hits = _scan_hits([str(repo / "a/one.py")], repo, ["a"])
    assert hits == {}


def test_a_scope_directory_that_includes_the_collision_reports_it(repo: Path) -> None:
    _track(repo, "a/one.py", _GH)
    _track(repo, "b/two.py", _GH)
    hits = _scan_hits([str(repo / "a/one.py")], repo, ["a", "b"])
    assert hits == {"a/one.py": ["Gh"]}


def test_a_test_file_is_never_scanned_even_as_the_collision_partner(
    repo: Path,
) -> None:
    _track(repo, "a/one.py", _GH)
    _track(repo, "tests/test_two.py", _GH)
    hits = _scan_hits([str(repo / "a/one.py")], repo, [])
    assert hits == {}


def test_a_test_file_named_on_argv_is_never_reported(repo: Path) -> None:
    _track(repo, "a/one.py", _GH)
    _track(repo, "tests/test_two.py", _GH)
    hits = _scan_hits(
        [str(repo / "a/one.py"), str(repo / "tests/test_two.py")], repo, []
    )
    assert "tests/test_two.py" not in hits


def test_an_untracked_file_is_still_compared_against_when_named_on_argv(
    repo: Path,
) -> None:
    """An argv path outside every scope directory is still scanned for its own
    classes — judging it needs to know what it defines."""
    _track(repo, "b/two.py", _GH)
    outside = repo / "a" / "one.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(_GH, encoding="utf-8")  # never committed
    hits = _scan_hits([str(outside)], repo, ["b"])
    assert hits == {"a/one.py": ["Gh"]}


def test_a_non_python_argv_path_is_ignored(repo: Path) -> None:
    _track(repo, "a/one.py", _GH)
    readme = repo / "README.md"
    readme.write_text("# hi\n", encoding="utf-8")
    hits = _scan_hits([str(repo / "a/one.py"), str(readme)], repo, [])
    assert hits == {}


def test_relative_argv_paths_resolve_against_the_current_directory(
    repo: Path, monkeypatch
) -> None:
    _track(repo, "a/one.py", _GH)
    _track(repo, "a/two.py", _GH)
    monkeypatch.chdir(repo)
    hits = _scan_hits(["a/one.py"], repo, [])
    assert hits == {"a/one.py": ["Gh"]}


# --------------------------------------------------------------------------- #
# main: argv/exit-code contract, message shape, opt-out.
# --------------------------------------------------------------------------- #
def test_main_reports_a_collision_and_exits_1(repo: Path, capsys) -> None:
    _track(repo, "a/one.py", _GH)
    _track(repo, "a/two.py", _GH)
    rc = mod.main([str(repo / "a/one.py"), "--repo-root", str(repo)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "a/one.py:1:" in err
    assert "duplicated class name `Gh`" in err
    assert f"# {mod.OPT_OUT}: <reason>" in err


def test_main_clean_tree_exits_0(repo: Path) -> None:
    _track(repo, "a/one.py", "class Row:\n    pass\n")
    assert mod.main([str(repo / "a/one.py"), "--repo-root", str(repo)]) == 0


def test_main_annotated_collision_exits_0(repo: Path) -> None:
    _track(
        repo,
        "a/one.py",
        "class Gh:  # allow-duplicate-class: deliberate\n    pass\n",
    )
    _track(repo, "a/two.py", _GH)
    assert mod.main([str(repo / "a/one.py"), "--repo-root", str(repo)]) == 0


def test_main_no_paths_exits_2_with_a_message(capsys) -> None:
    assert mod.main(["--scope", "a"]) == 2
    assert "no files to scan" in capsys.readouterr().err


def test_empty_argv_exits_2_via_cli_contract() -> None:
    result = subprocess.run(
        [sys.executable, mod.__file__], capture_output=True, check=False
    )
    assert result.returncode == 2


def test_the_real_tree_scanned_through_run_file_cli_exits_0_or_1() -> None:
    """Non-vacuity: `main` is reachable and returns an int contract status —
    driven through the real module file, not a mocked stand-in."""
    result = subprocess.run(
        [sys.executable, mod.__file__, "ci_truth_serum/check_duplicate_class_names.py"],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        check=False,
    )
    assert result.returncode in (0, 1)


def test_a_file_this_interpreter_cannot_parse_is_refused(repo: Path) -> None:
    """A refusal, never a crash and never a silent skip.

    `ast.parse` raising here used to take the whole check down with a bare
    traceback, so one file the running interpreter is too old for stopped every
    other file in the tree being judged.
    """
    _track(repo, "a/one.py", _GH)
    _track(repo, "a/bad.py", "class Broken(:\n")
    scan = mod.scan_repo([str(repo / "a/one.py"), str(repo / "a/bad.py")], repo, [])
    assert "a/bad.py" in scan.refusals
    assert "cannot parse this file" in scan.refusals["a/bad.py"]
    # The readable file is still scanned, so one bad file costs only itself.
    assert "a/one.py" in scan.classes_by_file
