"""Tests for ci_truth_serum/check_big_tuple_annotations.py — the guard against a
positional ``tuple[...]`` of many fixed elements.

Drives ``violations()`` directly for the exact element-count boundary,
variadic exemption, nested tuples, and the ``# big-tuple-ok:`` suppression,
plus ``main()`` for the argv/exit-code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_big_tuple_annotations.py", "check_big_tuple_annotations")


def _lines(source: str) -> list[int]:
    return [lineno for lineno, _count in mod.violations(source)]


def test_three_element_tuple_is_flagged() -> None:
    hits = mod.violations("def f() -> tuple[str, int, bool]: ...\n")
    assert hits == [(1, 3)]


def test_two_element_tuple_is_not_flagged() -> None:
    # The boundary: a pair is still readable positionally; the guard starts at 3.
    assert mod.violations("def f() -> tuple[str, int]: ...\n") == []


def test_single_and_unparametrized_tuple_are_not_flagged() -> None:
    assert mod.violations("x: tuple[int] = ...\n") == []
    assert mod.violations("x: tuple = ...\n") == []


def test_variadic_homogeneous_tuple_is_not_flagged() -> None:
    # tuple[X, ...] is a homogeneous SEQUENCE, not a positional record.
    assert mod.violations("x: tuple[str, ...] = ...\n") == []


def test_nested_inner_tuple_is_flagged_but_variadic_outer_is_not() -> None:
    # The outer tuple[..., ...] is variadic (exempt); the inner fixed triple is the
    # positional record and IS flagged — one finding, at the inner subscript.
    hits = mod.violations("x: tuple[tuple[str, str, str], ...] = ()\n")
    assert hits == [(1, 3)]


def test_trailing_ellipsis_past_two_elements_is_still_flagged() -> None:
    # tuple[X, ...] is variadic only at exactly two elements (PEP 484). A bogus
    # trailing `, ...` on a longer tuple must not exempt an otherwise-fixed record.
    hits = mod.violations("x: tuple[str, int, bool, ...] = ...\n")
    assert hits == [(1, 4)]


def test_ellipsis_in_a_non_trailing_position_is_still_flagged() -> None:
    hits = mod.violations("x: tuple[int, ..., bool] = ...\n")
    assert hits == [(1, 3)]


def test_empty_tuple_annotation_is_not_flagged() -> None:
    assert mod.violations("x: tuple[()] = ()\n") == []


def test_multi_target_assign_is_flagged_and_suppressible() -> None:
    # An `ast.Assign` is its own suppression unit, not the `ast.AnnAssign` the other
    # cases use, so the marker anywhere in the statement exempts it.
    assert _lines("a = b = tuple[str, int, bool]\n") == [1]
    assert (
        mod.violations("a = b = tuple[str, int, bool]  # big-tuple-ok: reason\n") == []
    )


def test_four_element_tuple_reports_its_count() -> None:
    hits = mod.violations("def f() -> tuple[int, str, bytes, bool]: ...\n")
    assert hits == [(1, 4)]


def test_capitalized_tuple_and_attribute_form_are_flagged() -> None:
    assert _lines("import typing\nx: typing.Tuple[int, str, bytes]\n") == [2]
    assert _lines("from typing import Tuple\nx: Tuple[int, str, bytes]\n") == [2]


def test_suppression_comment_exempts_the_annotation() -> None:
    src = "def f() -> tuple[str, int, bool]:  # big-tuple-ok: interop shape\n    ...\n"
    assert mod.violations(src) == []


def test_suppression_on_any_line_the_annotation_spans() -> None:
    # A multi-line signature: the marker on the closing line still exempts.
    src = "def f() -> tuple[\n    str, int, bool\n]:  # big-tuple-ok: reason\n    ...\n"
    assert mod.violations(src) == []


def test_suppression_without_the_marker_still_flags() -> None:
    # A bare comment that is not the exact marker does not exempt.
    src = "def f() -> tuple[str, int, bool]:  # just a note\n    ...\n"
    assert _lines(src) == [1]


def test_suppression_requires_a_reason() -> None:
    # A bare marker with no `: <reason>` states nothing and does not suppress.
    src = "def f() -> tuple[str, int, bool]:  # big-tuple-ok\n    ...\n"
    assert _lines(src) == [1]


def test_bare_expression_tuple_is_flagged() -> None:
    # A tuple[...] that is not inside an arg / assignment / def — a bare expression
    # statement — is still a positional record and IS flagged. Exercises the
    # suppression-span fallback: the parent climb reaches the Module without
    # matching an enclosing unit, so the marker span defaults to the node's line.
    hits = mod.violations("tuple[int, str, bytes]\n")
    assert hits == [(1, 3)]


def test_bare_expression_tuple_is_suppressible_on_its_own_line() -> None:
    # The fallback span still honors a marker on the node's own line.
    assert mod.violations("tuple[int, str, bytes]  # big-tuple-ok: reason\n") == []


def test_min_elements_flag_raises_the_threshold() -> None:
    # Configurable per consumer: the upstream default is 3, but a caller can raise it.
    assert (
        mod.violations("def f() -> tuple[str, int, bool]: ...\n", min_elements=4) == []
    )
    assert mod.violations(
        "def f() -> tuple[str, int, bool, bytes]: ...\n", min_elements=4
    ) == [(1, 4)]


def test_unparseable_source_is_skipped_not_crashed() -> None:
    assert mod.violations("def f(:\n") == []


# ── main: argv/exit-code contract ─────────────────────────────────────────
def test_main_flags_an_offending_file(tmp_path, capsys) -> None:
    bad = tmp_path / "prod.py"
    bad.write_text("def f() -> tuple[str, int, bool]: ...\n", encoding="utf-8")
    assert mod.main([str(bad)]) == 1
    err = capsys.readouterr().err
    assert "prod.py:1:" in err
    assert "3 elements" in err


def test_main_passes_on_a_clean_file(tmp_path) -> None:
    ok = tmp_path / "prod.py"
    ok.write_text("def f() -> tuple[str, int]: ...\n", encoding="utf-8")
    assert mod.main([str(ok)]) == 0


def test_main_skips_test_files() -> None:
    hit = mod.main(["tests/test_x.py"])
    assert hit == 0


def test_main_honors_min_elements_flag(tmp_path) -> None:
    src = tmp_path / "prod.py"
    src.write_text("def f() -> tuple[str, int, bool]: ...\n", encoding="utf-8")
    assert mod.main(["--min-elements", "4", str(src)]) == 0
    assert mod.main(["--min-elements", "3", str(src)]) == 1


def test_main_skips_unreadable_path(tmp_path) -> None:
    missing = tmp_path / "does-not-exist.py"
    assert mod.main([str(missing)]) == 0


def test_empty_argv_exits_2_via_cli_contract() -> None:
    # run_file_cli(main) refuses an empty argv rather than reporting a clean
    # pass over nothing.
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, mod.__file__], capture_output=True, check=False
    )
    assert result.returncode == 2


@pytest.mark.parametrize(
    "name",
    ["tests/test_x.py", "tests/helpers/u.py", "test_x.py", "x_test.py", "conftest.py"],
)
def test_is_test_path_scopes_are_excluded(name: str) -> None:
    # A test's ad-hoc tuple carries no production-runtime contract; main skips it
    # even though `read_text` never runs against a real path.
    assert mod.main([name]) == 0
